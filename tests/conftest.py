# Copyright 2023 by Garmin Ltd. or its subsidiaries
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import os
import sys
from os import getenv
from pathlib import Path

import pytest

logger = logging.getLogger(__name__)

# Base network for fixed test IPs. The last octet is derived from a hash
# of the place name, ensuring each DUT gets a unique, deterministic IP.
# Range: 10.13.200.1 - 10.13.200.254
FIXED_IP_PREFIX = "10.13.200"


def _get_ssh_target_ip(target) -> str | None:
    """Extract the IP that the SSHDriver will connect to from the target's NetworkService.

    The SSHDriver binds to a NetworkService resource whose address comes from
    the exporter config (e.g. "10.13.200.169%vlan200"). We must configure the
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


def _generate_fixed_ip(place_name: str) -> str:
    """Generate a deterministic fixed IP for a place name (hash fallback)."""
    import hashlib
    md5_hash = hashlib.md5(place_name.encode()).hexdigest()
    hash_value = int(md5_hash[:8], 16) % 253 + 1
    return f"{FIXED_IP_PREFIX}.{hash_value}"


def _resolve_target_from_place():
    lg_env = getenv("LG_ENV")
    lg_place = getenv("LG_PLACE")

    if lg_env or not lg_place:
        return lg_env

    parts = lg_place.split("-", 2)
    if len(parts) < 3:
        return None

    device_instance = parts[2]

    try:
        repo_root = Path(__file__).parent.parent
        labnet_path = repo_root / "labnet.yaml"

        if not labnet_path.exists():
            return None

        import yaml

        with open(labnet_path, "r") as f:
            labnet = yaml.safe_load(f)

        if device_instance in labnet.get("devices", {}):
            device_config = labnet["devices"][device_instance]
            target_name = device_config.get("target_file", device_instance)
            target_file = f"targets/{target_name}.yaml"
            if (repo_root / target_file).exists():
                return str(repo_root / target_file)

        for lab_name, lab_config in labnet.get("labs", {}).items():
            device_instances = lab_config.get("device_instances", {})
            for base_device, instances in device_instances.items():
                if device_instance in instances:
                    if base_device in labnet.get("devices", {}):
                        device_config = labnet["devices"][base_device]
                        target_name = device_config.get("target_file", base_device)
                        target_file = f"targets/{target_name}.yaml"
                        if (repo_root / target_file).exists():
                            return str(repo_root / target_file)

    except Exception:
        pass

    return None


def pytest_configure(config):
    config._metadata = getattr(config, "_metadata", {})
    config._metadata["version"] = "12.3.4"
    config._metadata["environment"] = "staging"

    config.addinivalue_line("markers", "mesh: multi-node LibreMesh mesh tests")

    resolved_env = _resolve_target_from_place()
    if resolved_env:
        os.environ["LG_ENV"] = resolved_env


device = getenv("LG_ENV", "Unknown").split("/")[-1].split(".")[0]


def pytest_addoption(parser):
    parser.addoption("--firmware", action="store", default="firmware.bin")


def ubus_call(command, namespace, method, params={}):
    output = command.run_check(f"ubus call {namespace} {method} '{json.dumps(params)}'")

    try:
        return json.loads("\n".join(output))
    except json.JSONDecodeError:
        return {}


@pytest.fixture(scope="session", autouse=True)
def setup_env(pytestconfig):
    try:
        from labgrid.pytestplugin.hooks import LABGRID_ENV_KEY
        env = pytestconfig.stash.get(LABGRID_ENV_KEY, None)
    except (ImportError, KeyError):
        env = None
    if env is not None:
        env.config.data.setdefault("images", {})["firmware"] = pytestconfig.getoption(
            "firmware"
        )


@pytest.fixture
def shell_command(strategy):
    try:
        strategy.transition("shell")
        return strategy.shell
    except Exception:
        logger.exception("Failed to transition to state shell")
        pytest.exit("Failed to transition to state shell", returncode=3)


def _find_lan_interface(shell_command) -> str | None:
    """Detect the LAN bridge or fallback interface for IP assignment.

    Tries br-lan first (standard OpenWrt/LibreMesh), then eth0, then the
    default route interface. Some devices (e.g. LibreRouter) don't create
    br-lan immediately or use a different bridge name.
    """
    import time
    for attempt in range(3):
        for iface in ("br-lan", "eth0"):
            _, _, rc = shell_command.run(f"ip link show {iface} 2>/dev/null")
            if rc == 0:
                return iface
        stdout, _, rc = shell_command.run(
            "ip route show default 2>/dev/null | awk '{print $5}' | head -1"
        )
        if rc == 0 and stdout and stdout[0].strip():
            return stdout[0].strip()
        if attempt < 2:
            logger.info("No LAN interface found yet, waiting for network init (attempt %d/3)", attempt + 1)
            time.sleep(5)
    return None


def _configure_fixed_ip(shell_command, target) -> str | None:
    """Configure a fixed IP on the LAN interface via serial for stable SSH access.

    Reads the IP from the target's SSHDriver NetworkService binding, which
    is the exact address SSH will connect to. This ensures the DUT has the
    IP that labgrid expects, regardless of LG_PLACE expansion.

    Falls back to a hash-based IP from LG_PLACE if NetworkService is unavailable.

    Returns the fixed IP if configured, None otherwise.
    """
    import time

    time.sleep(5)

    fixed_ip = _get_ssh_target_ip(target)
    if not fixed_ip:
        place_name = getenv("LG_PLACE", "")
        if not place_name or place_name == "+":
            logger.warning("Cannot determine SSH target IP (no NetworkService, LG_PLACE=%s)", place_name)
            return None
        fixed_ip = _generate_fixed_ip(place_name)
        logger.info("Using hash-based fallback IP %s for place %s", fixed_ip, place_name)
    logger.info("SSH target IP resolved to %s", fixed_ip)

    iface = _find_lan_interface(shell_command)
    if not iface:
        logger.error("No suitable network interface found for fixed IP")
        return None
    logger.info("Using interface %s for fixed IP", iface)

    _, _, rc = shell_command.run(f"ip addr show {iface} | grep -q '{fixed_ip}'")
    if rc == 0:
        logger.info("Fixed IP %s already configured on %s", fixed_ip, iface)
        return fixed_ip

    logger.info("Configuring fixed IP %s on %s", fixed_ip, iface)
    shell_command.run(f"ip addr add {fixed_ip}/16 dev {iface} 2>/dev/null || true")

    _, _, rc = shell_command.run(f"ip addr show {iface} | grep '{fixed_ip}'")
    if rc == 0:
        logger.info("Fixed IP %s configured successfully on %s", fixed_ip, iface)
        return fixed_ip

    logger.error("Failed to configure fixed IP %s on %s", fixed_ip, iface)
    return None


@pytest.fixture
def ssh_command(shell_command, target):
    fixed_ip = _configure_fixed_ip(shell_command, target)
    if fixed_ip:
        logger.info("Using fixed IP %s for SSH", fixed_ip)

    ssh = target.get_driver("SSHDriver")
    return ssh


sys.path.insert(0, str(Path(__file__).parent))
from conftest_mesh import mesh_nodes  # noqa: F811
