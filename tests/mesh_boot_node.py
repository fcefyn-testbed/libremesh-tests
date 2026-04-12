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

from lime_helpers import (
    _read_int_env,
    configure_fixed_ip,
    ensure_batman_mesh,
    generate_mesh_ssh_ip,
    query_node_ip,
    suppress_kernel_console,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("mesh_boot_node")

STOP_POLL_INTERVAL = 2
DEFAULT_BOOT_ATTEMPTS = 1
DEFAULT_RETRY_COOLDOWN = 8
DEFAULT_COORDINATOR = "localhost:20408"
EXCEPTION_SUMMARY_MAX_LEN = 240
STOP_REQUESTED = False


class BootFailure(RuntimeError):
    """Structured boot failure that can be reported back to the parent fixture."""

    def __init__(
        self,
        stage: str,
        error_type: str,
        summary: str,
        retriable: bool = True,
        attempts_used: int = 1,
    ):
        super().__init__(summary)
        self.stage = stage
        self.error_type = error_type
        self.summary = summary
        self.retriable = retriable
        self.attempts_used = attempts_used

    def to_status(self, place: str) -> dict:
        return {
            "place": place,
            "ok": False,
            "error": self.summary,
            "failure_stage": self.stage,
            "attempts_used": self.attempts_used,
            "error_type": self.error_type,
            "error_summary": self.summary,
        }


def _boot_attempt_limit() -> int:
    return max(1, _read_int_env("LG_MESH_BOOT_ATTEMPTS", DEFAULT_BOOT_ATTEMPTS))


def _boot_retry_cooldown() -> int:
    for env_name in ("LG_MESH_BOOT_RETRY_COOLDOWN", "LG_MESH_UBOOT_RETRY_COOLDOWN"):
        if os.environ.get(env_name, "").strip():
            return _read_int_env(env_name, DEFAULT_RETRY_COOLDOWN)
    return DEFAULT_RETRY_COOLDOWN


def _check_stop_requested():
    """Abort promptly when the parent or operator requested shutdown."""
    if STOP_REQUESTED:
        raise BootFailure(
            stage="shutdown",
            error_type="shutdown_requested",
            summary="Shutdown requested while booting",
            retriable=False,
        )


def _summarize_exception(exc: Exception, limit: int = EXCEPTION_SUMMARY_MAX_LEN) -> str:
    text = str(exc).strip()
    if not text:
        text = type(exc).__name__
    summary = f"{type(exc).__name__}: {text}"
    if len(summary) > limit:
        return summary[: limit - 3] + "..."
    return summary


def _error_type_for_stage(stage: str, exc: Exception) -> str:
    text = str(exc).lower()
    exc_name = type(exc).__name__.lower()
    if stage == "acquire_place":
        return "place_acquire_failure"
    if stage == "transition_to_uboot":
        if "waiting for login" in text or "timeout" in exc_name:
            return "uboot_prompt_timeout"
        return "uboot_activation_failure"
    if stage == "tftp_download":
        return "tftp_download_failure"
    if stage == "boot_kernel":
        return "kernel_boot_failure"
    if stage == "activate_shell":
        if "timeout" in exc_name:
            return "shell_activation_timeout"
        return "shell_activation_failure"
    if stage == "post_shell":
        return "post_shell_setup_failure"
    return "boot_failure"


def _raise_stage_failure(stage: str, exc: Exception, retriable: bool) -> None:
    raise BootFailure(
        stage=stage,
        error_type=_error_type_for_stage(stage, exc),
        summary=_summarize_exception(exc),
        retriable=retriable,
    ) from exc


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


def _boot_node_once(place: str, target, strategy) -> dict:
    """Execute one staged boot attempt after the place has been acquired."""
    _check_stop_requested()
    logger.info("Booting %s to shell...", place)

    try:
        strategy.transition_to_uboot_with_retry()
    except Exception as exc:
        _raise_stage_failure("transition_to_uboot", exc, retriable=True)

    try:
        strategy.run_download_commands()
    except Exception as exc:
        _raise_stage_failure("tftp_download", exc, retriable=False)

    try:
        strategy.boot_kernel()
    except Exception as exc:
        _raise_stage_failure("boot_kernel", exc, retriable=True)

    try:
        strategy.activate_shell()
    except Exception as exc:
        _raise_stage_failure("activate_shell", exc, retriable=True)

    logger.info("Shell ready on %s", place)
    shell = target.get_driver("ShellDriver")

    try:
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
            logger.info(
                "Node %s mesh IP unavailable, falling back to %s", place, mesh_ip
            )
    except Exception as exc:
        _raise_stage_failure("post_shell", exc, retriable=False)

    return {
        "ssh_ip": ssh_ip,
        "mesh_ip": mesh_ip,
    }


def _reset_after_failed_attempt(strategy, place: str):
    """Best-effort reset between boot attempts."""
    try:
        logger.info("Resetting %s to off before retry", place)
        strategy.transition("off")
    except Exception:
        logger.warning("Failed to reset %s to off before retry", place, exc_info=True)


def _run_boot_attempts(
    place: str, target, strategy, boot_attempts: int, retry_cooldown: int
) -> dict:
    """Run staged boot attempts until one succeeds or the retry budget is exhausted."""
    last_failure = None

    for attempt in range(1, boot_attempts + 1):
        _check_stop_requested()
        logger.info("Boot attempt %d/%d for %s", attempt, boot_attempts, place)
        try:
            result = _boot_node_once(place, target, strategy)
            result["attempts_used"] = attempt
            return result
        except BootFailure as exc:
            exc.attempts_used = attempt
            last_failure = exc
            logger.warning(
                "Boot attempt %d/%d for %s failed at %s (%s): %s",
                attempt,
                boot_attempts,
                place,
                exc.stage,
                exc.error_type,
                exc.summary,
            )
            if not exc.retriable or attempt >= boot_attempts:
                raise
            _reset_after_failed_attempt(strategy, place)
            logger.info("Retrying boot of %s in %ds", place, retry_cooldown)
            for _ in range(retry_cooldown):
                _check_stop_requested()
                time.sleep(1)

    if last_failure:
        raise last_failure
    raise BootFailure(
        stage="boot_attempt",
        error_type="boot_failure",
        summary=f"Unknown boot failure for {place}",
        retriable=False,
        attempts_used=boot_attempts,
    )


def boot_node(place: str, image: str, target_yaml: str, coordinator: str) -> dict:
    """Boot a node via labgrid and return session state needed by the parent."""
    _patch_reg_driver_idempotent()

    from labgrid import Environment
    from labgrid.remote.client import start_session

    os.environ["LG_PLACE"] = place
    os.environ["LG_IMAGE"] = image

    try:
        env = Environment(config_file=target_yaml)

        args = argparse.Namespace(
            place=place,
            state=None,
            initial_state=None,
            allow_unmatched=False,
        )
        session = start_session(
            coordinator, extra={"env": env, "role": "main", "args": args}
        )
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
    except Exception as exc:
        _raise_stage_failure("acquire_place", exc, retriable=False)

    boot_attempts = _boot_attempt_limit()
    retry_cooldown = _boot_retry_cooldown()
    result = _run_boot_attempts(place, target, strategy, boot_attempts, retry_cooldown)
    result.update(
        {
            "session": session,
            "target": target,
            "strategy": strategy,
        }
    )
    return result


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
        logger.info(
            "LG_MESH_KEEP_POWERED set: leaving %s powered on (place will be released)",
            place,
        )
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
    parser.add_argument("--coordinator", default=DEFAULT_COORDINATOR)
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--stop-file", required=True)
    args = parser.parse_args()

    stopping = False
    received_signal = None

    def handle_signal(signum, _frame):
        global STOP_REQUESTED
        nonlocal stopping, received_signal
        STOP_REQUESTED = True
        stopping = True
        received_signal = signum

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    session = None
    strategy = None

    try:
        state = boot_node(args.place, args.image, args.target_yaml, args.coordinator)
        session = state["session"]
        strategy = state["strategy"]

        _write_status(
            args.status_file,
            {
                "place": args.place,
                "ok": True,
                "ssh_ip": state.get("ssh_ip", ""),
                "mesh_ip": state.get("mesh_ip", ""),
                "attempts_used": state.get("attempts_used", 1),
            },
        )
        logger.info("Status written to %s, waiting for stop signal", args.status_file)

        while not stopping:
            if Path(args.stop_file).exists():
                logger.info("Stop file detected: %s", args.stop_file)
                break
            time.sleep(STOP_POLL_INTERVAL)

        if received_signal is not None:
            logger.info("Shutting down after signal %d", received_signal)

    except BootFailure as exc:
        logger.exception("Failed to boot %s", args.place)
        _write_status(args.status_file, exc.to_status(args.place))
        sys.exit(1)
    except Exception as exc:
        logger.exception("Failed to boot %s", args.place)
        failure = BootFailure(
            stage="boot_attempt",
            error_type=_error_type_for_stage("boot_attempt", exc),
            summary=_summarize_exception(exc),
            retriable=False,
        )
        _write_status(args.status_file, failure.to_status(args.place))
        sys.exit(1)
    finally:
        if session and strategy:
            shutdown_node(session, strategy, args.place)


if __name__ == "__main__":
    main()
