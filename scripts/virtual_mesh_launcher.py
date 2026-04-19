#!/usr/bin/env python3
"""Launch N QEMU VMs with LibreMesh + vwifi for virtual mesh testing.

Starts vwifi-server on the host, launches N QEMUs with user-mode networking
(SSH via port-forward), waits for SSH on each, and returns node info for pytest.

Usage (library):
    from scripts.virtual_mesh_launcher import launch_virtual_mesh
    nodes = launch_virtual_mesh(n_nodes=3, image_path="/path/to/img")

Usage (CLI, for fixture):
    virtual_mesh_launcher.py --nodes 3 --image /path/to/img --status-file /tmp/nodes.json

Environment:
    VIRTUAL_MESH_IMAGE    - Path to LibreMesh x86_64 image (required)
    VIRTUAL_MESH_NODES    - Number of nodes (default: 3)
    VIRTUAL_MESH_MAX_NODES - Max allowed (default: 3 on CI, 5 self-hosted)
    VIRTUAL_MESH_SSH_BASE_PORT - Base port for SSH (default: 2222)
    VIRTUAL_MESH_BOOT_TIMEOUT - Seconds to wait per VM (default: 180)
    VIRTUAL_MESH_USE_TAP  - If 1, use TAP+bridge (experimental, requires sudo).
                           Default (user-mode) is recommended.
    VIRTUAL_MESH_SKIP_VWIFI - If 1, skip vwifi-server (VMs boot; mesh-over-WiFi disabled).
    VIRTUAL_MESH_VWIFI_USE_SUDO - If 1, run vwifi-server with sudo -n (for root-required setups).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# Defaults matching the design document
DEFAULT_SSH_BASE_PORT = 2222
DEFAULT_BOOT_TIMEOUT = 180
DEFAULT_MAX_NODES_CI = 3
DEFAULT_MAX_NODES_SELFHOSTED = 5
VWIFI_SERVER_DEFAULT = "vwifi-server"
QEMU_BIN = "qemu-system-x86_64"

# TAP mode (same as libremesh_node.sh)
TAP_BRIDGE = "br0"
TAP_HOST_IP = "10.13.0.10"
TAP_NET_PREFIX = "10.13.0"


@dataclass
class VirtualMeshNode:
    """Represents a booted virtual mesh node."""

    place_id: str
    host: str
    port: int
    ssh_args: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"place_id": self.place_id, "host": self.host, "port": self.port}


def _get_max_nodes() -> int:
    """Infer max nodes from environment (CI vs self-hosted)."""
    env_val = os.environ.get("VIRTUAL_MESH_MAX_NODES", "").strip()
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            pass
    # Heuristic: self-hosted runners often have label
    if os.environ.get("RUNNER_NAME", "").lower().find("self") >= 0:
        return DEFAULT_MAX_NODES_SELFHOSTED
    return DEFAULT_MAX_NODES_CI


def _check_ssh_reachable(host: str, port: int, timeout: int = 5) -> bool:
    """Check if SSH is reachable on host:port."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except (socket.error, OSError):
        return False


