#!/usr/bin/env python3
"""Boot a single labgrid node and hold the session until signaled to stop.

Launched as a subprocess by conftest_multinode.py. Each node runs in its
own process to avoid labgrid's internal thread-safety issues with
concurrent sessions (StepLogger, coordinator multi-session crashes).

After booting to shell, assigns a unique test IP (10.200.0.<node_index+1>/24)
on br-lan via the serial console, so multiple nodes on the same VLAN don't
conflict (vanilla OpenWrt defaults all to 192.168.1.1).

Usage:
    python multinode_boot.py \
        --place labgrid-mylab-openwrt_one \
        --image /srv/tftp/firmwares/openwrt_one/openwrt-...-initramfs.itb \
        --target-yaml targets/openwrt_one.yaml \
        --node-index 0 \
        --status-file /tmp/node_status.json \
        --stop-file /tmp/node_stop

Set LG_MULTI_KEEP_POWERED=1 to leave the node powered on when stopping
(useful for post-test SSH/serial debugging).
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
logger = logging.getLogger("multinode_boot")

STOP_POLL_INTERVAL = 2
TEST_SUBNET = "10.200.0"
TEST_NETMASK = 24


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


def _assign_test_ip(shell, node_index: int) -> str:
    """Assign a unique test IP on br-lan via serial console.

    Uses a dedicated subnet (10.200.0.0/24) to avoid conflicts with
    OpenWrt's default 192.168.1.1. The IP is added as a secondary address
    so the default config stays intact.
    """
    test_ip = f"{TEST_SUBNET}.{node_index + 1}"
    iface = "br-lan"

    try:
        shell.run(f"ip addr add {test_ip}/{TEST_NETMASK} dev {iface} 2>/dev/null || true")
        shell.run(f"ip link set {iface} up")
        logger.info("Assigned test IP %s/%d on %s", test_ip, TEST_NETMASK, iface)
    except Exception:
        logger.warning("Failed to assign test IP on %s, trying eth0", iface)
        iface = "eth0"
        try:
            shell.run(f"ip addr add {test_ip}/{TEST_NETMASK} dev {iface} 2>/dev/null || true")
            shell.run(f"ip link set {iface} up")
            logger.info("Assigned test IP %s/%d on %s (fallback)", test_ip, TEST_NETMASK, iface)
        except Exception:
            logger.error("Could not assign test IP on any interface")
            return ""

    return test_ip


def boot_node(place: str, image: str, target_yaml: str,
              coordinator: str, node_index: int = 0) -> dict:
    """Boot a node via labgrid and return session state."""
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

    logger.info("Booting %s to shell...", place)
    strategy.transition("shell")
    logger.info("Shell ready on %s", place)

    shell = target.get_driver("ShellDriver")
    try:
        shell.run("dmesg -n 1")
    except Exception:
        pass

    node_ip = _assign_test_ip(shell, node_index)
    logger.info("Node %s has test IP %s", place, node_ip or "(none)")

    return {
        "session": session,
        "target": target,
        "strategy": strategy,
        "ip": node_ip,
    }


def _should_keep_powered() -> bool:
    val = os.environ.get("LG_MULTI_KEEP_POWERED", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def shutdown_node(session, strategy, place: str):
    """Power off and release a node."""
    if not _should_keep_powered():
        try:
            logger.info("Powering off %s", place)
            strategy.transition("off")
        except Exception:
            logger.warning("Power off failed for %s", place, exc_info=True)
    else:
        logger.info("LG_MULTI_KEEP_POWERED: leaving %s powered on", place)
    try:
        session.loop.run_until_complete(session.release())
        logger.info("Released %s", place)
    except Exception:
        logger.warning("Release failed for %s", place, exc_info=True)


def main():
    parser = argparse.ArgumentParser(description="Boot a single labgrid node")
    parser.add_argument("--place", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--target-yaml", required=True)
    parser.add_argument("--coordinator", default="localhost:20408")
    parser.add_argument("--node-index", type=int, default=0)
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
        state = boot_node(args.place, args.image, args.target_yaml,
                          args.coordinator, args.node_index)
        session = state["session"]
        strategy = state["strategy"]

        _write_status(args.status_file, {
            "place": args.place,
            "ok": True,
            "ip": state.get("ip", ""),
        })
        logger.info("Status written, waiting for stop signal at %s", args.stop_file)

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
