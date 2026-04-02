"""
Dynamic VLAN switching fixtures for unified pool architecture.

Switches DUT ports from isolated VLANs (100-108) to mesh VLAN (200) before
LibreMesh tests, and restores them on teardown. Uses the labgrid-switch-abstraction
package (pip install).

For single-node tests: uses LG_PLACE to identify the DUT.
For mesh tests: uses LG_MESH_PLACES (comma-separated) to switch all nodes.

Set VLAN_SWITCH_DISABLED=1 to skip VLAN switching (e.g. when switch is
already configured manually or running in mesh-only mode).

Config path: set SWITCH_DUT_CONFIG env var to point at dut-config.yaml.
"""

import logging
import os

import pytest

logger = logging.getLogger(__name__)

VLAN_MESH = 200
PLACE_PREFIX = "labgrid-fcefyn-"


def _place_to_dut_name(place: str) -> str:
    """Extract DUT name from labgrid place name."""
    if place.startswith(PLACE_PREFIX):
        return place[len(PLACE_PREFIX):]
    return place


def _is_disabled() -> bool:
    return os.environ.get("VLAN_SWITCH_DISABLED", "").lower() in ("1", "true", "yes")


def _try_import_switch_abstraction():
    """Try to import switch_abstraction; return module or None."""
    try:
        from switch_abstraction import vlan_manager
        return vlan_manager
    except ImportError:
        return None


@pytest.fixture(scope="session")
def vlan_manager_mod():
    """Provide the vlan_manager module (session-scoped, loaded once)."""
    if _is_disabled():
        pytest.skip("VLAN switching disabled via VLAN_SWITCH_DISABLED")
    mod = _try_import_switch_abstraction()
    if mod is None:
        logger.warning(
            "labgrid-switch-abstraction not installed; VLAN switching skipped. "
            "Install: pip install git+https://github.com/<org>/labgrid-switch-abstraction.git"
        )
    return mod


@pytest.fixture(scope="session")
def dut_map(vlan_manager_mod):
    """Load DUT hardware map once per session."""
    if vlan_manager_mod is None:
        return {}
    return vlan_manager_mod.load_dut_map()


@pytest.fixture(autouse=True)
def mesh_vlan_single(request, vlan_manager_mod, dut_map):
    """Switch DUT port to VLAN 200 for single-node LibreMesh tests.

    Activates when LG_PLACE is set and LG_MESH_PLACES is not (mesh tests
    handle their own VLAN switching via mesh_vlan_multi).
    """
    if _is_disabled() or vlan_manager_mod is None:
        yield
        return

    if os.environ.get("LG_MESH_PLACES"):
        yield
        return

    place = os.environ.get("LG_PLACE", "")
    if not place or place == "+":
        yield
        return

    dut_name = _place_to_dut_name(place)
    if dut_name not in dut_map:
        logger.warning("DUT '%s' not in config, skipping VLAN switch", dut_name)
        yield
        return

    logger.info("Switching DUT '%s' to VLAN %d", dut_name, VLAN_MESH)
    ok = vlan_manager_mod.set_port_vlan(dut_name, VLAN_MESH, dut_map=dut_map)
    if not ok:
        pytest.fail(f"Failed to switch DUT '{dut_name}' to VLAN {VLAN_MESH}")

    yield

    logger.info("Restoring DUT '%s' to isolated VLAN", dut_name)
    vlan_manager_mod.restore_port_vlan(dut_name, dut_map=dut_map)


@pytest.fixture(scope="session")
def mesh_vlan_multi(vlan_manager_mod, dut_map):
    """Switch all mesh DUT ports to VLAN 200 for multi-node tests.

    Activates based on LG_MESH_PLACES. Session-scoped: switches VLANs once
    for all mesh tests, restores on session teardown.

    Uses batch functions to send all VLAN commands in a single SSH session,
    avoiding one connection per DUT.
    """
    if _is_disabled() or vlan_manager_mod is None:
        yield []
        return

    places_str = os.environ.get("LG_MESH_PLACES", "")
    if not places_str:
        yield []
        return

    places = [p.strip() for p in places_str.split(",") if p.strip()]
    dut_names = [_place_to_dut_name(p) for p in places]
    valid_duts = [d for d in dut_names if d in dut_map]

    if not valid_duts:
        logger.warning("No mesh DUTs found in config, skipping VLAN switch")
        yield dut_names
        return

    logger.info("Switching %d mesh DUTs to VLAN %d in batch: %s", len(valid_duts), VLAN_MESH, valid_duts)
    ok = vlan_manager_mod.set_ports_vlan_batch(valid_duts, VLAN_MESH, dut_map=dut_map)
    if not ok:
        pytest.fail(f"Batch VLAN switch to {VLAN_MESH} failed for DUTs: {valid_duts}")

    yield dut_names

    logger.info("Restoring %d mesh DUTs to isolated VLANs in batch", len(valid_duts))
    vlan_manager_mod.restore_ports_vlan_batch(valid_duts, dut_map=dut_map)
