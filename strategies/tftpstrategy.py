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


class Status(enum.Enum):
    unknown = 0
    off = 1
    uboot = 2
    shell = 3


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
            self.transition(Status.off)
            self.target.activate(self.tftp)
            self.target.activate(self.console)

            staged_file = self.tftp.stage(self.target.env.config.get_image_path("root"))
            tftp_server_ip = self._resolve_tftp_server_ip()

            self.power.cycle()

            self.uboot.init_commands = (
                f"setenv bootfile {staged_file}",
            ) + self.uboot.init_commands

            if tftp_server_ip:
                tftp_dut_ip = ipaddress.ip_address(tftp_server_ip) + 1
                self.uboot.init_commands = (
                    f"setenv serverip {tftp_server_ip}",
                    f"setenv ipaddr {tftp_dut_ip}",
                ) + self.uboot.init_commands

            download_prefixes = ("tftp", "dhcp")
            original_cmds = self.uboot.init_commands
            self.uboot.init_commands = tuple(
                c for c in original_cmds
                if not c.strip().startswith(download_prefixes)
            )
            self._download_cmds = [
                c for c in original_cmds
                if c.strip().startswith(download_prefixes)
            ]

            self.target.activate(self.uboot)

        elif status == Status.shell:
            self.transition(Status.uboot)

            for cmd in self._download_cmds:
                self._download_with_retry(cmd)

            self.uboot.boot("")
            self.uboot.await_boot()
            self.target.activate(self.shell)
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
