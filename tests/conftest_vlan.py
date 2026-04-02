"""
Optional dynamic VLAN switching fixtures for multi-node tests.

Switches DUT ports between isolated VLANs (one DUT per VLAN) and a shared
VLAN before multi-node tests, restoring on teardown. Uses the
labgrid-switch-abstraction package for vendor-agnostic switch management.

Activation:
    - Enabled when LG_MULTI_PLACES is set (comma-separated place names)
    - Disabled by default: set VLAN_SWITCH_ENABLED=1 to activate
    - Requires labgrid-switch-abstraction installed (pip install .[switch])

Config:
    - Switch credentials: ~/.config/switch.conf
    - DUT map: /etc/testbed/dut-config.yaml (or SWITCH_DUT_CONFIG env var)

See docs/switch-abstraction.md for setup instructions.
"""

import logging
import os

import pytest

logger = logging.getLogger(__name__)


def _is_enabled() -> bool:
    return os.environ.get("VLAN_SWITCH_ENABLED", "").lower() in ("1", "true", "yes")


def _try_import_switch_abstraction():
    try:
        from switch_abstraction import vlan_manager
        return vlan_manager
    except ImportError:
        return None


def _place_to_dut_name(place: str) -> str:
    """Extract DUT name from a labgrid place name.

    Strips any lab-specific prefix (everything up to and including the
    second hyphen). Override via PLACE_PREFIX env var if your lab uses
    a different convention.

    Examples:
        labgrid-mylab-router_1  ->  router_1
        router_1                ->  router_1
    """
    prefix = os.environ.get("PLACE_PREFIX", "")
    if prefix and place.startswith(prefix):
        return place[len(prefix):]
    parts = place.split("-", 2)
    if len(parts) == 3:
        return parts[2]
    return place


@pytest.fixture(scope="session")
def vlan_manager_mod():
    """Provide the vlan_manager module, or skip if unavailable."""
    if not _is_enabled():
        yield None
        return

    mod = _try_import_switch_abstraction()
    if mod is None:
        logger.warning(
            "VLAN_SWITCH_ENABLED=1 but labgrid-switch-abstraction not installed. "
            "Install: pip install labgrid-switch-abstraction  "
            "See: https://github.com/fcefyn-testbed/labgrid-switch-abstraction"
        )
        yield None
        return

    yield mod


@pytest.fixture(scope="session")
def dut_config(vlan_manager_mod):
    """Load full DUT configuration once per session."""
    if vlan_manager_mod is None:
        return {}
    return vlan_manager_mod.load_config()


@pytest.fixture(scope="session")
def dut_map(dut_config):
    """Extract the DUT hardware map from configuration."""
    return dut_config.get("duts", {})


@pytest.fixture(scope="session")
def shared_vlan_multi(vlan_manager_mod, dut_config, dut_map):
    """Switch all multi-node DUT ports to a shared VLAN.

    Reads DUT names from LG_MULTI_PLACES env var (comma-separated).
    Session-scoped: switches VLANs once, restores on teardown.
    Uses batch operations for a single SSH session.

    The shared VLAN ID comes from dut-config.yaml (switch.vlan_topology)
    or defaults to 200.
    """
    if vlan_manager_mod is None:
        yield []
        return

    places_str = os.environ.get("LG_MULTI_PLACES", "")
    if not places_str:
        yield []
        return

    places = [p.strip() for p in places_str.split(",") if p.strip()]
    dut_names = [_place_to_dut_name(p) for p in places]
    valid_duts = [d for d in dut_names if d in dut_map]

    if not valid_duts:
        logger.warning("No DUTs from LG_MULTI_PLACES found in dut-config.yaml")
        yield dut_names
        return

    vlan_mesh = vlan_manager_mod.get_vlan_mesh(dut_config)
    logger.info(
        "Switching %d DUTs to shared VLAN %d (batch): %s",
        len(valid_duts), vlan_mesh, valid_duts,
    )
    ok = vlan_manager_mod.set_ports_vlan_batch(
        valid_duts, vlan_mesh, dut_map=dut_map, config=dut_config,
    )
    if not ok:
        pytest.fail(f"Batch VLAN switch to {vlan_mesh} failed for: {valid_duts}")

    yield dut_names

    logger.info("Restoring %d DUTs to isolated VLANs (batch)", len(valid_duts))
    vlan_manager_mod.restore_ports_vlan_batch(
        valid_duts, dut_map=dut_map, config=dut_config,
    )


@pytest.fixture(autouse=True)
def shared_vlan_single(request, vlan_manager_mod, dut_config, dut_map):
    """Switch a single DUT port to the shared VLAN for tests that need it.

    Activates when LG_PLACE is set and LG_MULTI_PLACES is not. Only runs
    for test functions/classes marked with @pytest.mark.shared_vlan.
    """
    if vlan_manager_mod is None:
        yield
        return

    if os.environ.get("LG_MULTI_PLACES"):
        yield
        return

    marker = request.node.get_closest_marker("shared_vlan")
    if marker is None:
        yield
        return

    place = os.environ.get("LG_PLACE", "")
    if not place or place == "+":
        yield
        return

    dut_name = _place_to_dut_name(place)
    if dut_name not in dut_map:
        logger.warning("DUT '%s' not in dut-config.yaml, skipping VLAN switch", dut_name)
        yield
        return

    vlan_mesh = vlan_manager_mod.get_vlan_mesh(dut_config)
    logger.info("Switching DUT '%s' to shared VLAN %d", dut_name, vlan_mesh)
    ok = vlan_manager_mod.set_port_vlan(
        dut_name, vlan_mesh, dut_map=dut_map, config=dut_config,
    )
    if not ok:
        pytest.fail(f"Failed to switch DUT '{dut_name}' to VLAN {vlan_mesh}")

    yield

    logger.info("Restoring DUT '%s' to isolated VLAN", dut_name)
    vlan_manager_mod.restore_port_vlan(dut_name, dut_map=dut_map, config=dut_config)
