#!/usr/bin/env python3
"""Boot a single labgrid node and hold the session until signaled to stop.

This script is designed to be launched as a subprocess by conftest_mesh.py.
Each node runs in its own process, avoiding labgrid's internal thread-safety
issues (StepLogger, coordinator multi-session crashes) that prevent
in-process parallel booting.

Usage:
    python mesh_boot_node.py \
        --place labgrid-fcefyn-belkin_rt3200_2 \
        --image /srv/tftp/firmwares/belkin_rt3200/lime-...itb \
        --target-yaml /path/to/targets/linksys_e8450.yaml \
        --status-file /tmp/mesh_node_XYZ.json \
        --stop-file /tmp/mesh_stop_XYZ

Set LG_MESH_KEEP_POWERED=1 (or true/yes) to leave the node powered on when stopping.
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("mesh_boot_node")

STOP_POLL_INTERVAL = 2


def _patch_reg_driver_idempotent():
    """Make target_factory.reg_driver idempotent for multiple Environment objects."""
    from labgrid.factory import target_factory

    if hasattr(target_factory.reg_driver, "__wrapped__"):
        return

    original = target_factory.reg_driver

    def idempotent(cls):
        if cls.__name__ in target_factory.all_classes:
            return target_factory.all_classes[cls.__name__]
        return original(cls)

    idempotent.__wrapped__ = original
    target_factory.reg_driver = idempotent


def _write_status(path: str, data: dict):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _query_node_ip(target, timeout: int = 120) -> str:
    """Query the booted node's br-lan IPv4 address via serial console (ShellDriver).

    Uses serial instead of SSH because LibreMesh may reassign the IP
    (e.g. from 192.168.1.1 to 10.13.x.x), making the exporter's
    NetworkService address stale. Serial is always reachable.
    """
    import re

    shell = target.get_driver("ShellDriver")
    deadline = time.time() + timeout

    # Wait for console to fully settle before running commands
    # Some devices (BananaPi R4) output kernel messages after login
    time.sleep(5)

    ip_cmd = ("ip -4 -o addr show br-lan 2>/dev/null || "
              "ip -4 -o addr show eth0 2>/dev/null || "
              "ip -4 -o addr show dev $(ip route show default "
              "| awk '{print $5}' | head -1) 2>/dev/null")

    while time.time() < deadline:
        try:
            output = shell.run_check(ip_cmd)
            text = "\n".join(output)
            match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", text)
            if match:
                return match.group(1)
        except Exception:
            pass
        time.sleep(5)

    logger.warning("Could not determine IP for node, returning empty")
    return ""


def _ensure_batman_mesh(target) -> bool:
    """Ensure batman-adv has at least one active mesh interface.

    If batman has no active interfaces, detect ports with carrier
    and configure batman on them. This handles devices like BananaPi R4
    where LibreMesh misconfigures mesh on SFP ports instead of DSA ports.

    Returns True if batman is active (either already or after fix).
    """
    shell = target.get_driver("ShellDriver")

    try:
        output = shell.run_check("batctl if 2>/dev/null || echo 'batctl_not_found'")
        batctl_output = "\n".join(output)
    except Exception as e:
        logger.warning("Failed to run batctl if: %s", e)
        return False

    if "batctl_not_found" in batctl_output:
        logger.info("batctl not available on this device, skipping batman fix")
        return False

    active_lines = [line for line in batctl_output.splitlines()
                    if line.strip() and "active" in line.lower()]
    if active_lines:
        logger.info("Batman-adv already has active interfaces: %s", active_lines)
        return True

    logger.warning("Batman-adv has no active interfaces, attempting auto-fix")

    try:
        output = shell.run_check("ls /sys/class/net/ | grep -E '^(lan|eth)[0-9]+$'")
        all_ifaces = [iface.strip() for iface in output if iface.strip()]
        logger.info("All matching interfaces: %s", all_ifaces)
    except Exception as e:
        logger.warning("Failed to list interfaces: %s", e)
        all_ifaces = []

    dsa_ports = [i for i in all_ifaces if i.startswith("lan") or i.startswith("wan")]
    eth_ports = [i for i in all_ifaces if i.startswith("eth")]

    dsa_conduits = set()
    for iface in eth_ports:
        try:
            out = shell.run_check(
                f"ls /sys/class/net/{iface}/dsa 2>/dev/null && echo 'is_dsa_conduit' "
                f"|| echo 'not_dsa_conduit'"
            )
            if "is_dsa_conduit" in "\n".join(out):
                dsa_conduits.add(iface)
                logger.info("Interface %s is a DSA conduit (master), excluding", iface)
        except Exception:
            pass

    candidates = dsa_ports + [i for i in eth_ports if i not in dsa_conduits]
    logger.info("Candidate interfaces (DSA ports first, conduits excluded): %s", candidates)

    if not candidates:
        logger.warning("No candidate interfaces found")
        return False

    fixed = False
    for iface in candidates:
        try:
            carrier_out = shell.run_check(f"cat /sys/class/net/{iface}/carrier 2>/dev/null || echo 0")
            carrier = carrier_out[0].strip() if carrier_out else "0"
        except Exception:
            carrier = "0"

        if carrier != "1":
            logger.info("Interface %s has no carrier, skipping", iface)
            continue

        logger.info("Configuring batman-adv on %s (has carrier)", iface)
        batadv_dev = f"lm_net_{iface}_batadv_dev"
        batadv_if = f"lm_net_{iface}_batadv_if"
        try:
            shell.run_check(f"uci set network.{batadv_dev}=device")
            shell.run_check(f"uci set network.{batadv_dev}.name={iface}_29")
            shell.run_check(f"uci set network.{batadv_dev}.type=8021ad")
            shell.run_check(f"uci set network.{batadv_dev}.ifname={iface}")
            shell.run_check(f"uci set network.{batadv_dev}.vid=29")
            shell.run_check(f"uci set network.{batadv_if}=interface")
            shell.run_check(f"uci set network.{batadv_if}.proto=batadv_hardif")
            shell.run_check(f"uci set network.{batadv_if}.master=bat0")
            shell.run_check(f"uci set network.{batadv_if}.mtu=1500")
            shell.run_check(f"uci set network.{batadv_if}.device={iface}_29")
            shell.run_check("uci commit network")
            logger.info("UCI config applied for %s, restarting network...", iface)
            shell.run_check("/etc/init.d/network restart")
            time.sleep(15)
            fixed = True
            break
        except Exception as e:
            logger.warning("Failed to configure batman on %s: %s", iface, e)
            continue

    if fixed:
        try:
            output = shell.run_check("batctl if 2>/dev/null")
            batctl_output = "\n".join(output)
            active_lines = [line for line in batctl_output.splitlines()
                            if line.strip() and "active" in line.lower()]
            if active_lines:
                logger.info("Batman-adv now has active interfaces: %s", active_lines)
                return True
            else:
                logger.warning("Batman still has no active interfaces after fix")
        except Exception as e:
            logger.warning("Failed to verify batman status: %s", e)

    logger.warning("Could not activate batman-adv mesh")
    return False


def boot_node(place: str, image: str, target_yaml: str,
              coordinator: str) -> dict:
    """Boot a node via labgrid and return session state needed by the parent."""
    _patch_reg_driver_idempotent()

    from labgrid import Environment
    from labgrid.remote.client import start_session

    os.environ["LG_PLACE"] = place
    os.environ["LG_IMAGE"] = image

    env = Environment(config_file=target_yaml)

    args = argparse.Namespace(
        place=place,
        state=None,
        initial_state=None,
        allow_unmatched=False,
    )
    session = start_session(coordinator, extra={"env": env, "role": "main", "args": args})
    loop = session.loop

    try:
        loop.run_until_complete(session.acquire())
    except Exception:
        logger.warning("Acquire failed for %s, releasing stale lock first", place)
        try:
            loop.run_until_complete(session.release())
        except Exception:
            pass
        loop.run_until_complete(session.acquire())
    logger.info("Place %s acquired", place)

    lg_place = session.get_place(place)
    target = session._get_target(lg_place)

    strategy = target.get_driver("Strategy")

    # #region agent log
    import json as _json, time as _time
    _dbg_log = "/tmp/mesh_debug_033974.log"
    try:
        _uboot = target.get_driver("UBootDriver")
        _dbg = {"sessionId": "033974", "hypothesisId": "A", "location": "mesh_boot_node.py:251",
                "message": "UBootDriver attrs before transition", "timestamp": int(_time.time() * 1000),
                "data": {"place": place, "login_timeout": _uboot.login_timeout,
                         "prompt": _uboot.prompt, "interrupt": repr(_uboot.interrupt)}}
        with open(_dbg_log, "a") as _f:
            _f.write(_json.dumps(_dbg) + "\n")
    except Exception as _e:
        with open(_dbg_log, "a") as _f:
            _f.write(_json.dumps({"sessionId": "033974", "hypothesisId": "A", "location": "mesh_boot_node.py:251",
                "message": "Failed to get UBootDriver", "timestamp": int(_time.time() * 1000),
                "data": {"place": place, "error": str(_e)}}) + "\n")
    # #endregion

    # #region agent log
    try:
        _pdu = target.get_driver("PDUDaemonDriver")
        _dbg2 = {"sessionId": "033974", "hypothesisId": "B", "location": "mesh_boot_node.py:252",
                 "message": "PDUDaemonDriver attrs", "timestamp": int(_time.time() * 1000),
                 "data": {"place": place, "pdu": str(_pdu)}}
        with open(_dbg_log, "a") as _f:
            _f.write(_json.dumps(_dbg2) + "\n")
    except Exception as _e2:
        with open(_dbg_log, "a") as _f:
            _f.write(_json.dumps({"sessionId": "033974", "hypothesisId": "B", "location": "mesh_boot_node.py:252",
                "message": "Failed to get PDUDaemonDriver", "timestamp": int(_time.time() * 1000),
                "data": {"place": place, "error": str(_e2)}}) + "\n")
    # #endregion

    logger.info("Booting %s to shell...", place)
    strategy.transition("shell")
    logger.info("Shell ready on %s", place)

    _ensure_batman_mesh(target)

    node_ip = _query_node_ip(target)
    logger.info("Node %s has IP %s", place, node_ip)

    return {
        "session": session,
        "target": target,
        "strategy": strategy,
        "ip": node_ip,
    }


def _should_keep_powered() -> bool:
    """Check LG_MESH_KEEP_POWERED to leave devices on for debugging."""
    val = os.environ.get("LG_MESH_KEEP_POWERED", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def shutdown_node(session, strategy, place: str):
    """Power off and release a node. Skips power-off if LG_MESH_KEEP_POWERED is set."""
    if not _should_keep_powered():
        try:
            logger.info("Powering off %s", place)
            strategy.transition("off")
        except Exception:
            logger.warning("Power off failed for %s", place, exc_info=True)
    else:
        logger.info("LG_MESH_KEEP_POWERED set: leaving %s powered on (place will be released)", place)
    try:
        session.loop.run_until_complete(session.release())
        logger.info("Released %s", place)
    except Exception:
        logger.warning("Release failed for %s", place, exc_info=True)


def main():
    parser = argparse.ArgumentParser(description="Boot a single labgrid mesh node")
    parser.add_argument("--place", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--target-yaml", required=True)
    parser.add_argument("--coordinator", default="localhost:20408")
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--stop-file", required=True)
    args = parser.parse_args()

    stopping = False

    def handle_signal(signum, frame):
        nonlocal stopping
        logger.info("Received signal %d, shutting down", signum)
        stopping = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    session = None
    strategy = None

    try:
        state = boot_node(args.place, args.image, args.target_yaml, args.coordinator)
        session = state["session"]
        strategy = state["strategy"]

        _write_status(args.status_file, {
            "place": args.place,
            "ok": True,
            "ip": state.get("ip", ""),
        })
        logger.info("Status written to %s, waiting for stop signal", args.status_file)

        while not stopping:
            if Path(args.stop_file).exists():
                logger.info("Stop file detected: %s", args.stop_file)
                break
            time.sleep(STOP_POLL_INTERVAL)

    except Exception:
        logger.exception("Failed to boot %s", args.place)
        _write_status(args.status_file, {
            "place": args.place,
            "ok": False,
            "error": "boot failed",
        })
        sys.exit(1)
    finally:
        if session and strategy:
            shutdown_node(session, strategy, args.place)


if __name__ == "__main__":
    main()
