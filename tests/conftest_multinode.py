"""
Multi-node test fixtures for openwrt-tests.

Boots N DUTs in parallel via labgrid (one subprocess per node) and provides
them as a list of MultiNode objects with SSH access.

Requires:
    LG_MULTI_PLACES: comma-separated labgrid place names
    LG_IMAGE or LG_IMAGE_MAP: firmware image(s) for the nodes

Optional:
    LG_MULTI_KEEP_POWERED=1: leave nodes powered on after tests
    LG_MULTI_VLAN_IFACE: VLAN interface for SSH proxy (default: vlan200)
    LG_MULTI_TEST_SUBNET: subnet prefix for test IPs (default: 192.168.1)

Usage:
    export LG_MULTI_PLACES="labgrid-mylab-device_a,labgrid-mylab-device_b"
    export LG_IMAGE="/path/to/openwrt-initramfs.itb"
    uv run pytest tests/test_multinode.py -v --log-cli-level=INFO
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest
import yaml

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOT_SCRIPT = Path(__file__).parent / "multinode_boot.py"
LABNET_PATH = REPO_ROOT / "labnet.yaml"

BOOT_TIMEOUT = 420
NETWORK_SETTLE_TIMEOUT = 90
SUBPROCESS_SHUTDOWN_TIMEOUT = 30


class SSHProxy:
    """Lightweight SSH client via subprocess, compatible with labgrid SSHDriver API.

    Connects through labgrid-bound-connect when vlan_iface is set, or
    directly when vlan_iface is None.
    """

    def __init__(
        self,
        host: str,
        vlan_iface: Optional[str] = "vlan200",
        username: str = "root",
        port: int = 22,
    ):
        self._host = host
        self._vlan_iface = vlan_iface
        self._username = username
        self._port = port

    def _build_ssh_cmd(self, command: str) -> list[str]:
        base = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
        ]
        if self._vlan_iface:
            proxy_cmd = (
                f"sudo /usr/local/sbin/labgrid-bound-connect "
                f"{self._vlan_iface} {self._host} {self._port}"
            )
            base.extend(
                ["-o", f"ProxyCommand={proxy_cmd}", f"{self._username}@{self._host}"]
            )
        else:
            base.extend(["-p", str(self._port), f"{self._username}@{self._host}"])
        base.append(command)
        return base

    def run_check(self, command: str) -> list[str]:
        """Run a command and return stdout lines. Raises on non-zero exit."""
        result = subprocess.run(
            self._build_ssh_cmd(command),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode,
                command,
                output=result.stdout,
                stderr=result.stderr,
            )
        return [line for line in result.stdout.splitlines() if line]

    def run(self, command: str) -> tuple[list[str], list[str], int]:
        """Run a command and return (stdout_lines, stderr_lines, exit_code)."""
        try:
            result = subprocess.run(
                self._build_ssh_cmd(command),
                capture_output=True,
                text=True,
                timeout=120,
            )
            stdout = [line for line in result.stdout.splitlines() if line]
            stderr = [line for line in result.stderr.splitlines() if line]
            return stdout, stderr, result.returncode
        except subprocess.TimeoutExpired:
            return [], ["SSH command timed out"], 1


@dataclass
class MultiNode:
    """Represents a booted DUT with SSH access."""

    place: str
    ssh: SSHProxy
    ip: str = ""
    _process: Optional[subprocess.Popen] = field(default=None, repr=False)
    _stop_file: str = field(default="", repr=False)


def _resolve_target_yaml(place_name: str) -> str:
    """Resolve the target YAML path for a labgrid place via labnet.yaml.

    Place name format: <prefix>-<host>-<device_instance>
    Falls back to targets/<device_instance>.yaml if labnet.yaml is missing.
    """
    parts = place_name.split("-", 2)
    if len(parts) < 3:
        device_instance = place_name
    else:
        device_instance = parts[2]

    if LABNET_PATH.exists():
        with open(LABNET_PATH) as f:
            labnet = yaml.safe_load(f) or {}

        devices = labnet.get("devices", {})
        if device_instance in devices:
            target_name = devices[device_instance].get("target_file", device_instance)
            target_file = REPO_ROOT / f"targets/{target_name}.yaml"
            if target_file.exists():
                return str(target_file)

        for lab_config in labnet.get("labs", {}).values():
            for base_device, instances in lab_config.get(
                "device_instances", {}
            ).items():
                if device_instance in instances and base_device in devices:
                    target_name = devices[base_device].get("target_file", base_device)
                    target_file = REPO_ROOT / f"targets/{target_name}.yaml"
                    if target_file.exists():
                        return str(target_file)

    fallback = REPO_ROOT / f"targets/{device_instance}.yaml"
    if fallback.exists():
        return str(fallback)

    raise FileNotFoundError(
        f"No target YAML found for place '{place_name}' "
        f"(device instance: '{device_instance}')"
    )


def _get_coordinator_address() -> str:
    return os.environ.get("LG_COORDINATOR", "localhost:20408")


def _get_vlan_iface() -> str:
    return os.environ.get("LG_MULTI_VLAN_IFACE", "vlan200")


def _resolve_image_map() -> dict[str, str]:
    """Parse LG_IMAGE_MAP into {place: image_path}.

    Format: "place1=/path/img1,place2=/path/img2"
    """
    raw = os.environ.get("LG_IMAGE_MAP", "").strip()
    if not raw:
        return {}
    result = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if "=" not in entry:
            continue
        place, _, path = entry.partition("=")
        result[place.strip()] = path.strip()
    return result


def _launch_boot_subprocess(
    place: str,
    image: str,
    target_yaml: str,
    coordinator: str,
    tmpdir: str,
    node_index: int = 0,
) -> tuple[subprocess.Popen, str, str, str]:
    """Launch multinode_boot.py as a subprocess for one place."""
    status_file = os.path.join(tmpdir, f"status_{place}.json")
    stop_file = os.path.join(tmpdir, f"stop_{place}")
    log_file = os.path.join(tmpdir, f"boot_{place}.log")

    cmd = [
        sys.executable,
        str(BOOT_SCRIPT),
        "--place",
        place,
        "--image",
        image,
        "--target-yaml",
        target_yaml,
        "--coordinator",
        coordinator,
        "--node-index",
        str(node_index),
        "--status-file",
        status_file,
        "--stop-file",
        stop_file,
    ]

    logger.info("Launching boot subprocess for %s", place)
    log_fh = open(log_file, "w")
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(REPO_ROOT),
    )
    return proc, status_file, stop_file, log_file


def _wait_for_status(
    status_file: str,
    proc: subprocess.Popen,
    place: str,
    timeout: int,
    log_file: str = "",
) -> dict:
    """Wait for a boot subprocess to write its status file."""
    deadline = time.time() + timeout

    while time.time() < deadline:
        if proc.poll() is not None:
            _dump_boot_log(place, log_file)
            return {"place": place, "ok": False, "error": "subprocess exited early"}

        if os.path.exists(status_file):
            with open(status_file) as f:
                status = json.load(f)
            _dump_boot_log(place, log_file)
            return status

        time.sleep(2)

    logger.error("Timeout waiting for %s to boot (>%ds)", place, timeout)
    proc.terminate()
    _dump_boot_log(place, log_file)
    return {"place": place, "ok": False, "error": f"boot timeout ({timeout}s)"}


def _dump_boot_log(place: str, log_file: str):
    if not log_file or not os.path.exists(log_file):
        return
    try:
        with open(log_file) as f:
            content = f.read()
        if content.strip():
            logger.info(
                "=== Boot log for %s ===\n%s\n=== End ===", place, content[-3000:]
            )
    except Exception:
        pass


def _shutdown_subprocess(proc: subprocess.Popen, stop_file: str, place: str):
    """Signal a boot subprocess to shut down and wait for it."""
    try:
        Path(stop_file).touch()
    except OSError:
        pass

    try:
        proc.wait(timeout=SUBPROCESS_SHUTDOWN_TIMEOUT)
    except subprocess.TimeoutExpired:
        logger.warning("Boot subprocess for %s didn't exit, sending SIGTERM", place)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("Killing boot subprocess for %s", place)
            proc.kill()


def _wait_for_network(nodes: list[MultiNode], timeout: int = NETWORK_SETTLE_TIMEOUT):
    """Wait until all nodes respond to SSH echo."""
    deadline = time.time() + timeout
    pending = set(range(len(nodes)))

    while pending and time.time() < deadline:
        for i in list(pending):
            try:
                output = nodes[i].ssh.run_check("echo node-ready")
                if "node-ready" in output:
                    logger.info("Node %s: SSH reachable", nodes[i].place)
                    pending.discard(i)
            except Exception:
                pass
        if pending:
            time.sleep(5)

    if pending:
        names = [nodes[i].place for i in pending]
        logger.warning("Nodes not reachable via SSH after %ds: %s", timeout, names)


@pytest.fixture(scope="session")
def multi_nodes(request):
    """Boot multiple DUTs and yield a list of MultiNode objects.

    Each MultiNode has:
      - .ssh: SSHProxy with run_check(cmd) and run(cmd)
      - .place: labgrid place name
      - .ip: node's unique test IPv4 address

    Each node gets a unique IP assigned via serial console after boot
    (default: 192.168.1.10, .11, .12... - configurable via LG_MULTI_TEST_SUBNET).
    VLAN interface per node is auto-detected from NetworkService or
    falls back to LG_MULTI_VLAN_IFACE.

    If conftest_vlan is loaded and VLAN_SWITCH_ENABLED=1, VLAN switching
    happens before nodes boot (shared_vlan_multi fixture).

    Triggered by LG_MULTI_PLACES env var.
    """
    try:
        request.getfixturevalue("shared_vlan_multi")
    except pytest.FixtureLookupError:
        pass

    places_str = os.environ.get("LG_MULTI_PLACES", "")
    if not places_str:
        pytest.skip("LG_MULTI_PLACES not set")

    places = [p.strip() for p in places_str.split(",") if p.strip()]
    if not places:
        pytest.skip("LG_MULTI_PLACES is empty")

    image_map = _resolve_image_map()
    default_image = os.environ.get("LG_IMAGE", "")
    if not image_map and not default_image:
        pytest.skip("LG_IMAGE or LG_IMAGE_MAP must be set")

    coordinator = _get_coordinator_address()

    logger.info(
        "Multi-node test: booting %d nodes in parallel: %s", len(places), places
    )

    tmpdir = tempfile.mkdtemp(prefix="multinode_boot_")
    procs = {}
    status_files = {}
    stop_files = {}
    log_files = {}

    for idx, place in enumerate(places):
        image_path = image_map.get(place, default_image)
        if not image_path:
            pytest.fail(
                f"No image for place {place} (set LG_IMAGE or add it to LG_IMAGE_MAP)"
            )
        target_yaml = _resolve_target_yaml(place)
        logger.info(
            "Node %s (index %d): target %s, image %s",
            place,
            idx,
            target_yaml,
            image_path,
        )
        proc, sf, stf, lf = _launch_boot_subprocess(
            place,
            image_path,
            target_yaml,
            coordinator,
            tmpdir,
            node_index=idx,
        )
        procs[place] = proc
        status_files[place] = sf
        stop_files[place] = stf
        log_files[place] = lf

    nodes = []
    failed = []

    for place in places:
        status = _wait_for_status(
            status_files[place],
            procs[place],
            place,
            BOOT_TIMEOUT,
            log_file=log_files[place],
        )
        if status.get("ok"):
            node_ip = status.get("ip", "")
            node_vlan = status.get("vlan_iface") or _get_vlan_iface()
            if not node_ip:
                logger.warning("Node %s: no test IP assigned, SSH may not work", place)
            ssh = SSHProxy(host=node_ip, vlan_iface=node_vlan)
            node = MultiNode(
                place=place,
                ssh=ssh,
                ip=node_ip,
                _process=procs[place],
                _stop_file=stop_files[place],
            )
            nodes.append(node)
            logger.info("Node %s booted (IP: %s, VLAN: %s)", place, node_ip, node_vlan)
        else:
            error = status.get("error", "unknown")
            logger.error("Node %s failed to boot: %s", place, error)
            failed.append(place)

    if failed:
        for node in nodes:
            _shutdown_subprocess(node._process, node._stop_file, node.place)
        for place in failed:
            if procs[place].poll() is None:
                procs[place].terminate()
        pytest.fail(
            f"Not all nodes booted: {len(nodes)}/{len(places)} (failed: {failed})"
        )

    nodes.sort(key=lambda n: places.index(n.place))

    logger.info(
        "All %d nodes booted, waiting %ds for network",
        len(nodes),
        NETWORK_SETTLE_TIMEOUT,
    )
    _wait_for_network(nodes, timeout=NETWORK_SETTLE_TIMEOUT)

    yield nodes

    logger.info("Tearing down %d nodes", len(nodes))
    for node in nodes:
        _shutdown_subprocess(node._process, node._stop_file, node.place)
