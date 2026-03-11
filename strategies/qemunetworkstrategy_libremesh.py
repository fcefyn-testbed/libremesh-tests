"""QEMU strategy for LibreMesh images.

Extends QEMUNetworkStrategy to handle LibreMesh's dynamic IP assignment.
LibreMesh replaces OpenWrt's default 192.168.1.1 with a 10.13.x.x address
derived from the device's MAC and adds an "anygw" address (10.13.0.1) on
br-lan.  The SLIRP LAN netdev must use net=10.13.0.0/16 so that the guest
IP falls within the virtual network, allowing port forwarding to work.

SSH port forwarding targets the anygw address (10.13.0.1:22) which is
always present once lime-config has finished.
"""

import enum
import logging
import time

import attr
from labgrid import step, target_factory
from labgrid.strategy import Strategy, StrategyError
from labgrid.util import get_free_port

logger = logging.getLogger(__name__)

ANYGW_ADDRESS = "10.13.0.1"
DROPBEAR_TIMEOUT = 120
DROPBEAR_POLL_INTERVAL = 5


class Status(enum.Enum):
    unknown = 0
    off = 1
    shell = 2


@target_factory.reg_driver
@attr.s(eq=False)
class QEMUNetworkStrategyLibreMesh(Strategy):
    bindings = {
        "qemu": "QEMUDriver",
        "shell": "ShellDriver",
        "ssh": "SSHDriver",
    }

    status = attr.ib(default=Status.unknown)

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        self.__port_forward = None
        self.__remote_port = self.ssh.networkservice.port

    @step(result=True)
    def get_remote_address(self):
        return str(self.shell.get_ip_addresses()[0].ip)

    def _wait_for_dropbear(self):
        """Poll until dropbear is listening on port 22.

        LibreMesh runs lime-config on first boot which takes ~30s to assign
        IPs and generate SSH keys.  We wait for dropbear to be ready so that
        the subsequent port-forward actually reaches a live SSH daemon.
        """
        deadline = time.time() + DROPBEAR_TIMEOUT
        while time.time() < deadline:
            try:
                lines = self.shell.run_check(
                    "netstat -tlnp 2>/dev/null | grep ':22 ' || true"
                )
                for line in lines:
                    if ":22 " in line:
                        logger.info("Dropbear ready: %s", line.strip())
                        return
            except Exception:
                pass
            remaining = int(deadline - time.time())
            logger.info(
                "Waiting for dropbear to start (%ds remaining)...", remaining
            )
            time.sleep(DROPBEAR_POLL_INTERVAL)
        raise StrategyError(
            f"Dropbear not listening after {DROPBEAR_TIMEOUT}s"
        )

    @step()
    def update_network_service(self):
        self._wait_for_dropbear()

        networkservice = self.ssh.networkservice

        if networkservice.address != ANYGW_ADDRESS:
            self.target.deactivate(self.ssh)

            if "user" in self.qemu.nic.split(","):
                if self.__port_forward is not None:
                    self.qemu.remove_port_forward(*self.__port_forward)

                local_port = get_free_port()
                local_address = "127.0.0.1"

                self.qemu.add_port_forward(
                    "tcp",
                    local_address,
                    local_port,
                    ANYGW_ADDRESS,
                    self.__remote_port,
                    "mesh0",
                )
                self.__port_forward = ("tcp", local_address, local_port)

                networkservice.address = local_address
                networkservice.port = local_port
            else:
                networkservice.address = ANYGW_ADDRESS
                networkservice.port = self.__remote_port

    @step(args=["state"])
    def transition(self, state, *, step):
        if not isinstance(state, Status):
            state = Status[state]

        if state == Status.unknown:
            raise StrategyError(f"can not transition to {state}")

        elif self.status == state:
            step.skip("nothing to do")
            return

        if state == Status.off:
            self.target.activate(self.qemu)
            self.qemu.off()

        elif state == Status.shell:
            self.target.activate(self.qemu)
            self.qemu.on()
            self.target.activate(self.shell)
            self.update_network_service()

        self.status = state
