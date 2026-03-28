"""Shared helpers for LibreMesh testing.

Functions used by both single-node (conftest.py) and multi-node
(conftest_mesh.py, mesh_boot_node.py) test paths. Centralised here to
avoid duplication and keep a single source of truth.
"""

import hashlib
import logging
import re
import time
from os import getenv
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

FIXED_IP_PREFIX = "10.13.200"

REPO_ROOT = Path(__file__).resolve().parent.parent
LABNET_PATH = REPO_ROOT / "labnet.yaml"


# ---------------------------------------------------------------------------
# Serial console helpers
# ---------------------------------------------------------------------------

def suppress_kernel_console(shell) -> None:
    """Set kernel console log level to ALERT-only (``dmesg -n 1``).

    Kernel messages (e.g. batman-adv interface activation) printed to the
    serial console interfere with pexpect prompt detection, causing TIMEOUT
    errors in ShellDriver.run().  Must be called once after the shell is
    ready and before any other serial commands.
    """
    try:
        shell.run("dmesg -n 1")
    except Exception:
        logger.debug("dmesg -n 1 failed (non-critical)")


# ---------------------------------------------------------------------------
# Fixed IP helpers
# ---------------------------------------------------------------------------

def generate_fixed_ip(place_name: str) -> str:
    """Generate a deterministic fixed IP in the 10.13.200.x range.

    The last octet is derived from a hash of the place name, ensuring each
    DUT gets a unique, repeatable address (range: .1 - .254).
    """
    md5_hash = hashlib.md5(place_name.encode()).hexdigest()
    hash_value = int(md5_hash[:8], 16) % 253 + 1
    return f"{FIXED_IP_PREFIX}.{hash_value}"


def get_ssh_target_ip(target) -> str | None:
    """Extract the IP that SSHDriver will connect to from NetworkService.

    The SSHDriver binds to a NetworkService resource whose address comes from
    the exporter config (e.g. "10.13.200.169%vlan200").  We must configure the
    DUT with that same IP so SSH can reach it.
    """
    try:
        ssh = target.get_driver("SSHDriver", activate=False)
        addr = ssh.networkservice.address
        logger.info("NetworkService address for SSH: %s", addr)
        if "%" in addr:
            addr = addr.split("%")[0]
        return addr if addr else None
    except Exception as e:
        logger.warning("Could not read NetworkService address: %s", e)
        return None


def is_qemu_target(target) -> bool:
    """Detect if the current target is a QEMU VM (not a physical DUT)."""
    try:
        ssh = target.get_driver("SSHDriver", activate=False)
        return ssh.networkservice.address == "127.0.0.1"
    except Exception:
        return False


def find_lan_interface(shell, max_wait: int = 60) -> str | None:
    """Wait for an UP LAN interface suitable for IP assignment.

    Waits up to *max_wait* seconds for br-lan (created by LibreMesh after
    wifi/batman init, can take 50+ seconds on slow devices like LibreRouter).
    Falls back to eth0 or the default-route interface if they are UP.
    """
    deadline = time.time() + max_wait
    while time.time() < deadline:
        _, _, rc = shell.run(
            "ip -o link show br-lan 2>/dev/null | grep -q 'state UP'"
        )
        if rc == 0:
            return "br-lan"

        for iface in ("eth0",):
            _, _, rc = shell.run(
                f"ip -o link show {iface} 2>/dev/null | grep -q 'state UP'"
            )
            if rc == 0:
                return iface

        stdout, _, rc = shell.run(
            "ip route show default 2>/dev/null | awk '{print $5}' | head -1"
        )
        if rc == 0 and stdout and stdout[0].strip():
            iface = stdout[0].strip()
            _, _, rc2 = shell.run(
                f"ip -o link show {iface} 2>/dev/null | grep -q ."
            )
            if rc2 == 0:
                return iface

        remaining = int(deadline - time.time())
        if remaining > 0:
            logger.info("Waiting for LAN interface to come UP (%ds remaining)", remaining)
            time.sleep(5)
    return None


