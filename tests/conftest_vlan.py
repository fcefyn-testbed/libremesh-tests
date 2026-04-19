"""
Dynamic VLAN switching fixtures for unified pool architecture.

Switches DUT ports from isolated VLANs (100-108) to mesh VLAN (200) before
LibreMesh tests, and restores them on teardown.

VLAN commands are executed via ``switch-vlan`` (labgrid-switch-abstraction):

- **Remote first**: if ``LG_PROXY`` is set, the command is sent via SSH to the
  proxy host (developer laptop scenario). The lab host owns the switch
  credentials and dut-config, so VLAN changes always run there.
- **Local fallback**: if ``LG_PROXY`` is NOT set, ``switch-vlan`` runs locally
  (lab host, CI runner). Requires ``switch-vlan`` in PATH and a configured
  ``SWITCH_DUT_CONFIG`` / ``SWITCH_PASSWORD``.

No local ``dut-config.yaml`` or ``labgrid-switch-abstraction`` install is
required on the developer machine.

For single-node tests: uses LG_PLACE to identify the DUT.
For mesh tests: uses LG_MESH_PLACES (comma-separated) to switch all nodes.

Set VLAN_SWITCH_DISABLED=1 to skip VLAN switching (e.g. when switch is
already configured manually or running in mesh-only mode).
"""

import logging
import os
import shutil
import subprocess

import pytest

logger = logging.getLogger(__name__)

VLAN_MESH = 200
MESH_TFTP_IP_DEFAULT = "192.168.200.1"
PLACE_PREFIX = "labgrid-fcefyn-"

SSH_TIMEOUT = 30


def _place_to_dut_name(place: str) -> str:
    """Extract DUT name from labgrid place name."""
    if place.startswith(PLACE_PREFIX):
        return place[len(PLACE_PREFIX) :]
    return place


def _is_disabled() -> bool:
    return os.environ.get("VLAN_SWITCH_DISABLED", "").lower() in ("1", "true", "yes")


def _resolve_proxy_host() -> str | None:
    """Extract SSH host from LG_PROXY (e.g. 'ssh://labgrid-fcefyn' -> 'labgrid-fcefyn')."""
    raw = os.environ.get("LG_PROXY", "").strip()
    if not raw:
        return None
    return raw.removeprefix("ssh://")


def _run_switch_vlan(args: list[str]) -> bool:
    """Run ``switch-vlan`` via SSH to LG_PROXY, or locally if no proxy is set.

    Rationale: ``LG_PROXY`` indicates the lab is at a remote host, so the VLAN
    command must run there (the proxy host owns switch credentials and
    dut-config). Only fall back to local execution when no proxy is set
    (lab host or CI runner).

    Returns True on success, False on failure (logged, never raises).
    """
    proxy = _resolve_proxy_host()
    if proxy:
        cmd = ["ssh", proxy, "switch-vlan " + " ".join(args)]
    elif shutil.which("switch-vlan"):
        cmd = ["switch-vlan", *args]
    else:
        logger.warning(
            "LG_PROXY not set and switch-vlan not in PATH; skipping VLAN command"
        )
        return False

    logger.debug("VLAN command: %s", cmd)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=SSH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        logger.error("switch-vlan timed out after %ds: %s", SSH_TIMEOUT, cmd)
        return False

    if result.returncode != 0:
        stderr = result.stderr.strip()
        hint = ""
        if "password required" in stderr.lower() and proxy:
            hint = (
                f"\nHint: switch-vlan ran on '{proxy}' as the SSH user. "
                f"That user needs a readable switch.conf "
                f"(per-user '~/.config/switch.conf' or system-wide "
                f"'/etc/switch.conf' with group access). "
                f"See libremesh-tests CONTRIBUTING_LAB.md."
            )
        logger.error(
            "switch-vlan failed (rc=%d): %s\nstderr: %s%s",
            result.returncode,
            cmd,
            stderr,
            hint,
        )
        return False

    logger.debug("switch-vlan stdout: %s", result.stdout.strip())
    return True


def _switch_to_mesh(dut_names: list[str]) -> bool:
    return _run_switch_vlan([*dut_names, str(VLAN_MESH)])


def _restore_vlans(dut_names: list[str]) -> bool:
    return _run_switch_vlan([*dut_names, "--restore"])


@pytest.fixture(autouse=True)
def mesh_vlan_single(request):
    """Switch DUT port to VLAN 200 for single-node LibreMesh tests.

    Activates when LG_PLACE is set and LG_MESH_PLACES is not (mesh tests
    handle their own VLAN switching via mesh_vlan_multi).
    """
    if _is_disabled():
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

    logger.info("Switching DUT '%s' to VLAN %d", dut_name, VLAN_MESH)
    ok = _switch_to_mesh([dut_name])
    if not ok:
        pytest.fail(f"Failed to switch DUT '{dut_name}' to VLAN {VLAN_MESH}")

    mesh_tftp_ip = os.environ.get("LG_MESH_TFTP_IP", MESH_TFTP_IP_DEFAULT)
    os.environ["TFTP_SERVER_IP"] = mesh_tftp_ip
    logger.info("Set TFTP_SERVER_IP=%s for VLAN %d", mesh_tftp_ip, VLAN_MESH)

    yield

    os.environ.pop("TFTP_SERVER_IP", None)
    logger.info("Restoring DUT '%s' to isolated VLAN", dut_name)
    _restore_vlans([dut_name])


@pytest.fixture(scope="session")
def mesh_vlan_multi():
    """Switch all mesh DUT ports to VLAN 200 for multi-node tests.

    Activates based on LG_MESH_PLACES. Session-scoped: switches VLANs once
    for all mesh tests, restores on session teardown.
    """
    if _is_disabled():
        yield []
        return

    places_str = os.environ.get("LG_MESH_PLACES", "")
    if not places_str:
        yield []
        return

    places = [p.strip() for p in places_str.split(",") if p.strip()]
    dut_names = [_place_to_dut_name(p) for p in places]

    logger.info(
        "Switching %d mesh DUTs to VLAN %d: %s",
        len(dut_names),
        VLAN_MESH,
        dut_names,
    )
    ok = _switch_to_mesh(dut_names)
    if not ok:
        pytest.fail(f"VLAN switch to {VLAN_MESH} failed for DUTs: {dut_names}")

    mesh_tftp_ip = os.environ.get("LG_MESH_TFTP_IP", MESH_TFTP_IP_DEFAULT)
    os.environ["TFTP_SERVER_IP"] = mesh_tftp_ip
    logger.info("Set TFTP_SERVER_IP=%s for VLAN %d", mesh_tftp_ip, VLAN_MESH)

    yield dut_names

    os.environ.pop("TFTP_SERVER_IP", None)
    logger.info("Restoring %d mesh DUTs to isolated VLANs", len(dut_names))
    _restore_vlans(dut_names)