def _wait_for_ssh(host: str, port: int, timeout: int) -> bool:
    """Wait until SSH port is open or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _check_ssh_reachable(host, port, timeout=2):
            return True
        time.sleep(2)
    return False


def _poll_ssh_ready(host: str, port: int, timeout: int) -> bool:
    """Poll until SSH login succeeds or timeout expires.

    Combines port-open check with actual login check in a single loop,
    retrying every few seconds. Handles slow-booting VMs gracefully.
    """
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        if _check_ssh_reachable(host, port, timeout=2):
            if _run_ssh_check(host, port, timeout=15):
                return True
            logger.info(
                "VM %s:%d: port open but SSH login not ready (attempt %d)",
                host,
                port,
                attempt,
            )
        time.sleep(5)
    return False


def _run_ssh_check(host: str, port: int, timeout: int = 30) -> bool:
    """Verify SSH login works (passwordless root on OpenWrt/LibreMesh)."""
    cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "PubkeyAuthentication=no",
        f"root@{host}",
        "-p",
        str(port),
        "echo",
        "virtual-mesh-ok",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0 and "virtual-mesh-ok" in (result.stdout or ""):
            return True
        logger.debug(
            "SSH check %s:%d rc=%d stderr=%s",
            host,
            port,
            result.returncode,
            (result.stderr or "").strip(),
        )
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _find_vwifi_server() -> str | None:
    """Find vwifi-server binary."""
    env_path = os.environ.get("VIRTUAL_MESH_VWIFI_SERVER", "").strip()
    if env_path and os.path.isfile(env_path):
        return env_path
    return shutil.which(VWIFI_SERVER_DEFAULT)


def _find_qemu() -> str:
    """Find QEMU binary."""
    env_path = os.environ.get("VIRTUAL_MESH_QEMU", "").strip()
    if env_path:
        return env_path
    path = shutil.which(QEMU_BIN)
    if path:
        return path
    raise FileNotFoundError(f"QEMU binary '{QEMU_BIN}' not found in PATH")


def _vm_has_kvm() -> bool:
    """Check if /dev/kvm is available for acceleration."""
    return Path("/dev/kvm").exists()


def _run_sudo(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run command with sudo. Requires passwordless sudo for ip/tuntap."""
    return subprocess.run(["sudo"] + cmd, capture_output=True, text=True, timeout=30)


def _setup_tap_mode(n_nodes: int) -> tuple[list[tuple[str, str]], Callable[[], None]]:
    """Create bridge and tap interfaces. Returns [(tap_name, vm_ip), ...] and cleanup."""
    taps: list[str] = []

    def cleanup():
        for tap in taps:
            _run_sudo(["ip", "link", "delete", tap])

    # Ensure bridge exists
    r = _run_sudo(["ip", "link", "show", TAP_BRIDGE])
    if r.returncode != 0:
        _run_sudo(["ip", "link", "add", "name", TAP_BRIDGE, "type", "bridge"])
        _run_sudo(["ip", "addr", "add", f"{TAP_HOST_IP}/16", "dev", TAP_BRIDGE])
        _run_sudo(["ip", "link", "set", TAP_BRIDGE, "up"])
    else:
        r = _run_sudo(["ip", "addr", "show", TAP_BRIDGE])
        if TAP_HOST_IP not in (r.stdout or ""):
            _run_sudo(["ip", "addr", "add", f"{TAP_HOST_IP}/16", "dev", TAP_BRIDGE])
        _run_sudo(["ip", "link", "set", TAP_BRIDGE, "up"])

    result = []
    for i in range(1, n_nodes + 1):
        tap = f"tap{i}"
        taps.append(tap)
        vm_ip = f"{TAP_NET_PREFIX}.{i}"
        _run_sudo(["ip", "link", "delete", tap])  # remove if leftover from previous run
        _run_sudo(
            [
                "ip",
                "tuntap",
                "add",
                "dev",
                tap,
                "mode",
                "tap",
                "user",
                os.environ.get("USER", "root"),
            ]
        )
        _run_sudo(["ip", "link", "set", tap, "master", TAP_BRIDGE])
        _run_sudo(["ip", "link", "set", tap, "up"])
        result.append((tap, vm_ip))
    return result, cleanup


