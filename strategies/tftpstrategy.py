import enum
import ipaddress
import logging
import os
import time

import attr
from labgrid.driver import TFTPProviderDriver
from labgrid.factory import target_factory
from labgrid.resource.remote import RemoteTFTPProvider
from labgrid.strategy.common import Strategy, StrategyError

logger = logging.getLogger(__name__)

TFTP_DOWNLOAD_TIMEOUT = 120
TFTP_RETRY_INTERVAL = 5
DEFAULT_UBOOT_RETRIES = 2
DEFAULT_UBOOT_RETRY_COOLDOWN = 8
DEFAULT_UBOOT_RETRY_BUDGET = 360


class Status(enum.Enum):
    unknown = 0
    off = 1
    uboot = 2
    shell = 3


def _read_int_env(name: str, default: int) -> int:
    """Return a non-negative integer from the environment."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%r, using default %d", name, raw, default)
        return default
    if value < 0:
        logger.warning("Negative integer for %s=%r, using default %d", name, raw, default)
        return default
    return value


def _retry_action(action, cleanup, retries: int, cooldown: int, label: str,
                  sleep_fn=time.sleep):
    """Run an action with bounded retries and best-effort cleanup between tries."""
    max_attempts = max(1, retries + 1)
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            return action()
        except Exception as exc:
            last_error = exc
            if attempt >= max_attempts:
                raise
            logger.warning(
                "%s failed on attempt %d/%d (%s), retrying in %ds",
                label, attempt, max_attempts, type(exc).__name__, cooldown,
            )
            try:
                cleanup()
            except Exception:
                logger.warning("%s cleanup failed before retry", label, exc_info=True)
            sleep_fn(cooldown)

    raise last_error


@target_factory.reg_driver
@attr.s(eq=False)
class UBootTFTPStrategy(Strategy):
    """Strategy that boots via U-Boot TFTP with retry logic for STP delay.

    After a PoE power cycle the switch port may need 30-60s to reach
    forwarding state (STP convergence, link negotiation).  Download
    commands (tftp/dhcp) extracted from init_commands are retried with
    a 60s per-attempt timeout until TFTP_DOWNLOAD_TIMEOUT expires.

    The TFTP server IP can be overridden via TFTP_SERVER_IP env var.
    This is used by mesh tests where conftest_vlan moves DUTs to VLAN
    200 before boot, making the exporter's isolated-VLAN external_ip
    unreachable.
    """

    bindings = {
        "power": "PowerProtocol",
        "console": "ConsoleProtocol",
        "uboot": "LinuxBootProtocol",
        "shell": "ShellDriver",
        "tftp": "TFTPProviderDriver",
    }
    tftp: TFTPProviderDriver

    status = attr.ib(default=Status.unknown)

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        self._download_cmds = []
        self._base_init_commands = tuple(self.uboot.init_commands)

    def _download_with_retry(self, download_cmd):
        """Run a U-Boot download command with retries for link-up delay."""
        deadline = time.monotonic() + TFTP_DOWNLOAD_TIMEOUT
        attempt = 0

        while True:
            attempt += 1
            remaining = deadline - time.monotonic()
            logger.info("TFTP download attempt %d, cmd='%s', %.0fs remaining",
                        attempt, download_cmd, remaining)
            try:
                self.uboot.run_check(download_cmd, timeout=60)
                logger.info("TFTP download succeeded on attempt %d", attempt)
                return
            except Exception as e:
                remaining = deadline - time.monotonic()
                if remaining <= TFTP_RETRY_INTERVAL:
                    raise StrategyError(
                        f"TFTP download failed after {attempt} attempts "
                        f"({TFTP_DOWNLOAD_TIMEOUT}s): {e}"
                    ) from e
                logger.warning(
                    "TFTP attempt %d failed (%s), retrying in %ds (%.0fs left)",
                    attempt, type(e).__name__, TFTP_RETRY_INTERVAL, remaining,
                )
                time.sleep(TFTP_RETRY_INTERVAL)

    def _resolve_tftp_server_ip(self):
        """Return the TFTP server IP, preferring env override over exporter config."""
        override = os.environ.get("TFTP_SERVER_IP", "").strip()
        if override:
            logger.info("Using TFTP_SERVER_IP override: %s", override)
            return override
        exporter_ip = self.target.get_resource(
            RemoteTFTPProvider, wait_avail=False
        ).external_ip
        logger.info("Using exporter external_ip: %s", exporter_ip)
        return exporter_ip

    def _prepare_uboot_commands(self, staged_file, tftp_server_ip):
        """Prepare a fresh set of U-Boot init commands for this boot attempt."""
        init_commands = (
            f"setenv bootfile {staged_file}",
        ) + self._base_init_commands

        if tftp_server_ip:
            tftp_dut_ip = ipaddress.ip_address(tftp_server_ip) + 1
            init_commands = (
                f"setenv serverip {tftp_server_ip}",
                f"setenv ipaddr {tftp_dut_ip}",
            ) + init_commands

        download_prefixes = ("tftp", "dhcp")
        self.uboot.init_commands = tuple(
            c for c in init_commands
            if not c.strip().startswith(download_prefixes)
        )
        self._download_cmds = [
            c for c in init_commands
            if c.strip().startswith(download_prefixes)
        ]

    def _effective_uboot_retries(self) -> int:
        """Cap configured U-Boot retries so one child cannot exhaust the boot budget."""
        configured = _read_int_env("LG_MESH_UBOOT_RETRIES", DEFAULT_UBOOT_RETRIES)
        login_timeout = int(getattr(self.uboot, "login_timeout", 60) or 60)
        login_timeout = max(1, login_timeout)
        max_attempts = max(1, DEFAULT_UBOOT_RETRY_BUDGET // login_timeout)
        effective = min(configured, max_attempts - 1)
        if effective != configured:
            logger.info(
                "Capping U-Boot retries from %d to %d for login_timeout=%ds "
                "(budget=%ds)",
                configured, effective, login_timeout, DEFAULT_UBOOT_RETRY_BUDGET,
            )
        return effective

    def _transition_to_uboot_once(self):
        """Power-cycle the node and activate the U-Boot console once."""
        self.transition(Status.off)
        self.target.activate(self.tftp)
        self.target.activate(self.console)

        staged_file = self.tftp.stage(self.target.env.config.get_image_path("root"))
        tftp_server_ip = self._resolve_tftp_server_ip()

        self.power.cycle()
        self._prepare_uboot_commands(staged_file, tftp_server_ip)
        self.target.activate(self.uboot)
        self.status = Status.uboot

    def transition_to_uboot_with_retry(self):
        """Reach the U-Boot prompt with bounded retries."""
        retries = self._effective_uboot_retries()
        cooldown = _read_int_env(
            "LG_MESH_UBOOT_RETRY_COOLDOWN", DEFAULT_UBOOT_RETRY_COOLDOWN
        )
        _retry_action(
            self._transition_to_uboot_once,
            lambda: self.transition(Status.off),
            retries=retries,
            cooldown=cooldown,
            label="U-Boot activation",
        )

    def run_download_commands(self):
        """Execute prepared TFTP/DHCP download commands."""
        if self.status != Status.uboot:
            self.transition_to_uboot_with_retry()
        for cmd in self._download_cmds:
            self._download_with_retry(cmd)

    def boot_kernel(self):
        """Issue the boot command and wait for the kernel handoff."""
        self.uboot.boot("")
        self.uboot.await_boot()

    def activate_shell(self):
        """Activate the shell driver after the kernel has booted."""
        self.target.activate(self.shell)
        self.status = Status.shell

    def transition(self, status):
        if not isinstance(status, Status):
            status = Status[status]
        if status == Status.unknown:
            raise StrategyError(f"can not transition to {status}")
        elif status == self.status:
            return
        elif status == Status.off:
            self.target.deactivate(self.console)
            self.target.activate(self.power)
            self.power.off()
        elif status == Status.uboot:
            self.transition_to_uboot_with_retry()
        elif status == Status.shell:
            self.transition(Status.uboot)
            self.run_download_commands()
            self.boot_kernel()
            self.activate_shell()
        else:
            raise StrategyError(f"no transition found from {self.status} to {status}")
        self.status = status

    def force(self, status):
        if not isinstance(status, Status):
            status = Status[status]
        if status == Status.off:
            self.target.activate(self.power)
        elif status == Status.uboot:
            self.target.activate(self.uboot)
        elif status == Status.shell:
            self.target.activate(self.shell)
        else:
            raise StrategyError("can not force state {}".format(status))
        self.status = status
