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

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from lime_helpers import (
    configure_fixed_ip,
    ensure_batman_mesh,
    is_qemu_target,
    resolve_target_yaml,
    suppress_kernel_console,
)

logger = logging.getLogger(__name__)

pytest_plugins = ["conftest_mesh", "conftest_vlan"]

device = getenv("LG_ENV", "Unknown").split("/")[-1].split(".")[0]


def _resolve_target_from_place() -> str | None:
    """Auto-set LG_ENV from LG_PLACE when LG_ENV is not provided."""
    lg_env = getenv("LG_ENV")
    lg_place = getenv("LG_PLACE")

    if lg_env or not lg_place:
        return lg_env

    try:
        return resolve_target_yaml(lg_place)
    except (ValueError, FileNotFoundError):
        return None


def pytest_addoption(parser):
    parser.addoption("--firmware", action="store", default="firmware.bin")


def pytest_configure(config):
    config._metadata = getattr(config, "_metadata", {})
    config._metadata["version"] = "12.3.4"
    config._metadata["environment"] = "staging"

    config.addinivalue_line("markers", "mesh: multi-node LibreMesh mesh tests")

    resolved_env = _resolve_target_from_place()
    if resolved_env:
        os.environ["LG_ENV"] = resolved_env


def ubus_call(command, namespace, method, params={}):
    output = command.run_check(f"ubus call {namespace} {method} '{json.dumps(params)}'")

    try:
        return json.loads("\n".join(output))
    except json.JSONDecodeError:
        return {}


@pytest.fixture(scope="session", autouse=True)
def setup_env(pytestconfig):
    """Inject --firmware into labgrid's env config when labgrid is active.

    Unlike upstream, this fixture does NOT take labgrid's ``env`` fixture as a
    parameter.  Reason: multi-node mesh tests (conftest_mesh.py) run without
    ``--lg-env`` and therefore labgrid never creates an ``env``.  If this
    fixture depended on ``env``, pytest would skip or error on every mesh test.
    We resolve the env from pytestconfig.stash instead, which is a no-op when
    labgrid is inactive.
    """
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
        shell = strategy.shell
        suppress_kernel_console(shell)
        return shell
    except Exception:
        logger.exception("Failed to transition to state shell")
        pytest.exit("Failed to transition to state shell", returncode=3)


@pytest.fixture
def ssh_command(shell_command, target):
    if not is_qemu_target(target):
        ensure_batman_mesh(target)

    fixed_ip = configure_fixed_ip(shell_command, target)
    if fixed_ip:
        logger.info("Using fixed IP %s for SSH", fixed_ip)

    ssh = target.get_driver("SSHDriver")
    return ssh
