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


def _generate_fixed_ip(place_name: str) -> str:
    """Generate a deterministic fixed IP for a place name.

    Uses MD5 hash of the place name to derive the last octet, ensuring:
    - Same place always gets the same IP (deterministic across runs)
    - Different places get different IPs (with high probability)
    - No hardcoding of specific devices

    Note: We use hashlib.md5 instead of hash() because Python's hash()
    is randomized by default (PYTHONHASHSEED) and not consistent across runs.

    Args:
        place_name: The labgrid place name (e.g., "labgrid-fcefyn-openwrt_one")

    Returns:
        IP address string (e.g., "10.13.200.42")
    """
    import hashlib
    # Use MD5 hash (deterministic) of place name, modulo 253 to get range 1-254
    # (avoiding .0 network and .255 broadcast)
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


def _configure_fixed_ip(shell_command, place_name: str) -> str | None:
    """Configure a fixed IP on br-lan via serial for stable SSH access.

    This ensures SSH connectivity works regardless of what IP LibreMesh
    auto-assigns. The fixed IP is added as a secondary address, preserving
    the original LibreMesh IP for tests that need to validate it.

    The IP is generated deterministically from the place name, so no
    hardcoding of specific devices is needed.

    Returns the fixed IP if configured, None otherwise.
    """
    if not place_name:
        logger.warning("No place name provided, cannot configure fixed IP")
        return None

    fixed_ip = _generate_fixed_ip(place_name)

    # Check if the fixed IP is already configured
    stdout, _, rc = shell_command.run(f"ip addr show br-lan | grep -q '{fixed_ip}'")
    if rc == 0:
        logger.info("Fixed IP %s already configured on %s", fixed_ip, place_name)
        return fixed_ip

    # Add the fixed IP as a secondary address on br-lan
    logger.info("Configuring fixed IP %s on %s", fixed_ip, place_name)
    shell_command.run(f"ip addr add {fixed_ip}/16 dev br-lan 2>/dev/null || true")

    # Verify it was added
    stdout, _, rc = shell_command.run(f"ip addr show br-lan | grep '{fixed_ip}'")
    if rc == 0:
        logger.info("Fixed IP %s configured successfully", fixed_ip)
        return fixed_ip
    else:
        logger.error("Failed to configure fixed IP %s", fixed_ip)
        return None


@pytest.fixture
def ssh_command(shell_command, target):
    # Get the place name to look up the fixed IP
    place_name = getenv("LG_PLACE", "")

    # Configure fixed IP via serial before attempting SSH
    fixed_ip = _configure_fixed_ip(shell_command, place_name)
    if fixed_ip:
        logger.info("Using fixed IP %s for SSH on %s", fixed_ip, place_name)

    ssh = target.get_driver("SSHDriver")
    return ssh


sys.path.insert(0, str(Path(__file__).parent))
from conftest_mesh import mesh_nodes  # noqa: F811
