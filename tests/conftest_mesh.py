"""
Multi-node mesh fixtures for LibreMesh testing.

Boots N DUTs in parallel via labgrid (one subprocess per node) and provides
SSH access to each. This avoids labgrid's internal thread-safety issues
(StepLogger, coordinator multi-session crashes) that prevent in-process
parallel booting.

Requires LG_MESH_PLACES (comma-separated place names) and either:
  - LG_IMAGE: single image path used for all nodes (backward compatible).
  - LG_IMAGE_MAP: per-place image paths, format "place1=/path/img1,place2=/path/img2".
    Falls back to LG_IMAGE for any place not listed in the map.

Optional: LG_MESH_KEEP_POWERED=1 to leave nodes powered on after tests (for SSH/serial debugging).
The labgrid place is still released so other sessions can use it later.

Usage (mixed device types):
    export LG_MESH_PLACES="labgrid-fcefyn-openwrt_one,labgrid-fcefyn-bananapi_bpi-r4"
    export LG_IMAGE_MAP="labgrid-fcefyn-openwrt_one=/srv/tftp/firmwares/openwrt_one/openwrt-24.10.5-mediatek-filogic-openwrt_one-initramfs.itb,labgrid-fcefyn-bananapi_bpi-r4=/srv/tftp/firmwares/bananapi_bpi-r4/openwrt-24.10.5-mediatek-filogic-bananapi_bpi-r4-initramfs-recovery.itb"
    uv run pytest tests/test_mesh.py -v --log-cli-level=INFO

Usage (single image for all nodes):
    export LG_MESH_PLACES="labgrid-fcefyn-belkin_rt3200_2,labgrid-fcefyn-belkin_rt3200_3"
    export LG_IMAGE="/srv/tftp/firmwares/belkin_rt3200/lime-...itb"
    uv run pytest tests/test_mesh.py -v --log-cli-level=INFO
"""

import json
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest
import yaml

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
LABNET_PATH = REPO_ROOT / "labnet.yaml"
BOOT_SCRIPT = Path(__file__).parent / "mesh_boot_node.py"

BOOT_TIMEOUT = 420
NETWORK_SETTLE_TIMEOUT = 90
SUBPROCESS_SHUTDOWN_TIMEOUT = 30


class SSHProxy:
    """Lightweight SSH client using subprocess, compatible with labgrid SSHDriver API.

    Connects to a DUT via labgrid-bound-connect (socat) through a VLAN interface,
    mirroring how labgrid's SSHDriver reaches DUTs in mesh mode.
    """

    def __init__(self, host: str, vlan_iface: str = "vlan200",
                 username: str = "root", port: int = 22):
        self._host = host
        self._vlan_iface = vlan_iface
        self._username = username
        self._port = port

    def _build_ssh_cmd(self, command: str) -> list[str]:
        proxy_cmd = (f"sudo /usr/local/sbin/labgrid-bound-connect "
                     f"{self._vlan_iface} {self._host} {self._port}")
        return [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", f"ProxyCommand={proxy_cmd}",
            f"{self._username}@{self._host}",
            command,
        ]

    def run_check(self, command: str) -> list[str]:
        """Run a command and return stdout lines. Raises on non-zero exit."""
        result = subprocess.run(
            self._build_ssh_cmd(command),
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, command,
                output=result.stdout, stderr=result.stderr,
            )
        return [line for line in result.stdout.splitlines() if line]

    def run(self, command: str) -> tuple[list[str], list[str], int]:
        """Run a command and return (stdout_lines, stderr_lines, exit_code)."""
        try:
            result = subprocess.run(
                self._build_ssh_cmd(command),
                capture_output=True, text=True, timeout=120,
            )
            stdout = [line for line in result.stdout.splitlines() if line]
            stderr = [line for line in result.stderr.splitlines() if line]
            return stdout, stderr, result.returncode
        except subprocess.TimeoutExpired:
            return [], ["SSH command timed out"], 1


@dataclass
class MeshNode:
    """Represents a booted mesh DUT with SSH access."""
    place: str
    ssh: SSHProxy
    ip: str = ""
    _process: Optional[subprocess.Popen] = field(default=None, repr=False)
    _stop_file: str = field(default="", repr=False)


def _resolve_target_yaml(place_name: str) -> str:
    """Resolve target YAML path from place name via labnet.yaml."""
    parts = place_name.split("-", 2)
    if len(parts) < 3:
        raise ValueError(f"Cannot parse device instance from place: {place_name}")
    device_instance = parts[2]

    with open(LABNET_PATH) as f:
        labnet = yaml.safe_load(f)

    if device_instance in labnet.get("devices", {}):
        device_config = labnet["devices"][device_instance]
        target_name = device_config.get("target_file", device_instance)
        target_file = REPO_ROOT / f"targets/{target_name}.yaml"
        if target_file.exists():
            return str(target_file)

    for lab_config in labnet.get("labs", {}).values():
        device_instances = lab_config.get("device_instances", {})
        for base_device, instances in device_instances.items():
            if device_instance in instances:
                if base_device in labnet.get("devices", {}):
                    device_config = labnet["devices"][base_device]
                    target_name = device_config.get("target_file", base_device)
                    target_file = REPO_ROOT / f"targets/{target_name}.yaml"
                    if target_file.exists():
                        return str(target_file)

    raise FileNotFoundError(
        f"No target YAML found for place {place_name} (instance: {device_instance})"
    )


def _get_coordinator_address() -> str:
    return os.environ.get("LG_COORDINATOR", "localhost:20408")


