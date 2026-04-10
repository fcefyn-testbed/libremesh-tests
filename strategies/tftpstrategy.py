import enum
import ipaddress
import logging
import time

import attr
from labgrid.driver import TFTPProviderDriver
from labgrid.factory import target_factory
from labgrid.resource.remote import RemoteTFTPProvider
from labgrid.strategy.common import Strategy, StrategyError

logger = logging.getLogger(__name__)

TFTP_DOWNLOAD_TIMEOUT = 120
TFTP_RETRY_INTERVAL = 10


class Status(enum.Enum):
    unknown = 0
    off = 1
    uboot = 2
    shell = 3


@target_factory.reg_driver
@attr.s(eq=False)
class UBootTFTPStrategy(Strategy):
    """Strategy that boots via U-Boot TFTP with retry logic.

    After a PoE power cycle the switch port may need 30-60s to reach
    forwarding state (STP, link negotiation).  The strategy retries the
    TFTP download (with a 60s per-attempt timeout) until the overall
    TFTP_DOWNLOAD_TIMEOUT expires, so transient link-down is tolerated.
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

    def _download_with_retry(self, download_cmd):
        """Run a U-Boot download command with retries for link-up delay."""
        deadline = time.monotonic() + TFTP_DOWNLOAD_TIMEOUT
        attempt = 0

        while True:
            attempt += 1
            # #region agent log
            logger.info("[DBG-913bc0-H4] attempt=%d, cmd=%s, remaining=%.0fs", attempt, download_cmd, deadline - time.monotonic())
            # #endregion
            try:
                self.uboot.run_check(download_cmd, timeout=60)
                logger.info("[DBG-913bc0] TFTP download succeeded on attempt %d", attempt)
                return
            except Exception as e:
                remaining = deadline - time.monotonic()
                # #region agent log
                logger.warning("[DBG-913bc0-H4] attempt %d FAILED, exception_type=%s, remaining=%.0fs, error=%s", attempt, type(e).__name__, remaining, e)
                # #endregion
                if remaining <= TFTP_RETRY_INTERVAL:
                    raise StrategyError(
                        f"TFTP download failed after {attempt} attempts "
                        f"({TFTP_DOWNLOAD_TIMEOUT}s): {e}"
                    ) from e
                logger.warning(
                    "TFTP attempt %d failed, retrying in %ds (%.0fs left): %s",
                    attempt, TFTP_RETRY_INTERVAL, remaining, e,
                )
                time.sleep(TFTP_RETRY_INTERVAL)

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
            tftp_server_ip = self.target.get_resource(
                RemoteTFTPProvider, wait_avail=False
            ).external_ip

            self.power.cycle()

            self.uboot.init_commands = (
                f"setenv bootfile {staged_file}",
            ) + self.uboot.init_commands

            # #region agent log
            logger.info("[DBG-913bc0-H6] tftp_server_ip from exporter=%s", tftp_server_ip)
            # #endregion
            if tftp_server_ip:
                tftp_dut_ip = ipaddress.ip_address(tftp_server_ip) + 1
                # #region agent log
                logger.info("[DBG-913bc0-H6] setting serverip=%s, ipaddr=%s", tftp_server_ip, tftp_dut_ip)
                # #endregion
                self.uboot.init_commands = (
                    f"setenv serverip {tftp_server_ip}",
                    f"setenv ipaddr {tftp_dut_ip}",
                ) + self.uboot.init_commands

            # #region agent log
            logger.info("[DBG-913bc0-H1H2] strategy version=RETRY_V2, file=%s", __file__)
            # #endregion
            download_prefixes = ("tftp", "dhcp")
            original_cmds = self.uboot.init_commands
            # #region agent log
            logger.info("[DBG-913bc0-H2] original init_commands=%s", list(original_cmds))
            # #endregion
            self.uboot.init_commands = tuple(
                c for c in original_cmds
                if not c.strip().startswith(download_prefixes)
            )
            self._download_cmds = [
                c for c in original_cmds
                if c.strip().startswith(download_prefixes)
            ]
            # #region agent log
            logger.info("[DBG-913bc0-H2] filtered init_commands=%s", list(self.uboot.init_commands))
            logger.info("[DBG-913bc0-H2] download_cmds=%s", self._download_cmds)
            # #endregion

            self.target.activate(self.uboot)

        elif status == Status.shell:
            self.transition(Status.uboot)

            # #region agent log
            logger.info("[DBG-913bc0-H2] entering shell transition, download_cmds=%s", self._download_cmds)
            # #endregion
            for cmd in self._download_cmds:
                # #region agent log
                logger.info("[DBG-913bc0-H4] starting _download_with_retry cmd=%s", cmd)
                # #endregion
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
