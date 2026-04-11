"""
Multi-node mesh fixtures for LibreMesh testing.

Supports two modes:

1. Physical (default): LG_MESH_PLACES, LG_IMAGE/LG_IMAGE_MAP.
   Boots N DUTs in parallel via labgrid (one subprocess per node).

2. Virtual: LG_VIRTUAL_MESH=1, VIRTUAL_MESH_IMAGE, VIRTUAL_MESH_NODES.
   Launches N QEMU VMs with vwifi; no labgrid.

Requires LG_MESH_PLACES (comma-separated place names) and either:
  - LG_IMAGE: single image path used for all nodes (backward compatible).
  - LG_IMAGE_MAP: per-place image paths, format "place1=/path/img1,place2=/path/img2".
    Falls back to LG_IMAGE for any place not listed in the map.

Optional: LG_MESH_KEEP_POWERED=1 to leave nodes powered on after tests (for SSH/serial debugging).
The labgrid place is still released so other sessions can use it later.

Usage (physical, mixed device types):
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
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest
from lime_helpers import REPO_ROOT, generate_mesh_ssh_ip, resolve_target_yaml

# Allow importing scripts from repo root
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger(__name__)

BOOT_SCRIPT = Path(__file__).parent / "mesh_boot_node.py"

BOOT_TIMEOUT = 420
NETWORK_SETTLE_TIMEOUT = 90
SUBPROCESS_SHUTDOWN_TIMEOUT = 30


class SSHProxy:
    """Lightweight SSH client using subprocess, compatible with labgrid SSHDriver API.

    Two modes:
    - Physical (vlan_iface set): Connects via labgrid-bound-connect through a VLAN.
    - Virtual (vlan_iface None): Direct SSH to host:port (e.g. 127.0.0.1:2222).
    """

    def __init__(self, host: str, vlan_iface: Optional[str] = "vlan200",
                 username: str = "root", port: int = 22):
        self._host = host
        self._vlan_iface = vlan_iface
        self._username = username
        self._port = port

    def _build_ssh_cmd(self, command: str) -> list[str]:
        base = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
        ]
        if self._vlan_iface:
            proxy_cmd = (f"sudo /usr/local/sbin/labgrid-bound-connect "
                         f"{self._vlan_iface} {self._host} {self._port}")
            base.extend(["-o", f"ProxyCommand={proxy_cmd}", f"{self._username}@{self._host}"])
        else:
            base.extend(["-p", str(self._port), f"{self._username}@{self._host}"])
        base.append(command)
        return base

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
    mesh_ip: str = ""
    _process: Optional[subprocess.Popen] = field(default=None, repr=False)
    _stop_file: str = field(default="", repr=False)

    @property
    def ip(self) -> str:
        """Backward-compatible alias for older tests that still read ``.ip``."""
        return self.mesh_ip


def _build_mesh_ssh_ip_map(places: list[str]) -> dict[str, str]:
    """Return a unique mesh SSH/control IP for each place, failing on collisions."""
    mesh_ssh_ip_map = {}
    seen = {}
    for place in places:
        mesh_ssh_ip = generate_mesh_ssh_ip(place)
        if mesh_ssh_ip in seen:
            raise ValueError(
                f"Duplicate mesh SSH IP {mesh_ssh_ip} for {seen[mesh_ssh_ip]} and {place}"
            )
        mesh_ssh_ip_map[place] = mesh_ssh_ip
        seen[mesh_ssh_ip] = place
    return mesh_ssh_ip_map




def _get_coordinator_address() -> str:
    return os.environ.get("LG_COORDINATOR", "localhost:20408")


def _get_vlan_iface() -> str:
    return os.environ.get("LG_MESH_VLAN_IFACE", "vlan200")


def _get_mesh_tftp_ip() -> str:
    """TFTP server IP on the mesh VLAN (host address on vlan200)."""
    return os.environ.get("LG_MESH_TFTP_IP", "192.168.200.1")


def _compute_network_settle_timeout(node_count: int) -> int:
    """Return a convergence window that scales mildly with topology size."""
    extra_nodes = max(0, node_count - 3)
    return NETWORK_SETTLE_TIMEOUT + extra_nodes * 30


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
        if os.path.exists(status_file):
            with open(status_file) as f:
                status = json.load(f)
            _dump_boot_log(place, log_file)
            return status

        if proc.poll() is not None:
            _dump_boot_log(place, log_file)
            return {"place": place, "ok": False, "error": "subprocess exited early"}

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


def _configure_vwifi_node(ssh: SSHProxy, vwifi_server_ip: str, node_index: int):
    """Configure vwifi-client and restart wireless on a virtual node.

    Networking: LibreMesh absorbs eth0/eth1 into br-lan, breaking SLIRP. We use
    a dedicated eth2 (10.99.0.0/24 SLIRP) that LibreMesh doesn't touch, and point
    vwifi-client to the SLIRP gateway (10.99.0.2) where vwifi-server listens.

    Wireless band override (root cause): LibreMesh defaults to 5GHz channel 48.
    hostapd on mac80211_hwsim fails with "Could not determine operating frequency"
    in 5GHz—the virtual driver does not provide the frequency info hostapd expects.
    As a result, the phy never gets a channel set, wlan0-mesh stays NO-CARRIER,
    no beacons are transmitted, and mesh nodes never discover each other.
    We override to 2.4GHz channel 1 after lime-config so hostapd succeeds and
    the mesh forms.
    """
    mac_hex = f"{node_index:02x}"
    eth2_ip = f"10.99.0.{10 + node_index}"
    setup_script = (
        "set -eu; "
        f"ip link set eth2 nomaster 2>/dev/null || true; "
        f"ip addr flush dev eth2 2>/dev/null || true; "
        f"ip addr add {eth2_ip}/24 dev eth2; "
        f"ip link set eth2 up; "
        f"service vwifi-client stop 2>/dev/null || true; "
        f"uci set vwifi.config.server_ip='{vwifi_server_ip}'; "
        f"uci set vwifi.config.mac_prefix='02:00:00:00:00:{mac_hex}'; "
        f"uci set vwifi.config.enabled='1'; "
        f"uci commit vwifi; "
        f"service vwifi-client start; "
        f"sleep 5; "
        f"lime-config; "
        # Override to 2.4GHz ch1: hostapd fails "Could not determine operating
        # frequency" on 5GHz with mac80211_hwsim; phy stays unchanneled, mesh
        # NO-CARRIER, no beacons. 2.4GHz works. See docs root cause section.
        f"uci set wireless.radio0.channel='1'; "
        f"uci set wireless.radio0.band='2g'; "
        f"uci set wireless.radio0.htmode='HT20'; "
        f"uci commit wireless; "
        f"wifi down; "
        f"sleep 1; "
        f"wifi up"
    )
    stdout, stderr, rc = ssh.run(setup_script)
    return rc == 0


def _mesh_nodes_virtual():
    """Launch virtual mesh (QEMU + vwifi) and yield MeshNode list.

    Uses LG_VIRTUAL_MESH=1, VIRTUAL_MESH_IMAGE, VIRTUAL_MESH_NODES.

    If vwifi-server is running, configures each VM's vwifi-client to connect
    to it, then runs lime-config + wifi up so nodes form a mesh over simulated
    WiFi (802.11 via mac80211_hwsim + vwifi).
    """
    from scripts.virtual_mesh_launcher import launch_virtual_mesh

    image = os.environ.get("VIRTUAL_MESH_IMAGE", "").strip()
    if not image:
        pytest.skip("VIRTUAL_MESH_IMAGE required when LG_VIRTUAL_MESH=1")

    n_nodes = int(os.environ.get("VIRTUAL_MESH_NODES", "3"))
    logger.info("Virtual mesh: launching %d nodes with image %s", n_nodes, image)

    nodes_raw, cleanup = launch_virtual_mesh(n_nodes=n_nodes, image_path=image)

    mesh_nodes_list = []
    for n in nodes_raw:
        ssh = SSHProxy(host=n.host, vlan_iface=None, port=n.port)
        node = MeshNode(place=n.place_id, ssh=ssh, mesh_ip="")
        mesh_nodes_list.append(node)

    skip_vwifi = os.environ.get("VIRTUAL_MESH_SKIP_VWIFI", "").strip() == "1"
    if not skip_vwifi:
        # In user-mode networking, we use a dedicated eth2 interface on a separate
        # SLIRP network (10.99.0.0/24) for vwifi-client connectivity. The gateway
        # is 10.99.0.2. vwifi-server listens on 0.0.0.0:8214, reachable from guests
        # via this gateway IP.
        vwifi_server_ip = os.environ.get("VIRTUAL_MESH_VWIFI_HOST", "10.99.0.2")
        logger.info("Configuring vwifi-client on %d nodes (server=%s)",
                    len(mesh_nodes_list), vwifi_server_ip)
        for i, node in enumerate(mesh_nodes_list, start=1):
            ok = _configure_vwifi_node(node.ssh, vwifi_server_ip, i)
            if ok:
                logger.info("Node %s: vwifi-client configured", node.place)
            else:
                logger.warning("Node %s: vwifi-client setup failed (vwifi may not be in image)",
                              node.place)

        convergence_wait = int(os.environ.get("VIRTUAL_MESH_CONVERGENCE_WAIT", "60"))
        logger.info("Waiting %ds for mesh convergence (batman-adv/babeld over vwifi)...",
                    convergence_wait)
        time.sleep(convergence_wait)
    else:
        logger.info("Skipping vwifi-client configuration (VIRTUAL_MESH_SKIP_VWIFI=1)")

    settle_timeout = _compute_network_settle_timeout(len(mesh_nodes_list))
    logger.info("All %d virtual nodes booted, waiting %ds for network to settle",
                len(mesh_nodes_list), settle_timeout)
    _wait_for_network(mesh_nodes_list, timeout=settle_timeout)

    yield mesh_nodes_list

    logger.info("Tearing down %d virtual mesh nodes", len(mesh_nodes_list))
    cleanup()


@pytest.fixture(scope="session")
def mesh_nodes(request, mesh_vlan_multi):
    """Boot mesh DUTs and yield MeshNode list. Physical or virtual based on LG_VIRTUAL_MESH.

    Depends on mesh_vlan_multi to ensure all DUT ports are switched to
    VLAN 200 before booting nodes (and restored on session teardown).

    Each MeshNode has:
      - .ssh (SSHProxy): run_check(cmd) -> list[str], run(cmd) -> (stdout, stderr, code)
      - .place (str): labgrid place name or virtual-mesh-N
      - .mesh_ip (str): node's real br-lan IPv4 address (empty for virtual until queried)

    Physical: LG_MESH_PLACES, LG_IMAGE/LG_IMAGE_MAP.
    Virtual: LG_VIRTUAL_MESH=1, VIRTUAL_MESH_IMAGE, VIRTUAL_MESH_NODES.
    """
    if os.environ.get("LG_VIRTUAL_MESH") == "1":
        yield from _mesh_nodes_virtual()
        return

    places_str = os.environ.get("LG_MESH_PLACES", "")
    if not places_str:
        pytest.skip("LG_MESH_PLACES not set")

    places = [p.strip() for p in places_str.split(",") if p.strip()]
    if not places:
        pytest.skip("LG_MESH_PLACES must contain at least 1 place")

    try:
        mesh_ssh_ip_map = _build_mesh_ssh_ip_map(places)
    except ValueError as e:
        pytest.fail(str(e))

    image_map = _resolve_image_map()
    default_image = os.environ.get("LG_IMAGE", "")
    if not image_map and not default_image:
        pytest.skip("LG_IMAGE or LG_IMAGE_MAP must be set")

    coordinator = _get_coordinator_address()
    vlan_iface = _get_vlan_iface()

    mesh_tftp_ip = _get_mesh_tftp_ip()
    os.environ["TFTP_SERVER_IP"] = mesh_tftp_ip
    logger.info("Mesh test: booting %d nodes in parallel (TFTP_SERVER_IP=%s): %s",
                len(places), mesh_tftp_ip, places)
    logger.info("Reserved mesh SSH IPs: %s", mesh_ssh_ip_map)

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
        target_yaml = resolve_target_yaml(place)
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
    seen_ssh_ips = {}

    for place in places:
        status = _wait_for_status(
            status_files[place], procs[place], place, BOOT_TIMEOUT,
            log_file=log_files[place],
        )
        if status.get("ok"):
            ssh_ip = status.get("ssh_ip") or status.get("ip", "")
            mesh_ip = status.get("mesh_ip") or status.get("ip", "")
            if not ssh_ip:
                logger.error("Node %s booted but did not report ssh_ip", place)
                failed.append(place)
                continue
            if ssh_ip in seen_ssh_ips:
                logger.error(
                    "Duplicate ssh_ip %s reported by %s and %s",
                    ssh_ip, seen_ssh_ips[ssh_ip], place,
                )
                failed.append(place)
                continue
            seen_ssh_ips[ssh_ip] = place
            ssh = SSHProxy(host=ssh_ip, vlan_iface=vlan_iface)
            node = MeshNode(
                place=place,
                ssh=ssh,
                mesh_ip=mesh_ip,
                _process=procs[place],
                _stop_file=stop_files[place],
            )
            nodes.append(node)
            attempts_used = status.get("attempts_used", 1)
            logger.info(
                "Node %s booted successfully (ssh_ip=%s, mesh_ip=%s, attempts=%s)",
                place, ssh_ip, mesh_ip, attempts_used,
            )
        else:
            error = status.get("error", "unknown")
            stage = status.get("failure_stage", "unknown")
            error_type = status.get("error_type", "unknown")
            attempts_used = status.get("attempts_used", "?")
            summary = status.get("error_summary", error)
            logger.error(
                "Node %s failed to boot after %s attempts at stage %s (%s): %s",
                place, attempts_used, stage, error_type, summary,
            )
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

    settle_timeout = _compute_network_settle_timeout(len(nodes))
    logger.info("All %d nodes booted, waiting %ds for network to settle",
                len(nodes), settle_timeout)
    _wait_for_network(nodes, timeout=settle_timeout)

    yield nodes

    logger.info("Tearing down %d mesh nodes", len(nodes))
    for node in nodes:
        _shutdown_subprocess(node._process, node._stop_file, node.place)
    os.environ.pop("TFTP_SERVER_IP", None)


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