def configure_fixed_ip(shell, target, place_name: str | None = None) -> str | None:
    """Configure a fixed IP on the LAN interface via serial for stable SSH.

    Reads the IP from the target's SSHDriver NetworkService binding (the
    exact address SSH will connect to).  Falls back to a hash-based IP
    derived from *place_name* (or ``LG_PLACE``) when NetworkService is
    unavailable.

    Returns the fixed IP string if configured, ``None`` otherwise.
    """
    time.sleep(5)

    fixed_ip = get_ssh_target_ip(target)

    if fixed_ip and fixed_ip in ("127.0.0.1", "::1", "localhost"):
        logger.info(
            "QEMU target detected (NetworkService address=%s); "
            "skipping fixed IP configuration (strategy handles port forwarding)",
            fixed_ip,
        )
        return None

    if not fixed_ip:
        place_name = place_name or getenv("LG_PLACE", "")
        if not place_name or place_name == "+":
            logger.warning(
                "Cannot determine SSH target IP (no NetworkService, LG_PLACE=%s)",
                place_name,
            )
            return None
        fixed_ip = generate_fixed_ip(place_name)
        logger.info("Using hash-based fallback IP %s for place %s", fixed_ip, place_name)
    logger.info("SSH target IP resolved to %s", fixed_ip)

    max_attempts = 3
    retry_delay = 15
    for attempt in range(1, max_attempts + 1):
        iface = find_lan_interface(shell)
        if not iface:
            logger.error("No suitable network interface found for fixed IP")
            return None
        logger.info("Using interface %s for fixed IP (attempt %d/%d)", iface, attempt, max_attempts)

        _, _, rc = shell.run(f"ip addr show {iface} | grep -q '{fixed_ip}'")
        if rc == 0:
            logger.info("Fixed IP %s already configured on %s", fixed_ip, iface)
            return fixed_ip

        logger.info("Configuring fixed IP %s on %s", fixed_ip, iface)
        shell.run(f"ip addr add {fixed_ip}/16 dev {iface} 2>/dev/null || true")

        stdout, stderr, rc = shell.run(f"ip addr show {iface} | grep '{fixed_ip}'")
        if rc == 0:
            logger.info("Fixed IP %s configured successfully on %s", fixed_ip, iface)
            return fixed_ip

        if attempt < max_attempts:
            err_msg = " ".join(stderr) if stderr else " ".join(stdout) if stdout else "exit %d" % rc
            logger.warning(
                "Failed to verify IP on %s (attempt %d/%d): %s; retrying in %ds",
                iface, attempt, max_attempts, err_msg, retry_delay,
            )
            time.sleep(retry_delay)
        else:
            logger.error(
                "Failed to configure fixed IP %s on %s after %d attempts",
                fixed_ip, iface, max_attempts,
            )
            return None


# ---------------------------------------------------------------------------
# labnet.yaml target resolution
# ---------------------------------------------------------------------------

def resolve_target_yaml(place_name: str, *, repo_root: Path | None = None) -> str:
    """Resolve the target YAML path for a labgrid place via labnet.yaml.

    The place name format is ``<lab>-<host>-<device_instance>``.  The function
    looks up the device instance in ``labnet.yaml`` (direct match first, then
    via the lab's ``device_instances`` mapping).

    Returns an absolute path string to the target YAML, or raises
    ``FileNotFoundError`` if no match is found.
    """
    repo_root = repo_root or REPO_ROOT
    labnet_path = repo_root / "labnet.yaml"

    parts = place_name.split("-", 2)
    if len(parts) < 3:
        raise ValueError(f"Cannot parse device instance from place: {place_name}")
    device_instance = parts[2]

    if not labnet_path.exists():
        raise FileNotFoundError(f"labnet.yaml not found at {labnet_path}")

    with open(labnet_path) as f:
        labnet = yaml.safe_load(f)

    if device_instance in labnet.get("devices", {}):
        device_config = labnet["devices"][device_instance]
        target_name = device_config.get("target_file", device_instance)
        target_file = repo_root / f"targets/{target_name}.yaml"
        if target_file.exists():
            return str(target_file)

    for lab_config in labnet.get("labs", {}).values():
        device_instances = lab_config.get("device_instances", {})
        for base_device, instances in device_instances.items():
            if device_instance in instances:
                if base_device in labnet.get("devices", {}):
                    device_config = labnet["devices"][base_device]
                    target_name = device_config.get("target_file", base_device)
                    target_file = repo_root / f"targets/{target_name}.yaml"
                    if target_file.exists():
                        return str(target_file)

    raise FileNotFoundError(
        f"No target YAML found for place {place_name} (instance: {device_instance})"
    )


# ---------------------------------------------------------------------------
# batman-adv auto-fix
# ---------------------------------------------------------------------------

def ensure_batman_mesh(target) -> bool:
    """Ensure batman-adv has at least one active mesh interface.

    If batman has no active interfaces, detect ports with carrier and
    configure batman on them.  This handles devices like BananaPi R4 where
    LibreMesh misconfigures mesh on SFP ports instead of DSA ports.

    Returns ``True`` if batman is active (either already or after fix).
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

    active_lines = [
        line for line in batctl_output.splitlines()
        if line.strip() and "active" in line.lower()
    ]
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

    dsa_conduits: set[str] = set()
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
            carrier_out = shell.run_check(
                f"cat /sys/class/net/{iface}/carrier 2>/dev/null || echo 0"
            )
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
            active_lines = [
                line for line in batctl_output.splitlines()
                if line.strip() and "active" in line.lower()
            ]
            if active_lines:
                logger.info("Batman-adv now has active interfaces: %s", active_lines)
                return True
            else:
                logger.warning("Batman still has no active interfaces after fix")
        except Exception as e:
            logger.warning("Failed to verify batman status: %s", e)

    logger.warning("Could not activate batman-adv mesh")
    return False


# ---------------------------------------------------------------------------
# Node IP query (serial console)
# ---------------------------------------------------------------------------

def query_node_ip(target, timeout: int = 120) -> str:
    """Query the booted node's br-lan IPv4 via serial console (ShellDriver).

    Uses serial instead of SSH because LibreMesh may reassign the IP
    (e.g. from 192.168.1.1 to 10.13.x.x), making the exporter's
    NetworkService address stale.  Serial is always reachable.
    """
    shell = target.get_driver("ShellDriver")
    deadline = time.time() + timeout

    time.sleep(5)

    ip_cmd = (
        "ip -4 -o addr show br-lan 2>/dev/null || "
        "ip -4 -o addr show eth0 2>/dev/null || "
        "ip -4 -o addr show dev $(ip route show default "
        "| awk '{print $5}' | head -1) 2>/dev/null"
    )

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