def launch_virtual_mesh(
    n_nodes: int,
    image_path: str,
    ssh_base_port: int | None = None,
    boot_timeout: int | None = None,
    status_file: str | None = None,
) -> tuple[list[VirtualMeshNode], Callable[[], None]]:
    """Launch vwifi-server and N QEMU VMs, wait for SSH, return node list and cleanup.

    vwifi-server is optional: if it exits on startup, a warning is logged (with
    log content saved to /tmp/virtual_mesh_vwifi_server.log) and execution
    continues. VMs boot normally; mesh-over-WiFi tests may fail. Use
    VIRTUAL_MESH_SKIP_VWIFI=1 to skip vwifi entirely, or
    VIRTUAL_MESH_VWIFI_USE_SUDO=1 to run vwifi-server with sudo.

    Args:
        n_nodes: Number of VMs to start.
        image_path: Path to LibreMesh x86_64 image (ext4 or similar).
        ssh_base_port: Base port for SSH (default from env or 2222).
        boot_timeout: Seconds to wait per VM (default from env or 180).
        status_file: If set, write JSON status to this file.

    Returns:
        Tuple of (list of VirtualMeshNode, cleanup_callable).
        Call cleanup_callable() on fixture teardown to stop all processes.

    Raises:
        ValueError: If n_nodes exceeds max or image missing.
        RuntimeError: If VM launch or SSH wait fails (not vwifi-server failure).
    """
    max_nodes = _get_max_nodes()
    if n_nodes > max_nodes:
        raise ValueError(
            f"VIRTUAL_MESH_NODES={n_nodes} exceeds VIRTUAL_MESH_MAX_NODES={max_nodes}. "
            f"Set VIRTUAL_MESH_MAX_NODES or reduce nodes."
        )

    image = Path(image_path)
    if not image.is_file():
        raise ValueError(f"Image not found: {image_path}")

    ssh_base = ssh_base_port or int(
        os.environ.get("VIRTUAL_MESH_SSH_BASE_PORT", str(DEFAULT_SSH_BASE_PORT))
    )
    timeout = boot_timeout or int(
        os.environ.get("VIRTUAL_MESH_BOOT_TIMEOUT", str(DEFAULT_BOOT_TIMEOUT))
    )

    qemu_bin = _find_qemu()
    vwifi_bin = _find_vwifi_server()
    if os.environ.get("VIRTUAL_MESH_SKIP_VWIFI", "").strip() == "1":
        vwifi_bin = None
    if not vwifi_bin:
        logger.warning(
            "vwifi-server not found or skipped; VMs may not form mesh over WiFi"
        )

    tmpdir = tempfile.mkdtemp(prefix="virtual_mesh_")
    vwifi_proc: subprocess.Popen | None = None
    qemu_procs: list[subprocess.Popen] = []
    qemu_logs: list[Path] = []

    def tap_cleanup() -> None:
        return None

    def do_cleanup():
        for p in qemu_procs:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        if vwifi_proc:
            try:
                vwifi_proc.terminate()
                vwifi_proc.wait(timeout=3)
            except Exception:
                try:
                    vwifi_proc.kill()
                except Exception:
                    pass
        tap_cleanup()

    use_tap = os.environ.get("VIRTUAL_MESH_USE_TAP", "").strip() == "1"

    try:
        if vwifi_bin:
            # Kill any stale vwifi-server from previous runs to avoid "Address already in use"
            subprocess.run(["pkill", "-9", "-f", "vwifi-server"], capture_output=True)
            subprocess.run(["killall", "-9", "vwifi-server"], capture_output=True)

            # Wait for port 8214 to be free (vwifi-server default port)
            for attempt in range(10):
                result = subprocess.run(
                    ["ss", "-tln", "sport", "=", "8214"], capture_output=True, text=True
                )
                if "8214" not in result.stdout:
                    break
                logger.info(
                    "Waiting for port 8214 to be released (attempt %d)", attempt + 1
                )
                time.sleep(2)

            logger.info("Starting vwifi-server")
            vwifi_log = Path(tmpdir) / "vwifi-server.log"
            use_sudo = os.environ.get("VIRTUAL_MESH_VWIFI_USE_SUDO", "").strip() == "1"
            vwifi_cmd = (
                ["sudo", "-n", vwifi_bin, "-u"] if use_sudo else [vwifi_bin, "-u"]
            )
            with open(vwifi_log, "w") as f:
                vwifi_proc = subprocess.Popen(
                    vwifi_cmd,
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            time.sleep(2)
            if vwifi_proc.poll() is not None:
                log_content = (
                    vwifi_log.read_text() if vwifi_log.exists() else "(no log)"
                )
                persist_log = Path("/tmp/virtual_mesh_vwifi_server.log")
                try:
                    persist_log.write_text(log_content)
                    logger.warning(
                        "vwifi-server exited; continuing without mesh-over-WiFi. "
                        "Log saved to %s:\n%s",
                        persist_log,
                        log_content,
                    )
                except OSError:
                    logger.warning(
                        "vwifi-server exited; continuing without mesh-over-WiFi. "
                        "vwifi-server.log:\n%s",
                        log_content,
                    )
                vwifi_proc = None

        use_kvm = _vm_has_kvm()
        nodes: list[VirtualMeshNode] = []

        if use_tap:
            tap_info, tap_cleanup = _setup_tap_mode(n_nodes)
            logger.info("TAP mode: bridge %s, host %s", TAP_BRIDGE, TAP_HOST_IP)
            for i in range(1, n_nodes + 1):
                tap_name, vm_ip = tap_info[i - 1]
                place_id = f"virtual-mesh-{i}"
                log_file = Path(tmpdir) / f"qemu-{i}.log"
                qemu_logs.append(log_file)
                mac = f"52:54:00:00:00:{i:02x}"
                cmd = [
                    qemu_bin,
                    "-M",
                    "q35",
                    "-m",
                    "512",
                    "-smp",
                    "2",
                    "-nographic",
                    "-snapshot",
                    "-drive",
                    f"file={image},if=virtio,format=raw,file.locking=off",
                    "-netdev",
                    f"tap,id=net{i},ifname={tap_name},script=no,downscript=no",
                    "-device",
                    f"virtio-net-pci,netdev=net{i},mac={mac}",
                    "-device",
                    "virtio-rng-pci",
                ]
                if use_kvm:
                    cmd.insert(1, "-enable-kvm")
                    cmd.insert(2, "-cpu")
                    cmd.insert(3, "host")
                logger.info(
                    "Starting VM %d (TAP %s, expected IP %s)", i, tap_name, vm_ip
                )
                with open(log_file, "w") as f:
                    proc = subprocess.Popen(
                        cmd, stdout=f, stderr=subprocess.STDOUT, start_new_session=True
                    )
                qemu_procs.append(proc)
            logger.info("Waiting up to %ds for SSH on each VM (TAP)", timeout)
            for i in range(1, n_nodes + 1):
                place_id = f"virtual-mesh-{i}"
                vm_ip = tap_info[i - 1][1]
                if _poll_ssh_ready(vm_ip, 22, timeout):
                    nodes.append(
                        VirtualMeshNode(
                            place_id=place_id,
                            host=vm_ip,
                            port=22,
                            ssh_args=[
                                "-o",
                                "StrictHostKeyChecking=no",
                                "-o",
                                "UserKnownHostsFile=/dev/null",
                            ],
                        )
                    )
                    logger.info("VM %d ready (%s:22)", i, vm_ip)
                else:
                    log_path = qemu_logs[i - 1]
                    tail = ""
                    if log_path.exists():
                        with open(log_path) as f:
                            tail = f.read()[-2000:]
                    logger.error(
                        "VM %d: SSH failed after %ds. Last log:\n%s", i, timeout, tail
                    )
                    raise RuntimeError(
                        f"VM {place_id}: SSH to {vm_ip} failed after {timeout}s"
                    )
        else:
            for i in range(1, n_nodes + 1):
                place_id = f"virtual-mesh-{i}"
                port = ssh_base + i - 1
                log_file = Path(tmpdir) / f"qemu-{i}.log"
                qemu_logs.append(log_file)
                mac = f"52:54:00:00:00:{i:02x}"
                # LibreMesh anygw: all nodes share 10.13.0.1 on br-lan.
                # Each VM has its own isolated SLIRP, so same guest addr is fine.
                hostfwd = f"tcp::{port}-10.13.0.1:22"
                # Third NIC (eth2) for vwifi-client -> vwifi-server connection.
                # Uses a separate SLIRP network (10.99.0.0/24) that LibreMesh won't
                # absorb into br-lan because it's not in the 10.13.x.x range.
                # The VM will need to configure eth2 with DHCP or static IP to reach
                # the host gateway at 10.99.0.2.
                cmd = [
                    qemu_bin,
                    "-M",
                    "q35",
                    "-m",
                    "512",
                    "-smp",
                    "2",
                    "-nographic",
                    "-snapshot",
                    "-drive",
                    f"file={image},if=virtio,format=raw,file.locking=off",
                    "-device",
                    f"virtio-net-pci,mac={mac},netdev=mesh0",
                    "-netdev",
                    f"user,id=mesh0,net=10.13.0.0/16,hostfwd={hostfwd}",
                    "-device",
                    "virtio-net-pci,netdev=wan0",
                    "-netdev",
                    "user,id=wan0",
                    "-device",
                    f"virtio-net-pci,mac=52:54:99:00:00:{i:02x},netdev=vwifi0",
                    "-netdev",
                    "user,id=vwifi0,net=10.99.0.0/24",
                    "-device",
                    "virtio-rng-pci",
                ]
                if use_kvm:
                    cmd.insert(1, "-enable-kvm")
                    cmd.insert(2, "-cpu")
                    cmd.insert(3, "host")
                logger.info("Starting VM %d (SSH port %d)", i, port)
                with open(log_file, "w") as f:
                    proc = subprocess.Popen(
                        cmd, stdout=f, stderr=subprocess.STDOUT, start_new_session=True
                    )
                qemu_procs.append(proc)
            logger.info("Waiting up to %ds for SSH on each VM", timeout)
            for i in range(1, n_nodes + 1):
                port = ssh_base + i - 1
                place_id = f"virtual-mesh-{i}"
                if _poll_ssh_ready("127.0.0.1", port, timeout):
                    nodes.append(
                        VirtualMeshNode(
                            place_id=place_id,
                            host="127.0.0.1",
                            port=port,
                            ssh_args=[
                                "-o",
                                "StrictHostKeyChecking=no",
                                "-o",
                                "UserKnownHostsFile=/dev/null",
                                "-p",
                                str(port),
                            ],
                        )
                    )
                    logger.info("VM %d ready (127.0.0.1:%d)", i, port)
                else:
                    log_path = qemu_logs[i - 1]
                    tail = ""
                    if log_path.exists():
                        with open(log_path) as f:
                            tail = f.read()[-2000:]
                    logger.error(
                        "VM %d: SSH failed after %ds. Last log:\n%s", i, timeout, tail
                    )
                    raise RuntimeError(f"VM {place_id}: SSH timeout after {timeout}s")

        if status_file:
            with open(status_file, "w") as f:
                json.dump([n.to_dict() for n in nodes], f, indent=2)
            logger.info("Wrote status to %s", status_file)

        return nodes, do_cleanup

    except Exception:
        do_cleanup()
        raise


def main() -> int:
    """CLI entry point for virtual mesh launcher."""
    import argparse

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    parser = argparse.ArgumentParser(description="Launch virtual mesh for testing")
    parser.add_argument("--nodes", "-n", type=int, default=None, help="Number of nodes")
    parser.add_argument("--image", "-i", required=True, help="Path to LibreMesh image")
    parser.add_argument("--status-file", "-o", help="Write node status JSON here")
    parser.add_argument("--ssh-base-port", type=int, default=None)
    parser.add_argument("--boot-timeout", type=int, default=None)
    args = parser.parse_args()

    n_nodes = args.nodes or int(os.environ.get("VIRTUAL_MESH_NODES", "3"))
    image = args.image or os.environ.get("VIRTUAL_MESH_IMAGE", "")
    if not image:
        print("ERROR: --image or VIRTUAL_MESH_IMAGE required", file=sys.stderr)
        return 1

    try:
        nodes, _cleanup = launch_virtual_mesh(
            n_nodes=n_nodes,
            image_path=image,
            ssh_base_port=args.ssh_base_port,
            boot_timeout=args.boot_timeout,
            status_file=args.status_file,
        )
        print(f"Launched {len(nodes)} nodes")
        for n in nodes:
            print(f"  {n.place_id}: ssh root@{n.host} -p {n.port}")
        return 0
    except (ValueError, RuntimeError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
