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

sys.path.insert(0, str(Path(__file__).parent))

from lime_helpers import (configure_fixed_ip, ensure_batman_mesh,
                          generate_mesh_ssh_ip, query_node_ip,
                          suppress_kernel_console)

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

    logger.info("Booting %s to shell...", place)
    strategy.transition("shell")
    logger.info("Shell ready on %s", place)

    shell = target.get_driver("ShellDriver")
    suppress_kernel_console(shell)

    ensure_batman_mesh(target)
    mesh_ssh_ip = generate_mesh_ssh_ip(place)
    fixed_ip = configure_fixed_ip(
        shell,
        target,
        place_name=place,
        fixed_ip=mesh_ssh_ip,
        prefer_networkservice=False,
    )
    if fixed_ip:
        ssh_ip = fixed_ip
        logger.info("Node %s using mesh SSH IP %s", place, ssh_ip)
    else:
        ssh_ip = query_node_ip(target, prefer_mesh=True)
        logger.info("Node %s using fallback SSH IP %s", place, ssh_ip)

    mesh_ip = query_node_ip(target, prefer_mesh=True)
    if mesh_ip:
        logger.info("Node %s mesh IP %s", place, mesh_ip)
    else:
        mesh_ip = ssh_ip
        logger.info("Node %s mesh IP unavailable, falling back to %s", place, mesh_ip)

    return {
        "session": session,
        "target": target,
        "strategy": strategy,
        "ssh_ip": ssh_ip,
        "mesh_ip": mesh_ip,
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
            "ssh_ip": state.get("ssh_ip", ""),
            "mesh_ip": state.get("mesh_ip", ""),
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