def _get_vlan_iface() -> str:
    return os.environ.get("LG_MESH_VLAN_IFACE", "vlan200")


def _resolve_image_map() -> dict[str, str]:
    """Parse LG_IMAGE_MAP into {place: image_path}.

    Format: "place1=/path/img1,place2=/path/img2"
    Falls back to empty dict if not set; callers fall back to LG_IMAGE.
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


def _get_image_for_place(place: str, image_map: dict[str, str],
                         default_image: str) -> str:
    """Return the image path for a given place, with fallback to default."""
    return image_map.get(place, default_image)


def _launch_boot_subprocess(place: str, image: str, target_yaml: str,
                            coordinator: str, tmpdir: str) -> tuple[subprocess.Popen, str, str, str]:
    """Launch mesh_boot_node.py as a subprocess for one place."""
    status_file = os.path.join(tmpdir, f"status_{place}.json")
    stop_file = os.path.join(tmpdir, f"stop_{place}")
    log_file = os.path.join(tmpdir, f"boot_{place}.log")

    cmd = [
        "uv", "run", "python", str(BOOT_SCRIPT),
        "--place", place,
        "--image", image,
        "--target-yaml", target_yaml,
        "--coordinator", coordinator,
        "--status-file", status_file,
        "--stop-file", stop_file,
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


def _wait_for_status(status_file: str, proc: subprocess.Popen,
                     place: str, timeout: int, log_file: str = "") -> dict:
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
    """Read and log the boot subprocess output for debugging."""
    if not log_file or not os.path.exists(log_file):
        return
    try:
        with open(log_file) as f:
            content = f.read()
        if content.strip():
            logger.info("=== Boot log for %s ===\n%s\n=== End boot log ===",
                        place, content[-3000:])
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


@pytest.fixture(scope="session")
def mesh_nodes(request):
    """Boot mesh DUTs in parallel (one subprocess per node) and yield MeshNode list.

    Each MeshNode has:
      - .ssh (SSHProxy): run_check(cmd) -> list[str], run(cmd) -> (stdout, stderr, code)
      - .place (str): labgrid place name
      - .ip (str): node's br-lan IPv4 address

    Set LG_MESH_PLACES as comma-separated list of labgrid place names.
    """
    places_str = os.environ.get("LG_MESH_PLACES", "")
    if not places_str:
        pytest.skip("LG_MESH_PLACES not set")

    places = [p.strip() for p in places_str.split(",") if p.strip()]
    if not places:
        pytest.skip("LG_MESH_PLACES must contain at least 1 place")

    image_map = _resolve_image_map()
    default_image = os.environ.get("LG_IMAGE", "")
    if not image_map and not default_image:
        pytest.skip("LG_IMAGE or LG_IMAGE_MAP must be set")

    coordinator = _get_coordinator_address()
    vlan_iface = _get_vlan_iface()

    logger.info("Mesh test: booting %d nodes in parallel: %s", len(places), places)

    tmpdir = tempfile.mkdtemp(prefix="mesh_boot_")
    procs = {}
    status_files = {}
    stop_files = {}
    log_files = {}

    for place in places:
        image_path = _get_image_for_place(place, image_map, default_image)
        if not image_path:
            pytest.fail(f"No image configured for place {place} "
                        "(set LG_IMAGE or add it to LG_IMAGE_MAP)")
        target_yaml = _resolve_target_yaml(place)
        logger.info("Node %s: target YAML %s, image %s", place, target_yaml, image_path)
        proc, sf, stf, lf = _launch_boot_subprocess(
            place, image_path, target_yaml, coordinator, tmpdir,
        )
        procs[place] = proc
        status_files[place] = sf
        stop_files[place] = stf
        log_files[place] = lf

    nodes = []
    failed = []

    for place in places:
        status = _wait_for_status(
            status_files[place], procs[place], place, BOOT_TIMEOUT,
            log_file=log_files[place],
        )
        if status.get("ok"):
            node_ip = status.get("ip", "192.168.1.1")
            ssh = SSHProxy(host=node_ip, vlan_iface=vlan_iface)
            node = MeshNode(
                place=place,
                ssh=ssh,
                ip=node_ip,
                _process=procs[place],
                _stop_file=stop_files[place],
            )
            nodes.append(node)
            logger.info("Node %s booted successfully (IP: %s)", place, node_ip)
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
            f"Not all mesh nodes booted: {len(nodes)}/{len(places)} "
            f"(failed: {failed})"
        )

    nodes.sort(key=lambda n: places.index(n.place))

    logger.info("All %d nodes booted, waiting %ds for network to settle",
                len(nodes), NETWORK_SETTLE_TIMEOUT)
    _wait_for_network(nodes, timeout=NETWORK_SETTLE_TIMEOUT)

    yield nodes

    logger.info("Tearing down %d mesh nodes", len(nodes))
    for node in nodes:
        _shutdown_subprocess(node._process, node._stop_file, node.place)


def _wait_for_network(nodes: list[MeshNode], timeout: int = NETWORK_SETTLE_TIMEOUT):
    """Wait until all nodes respond to SSH echo."""
    deadline = time.time() + timeout
    pending = set(range(len(nodes)))

    while pending and time.time() < deadline:
        for i in list(pending):
            try:
                output = nodes[i].ssh.run_check("echo mesh-ready")
                if "mesh-ready" in output:
                    logger.info("Node %s: SSH reachable", nodes[i].place)
                    pending.discard(i)
            except Exception:
                pass
        if pending:
            time.sleep(5)

    if pending:
        names = [nodes[i].place for i in pending]
        logger.warning("Nodes not reachable via SSH after %ds: %s", timeout, names)
