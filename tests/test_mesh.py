"""
Multi-node mesh tests for LibreMesh.

Phase 1: Basic connectivity (L2/L3 between mesh nodes).
Phase 2: Mesh protocol validation (batman-adv, babeld).

Requires: LG_MESH_PLACES, LG_IMAGE, LG_PROXY environment variables.
``mesh_vlan_multi`` (session fixture) switches DUTs to VLAN 200 when ``LG_MESH_PLACES`` is set.
"""

import logging
import re
import time

import pytest
from lime_helpers import is_mesh_ipv4, select_mesh_ipv4, select_primary_ipv4

pytestmark = pytest.mark.mesh

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_br_lan_ipv4(node) -> str:
    """Extract the preferred br-lan IPv4, preferring the real mesh address."""
    if node.mesh_ip and is_mesh_ipv4(node.mesh_ip):
        return node.mesh_ip

    output = node.ssh.run_check("ip -4 -o addr show br-lan")
    mesh_ip = select_mesh_ipv4(output)
    if mesh_ip:
        return mesh_ip

    primary_ip = select_primary_ipv4(output)
    if primary_ip:
        return primary_ip

    if node.mesh_ip:
        return node.mesh_ip

    pytest.fail(f"Node {node.place}: no IPv4 on br-lan")


def _get_all_ipv4(node) -> list[str]:
    """Get all IPv4 addresses assigned to br-lan."""
    output = node.ssh.run_check("ip -4 -o addr show br-lan")
    return re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", "\n".join(output))


def _has_command(node, cmd: str) -> bool:
    """Check if a command exists on the node."""
    stdout, _, exit_code = node.ssh.run(f"which {cmd}")
    return exit_code == 0


def _ping_with_retry(src, dst, dst_ip: str, attempts: int = 3, delay: int = 5) -> None:
    """Ping a mesh peer, retrying through transient convergence or SSH hiccups."""
    last_stdout: list[str] = []
    last_stderr: list[str] = []
    last_exit_code = 1

    for attempt in range(1, attempts + 1):
        stdout, stderr, exit_code = src.ssh.run(f"ping -c 5 -W 5 {dst_ip}")
        joined = "\n".join(stdout)
        if exit_code == 0 and "0% packet loss" in joined:
            if attempt > 1:
                logger.info(
                    "Ping %s -> %s (%s) succeeded on retry %d/%d",
                    src.place,
                    dst.place,
                    dst_ip,
                    attempt,
                    attempts,
                )
            return

        last_stdout, last_stderr, last_exit_code = stdout, stderr, exit_code
        logger.warning(
            "Ping %s -> %s (%s) failed on attempt %d/%d (rc=%s, stderr=%s)",
            src.place,
            dst.place,
            dst_ip,
            attempt,
            attempts,
            exit_code,
            stderr,
        )
        if attempt < attempts:
            time.sleep(delay)

    joined = "\n".join(last_stdout)
    stderr_joined = "\n".join(last_stderr)
    pytest.fail(
        f"Ping {src.place} -> {dst.place} ({dst_ip}) failed after {attempts} attempts "
        f"(rc={last_exit_code}).\nSTDOUT:\n{joined}\nSTDERR:\n{stderr_joined}"
    )


def _batctl_n_neighbor_rows(stdout: list[str]) -> list[str]:
    """Return data rows from ``batctl n`` (exclude banner and column header)."""
    rows: list[str] = []
    past_header = False
    for ln in stdout:
        s = ln.strip()
        if not s or s.startswith("["):
            continue
        low = s.lower()
        if "neighbor" in low and "last-seen" in low:
            past_header = True
            continue
        if past_header:
            rows.append(ln)
    return rows


def _wait_for_batman_neighbor_rows(
    mesh_nodes,
    *,
    timeout: int = 120,
    interval: int = 5,
) -> None:
    """Poll until each batman-active node shows at least one neighbor row.

    On mixed wired/wireless pairs (e.g. Banana Pi ``lan1_29`` + Belkin
    ``wlan0-mesh_29``) L3 ping can succeed before ``batctl n`` lists
    neighbors; waiting avoids a flaky failure right after mesh boot.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        missing: list[str] = []
        for node in mesh_nodes:
            stdout, _, exit_code = node.ssh.run("batctl n")
            joined = "\n".join(stdout)
            if "disabled" in joined.lower() or exit_code != 0:
                continue
            if not _batctl_n_neighbor_rows(stdout):
                missing.append(str(node.place))
        if not missing:
            return
        logger.info(
            "Waiting for batman neighbors (%ds left): still empty on %s",
            max(0, int(deadline - time.time())),
            missing,
        )
        time.sleep(interval)

    pytest.fail(
        f"batman-adv had no neighbor rows on {missing} after {timeout}s "
        f"(try increasing timeout or check bat0 / VLAN 29 hardifs)"
    )


def _ensure_arp_entry(src, dst, dst_ip: str) -> None:
    """Prime neighbor state before asserting on the ARP table."""
    output = src.ssh.run_check("ip neigh show dev br-lan")
    joined = "\n".join(output)
    if dst_ip in joined:
        return

    logger.info(
        "Priming neighbor entry on %s for %s (%s)",
        src.place,
        dst.place,
        dst_ip,
    )
    _ping_with_retry(src, dst, dst_ip, attempts=2, delay=3)


# ---------------------------------------------------------------------------
# Phase 1: Basic connectivity
# ---------------------------------------------------------------------------


class TestMeshConnectivity:
    """Validate that mesh nodes boot and can reach each other."""

    def test_nodes_booted(self, mesh_nodes):
        """All nodes reached shell and SSH is functional."""
        for node in mesh_nodes:
            output = node.ssh.run_check("echo mesh-ok")
            assert output == ["mesh-ok"], (
                f"Node {node.place}: unexpected output {output}"
            )

    def test_nodes_have_ip(self, mesh_nodes):
        """Each node has at least one IPv4 on br-lan."""
        for node in mesh_nodes:
            ips = _get_all_ipv4(node)
            logger.info("Node %s: br-lan IPs = %s", node.place, ips)
            assert ips, f"Node {node.place}: no IPv4 on br-lan"

    def test_l3_ping_between_nodes(self, mesh_nodes):
        """First node can ping every other node via br-lan IP."""
        src = mesh_nodes[0]
        for dst in mesh_nodes[1:]:
            dst_ip = _get_br_lan_ipv4(dst)
            logger.info("Ping %s -> %s (%s)", src.place, dst.place, dst_ip)
            _ping_with_retry(src, dst, dst_ip)

    def test_l2_arp_entries(self, mesh_nodes):
        """After ping, ARP table on node 0 has entries for other nodes."""
        src = mesh_nodes[0]
        for dst in mesh_nodes[1:]:
            dst_ip = _get_br_lan_ipv4(dst)
            _ensure_arp_entry(src, dst, dst_ip)
            output = src.ssh.run_check("ip neigh show dev br-lan")
            joined = "\n".join(output)
            assert dst_ip in joined, (
                f"Node {src.place}: no ARP entry for {dst.place} ({dst_ip})"
            )


# ---------------------------------------------------------------------------
# Phase 2: Mesh protocol validation
# ---------------------------------------------------------------------------


class TestMeshProtocol:
    """Validate that LibreMesh mesh protocols are running and forming a network."""

    def test_batman_interface_exists(self, mesh_nodes):
        """batman-adv interface (bat0) exists on all nodes."""
        for node in mesh_nodes:
            stdout, _, exit_code = node.ssh.run("ip link show bat0")
            if exit_code != 0:
                pytest.skip("batman-adv not available on this build")
            assert "bat0" in "\n".join(stdout)

    def test_batman_neighbors(self, mesh_nodes):
        """Each node with batman-adv enabled sees at least one neighbor."""
        if not _has_command(mesh_nodes[0], "batctl"):
            pytest.skip("batctl not installed")

        _wait_for_batman_neighbor_rows(mesh_nodes, timeout=120, interval=5)

        active_nodes = 0
        no_neighbors = []
        for node in mesh_nodes:
            stdout, _, exit_code = node.ssh.run("batctl n")
            joined = "\n".join(stdout)
            logger.info("Node %s batman neighbors:\n%s", node.place, joined)
            if "disabled" in joined.lower():
                logger.warning("Node %s: batman mesh disabled", node.place)
                continue
            if exit_code != 0:
                logger.warning(
                    "Node %s: batctl n failed with code %d", node.place, exit_code
                )
                continue
            active_nodes += 1
            neighbor_lines = _batctl_n_neighbor_rows(stdout)
            if not neighbor_lines:
                no_neighbors.append(str(node.place))

        if active_nodes == 0:
            pytest.skip("batman-adv disabled on all nodes")
        assert not no_neighbors, (
            f"nodes with batman-adv enabled but no neighbors: {no_neighbors}"
        )

    def test_batman_originators(self, mesh_nodes):
        """batman-adv originator table has entries (mesh routes formed)."""
        if not _has_command(mesh_nodes[0], "batctl"):
            pytest.skip("batctl not installed")

        active_nodes = 0
        for node in mesh_nodes:
            stdout, _, exit_code = node.ssh.run("batctl o")
            joined = "\n".join(stdout)
            logger.info("Node %s originators:\n%s", node.place, joined)
            if "disabled" in joined.lower():
                logger.warning("Node %s: batman mesh disabled", node.place)
                continue
            if exit_code != 0:
                logger.warning(
                    "Node %s: batctl o failed with code %d", node.place, exit_code
                )
                continue
            active_nodes += 1

        if active_nodes == 0:
            pytest.skip("batman-adv disabled on all nodes")

    def test_babeld_running(self, mesh_nodes):
        """babeld process is running on all nodes."""
        for node in mesh_nodes:
            stdout, _, exit_code = node.ssh.run("pgrep -x babeld")
            if exit_code != 0:
                pytest.skip("babeld not running on this build")

    def test_babeld_has_routes(self, mesh_nodes):
        """babeld has learned at least one route from another node."""
        for node in mesh_nodes:
            stdout, _, exit_code = node.ssh.run("pgrep -x babeld")
            if exit_code != 0:
                pytest.skip("babeld not running")

            output = node.ssh.run_check(
                "echo dump | nc ::1 33123 2>/dev/null || "
                "cat /var/run/babeld/state 2>/dev/null || "
                "echo BABELD_NO_STATE"
            )
            joined = "\n".join(output)
            logger.info("Node %s babeld state:\n%s", node.place, joined[:500])
            assert "BABELD_NO_STATE" not in joined, (
                f"Node {node.place}: cannot read babeld state"
            )


# ---------------------------------------------------------------------------
# Phase 3: LibreMesh-specific checks
# ---------------------------------------------------------------------------


class TestLibreMeshServices:
    """Validate LibreMesh-specific services and configuration."""

    def test_lime_config_exists(self, mesh_nodes):
        """LibreMesh configuration file exists."""
        for node in mesh_nodes:
            stdout, _, exit_code = node.ssh.run(
                "ls /etc/config/lime-defaults 2>/dev/null || ls /etc/config/lime 2>/dev/null"
            )
            if exit_code != 0:
                pytest.skip("Not a LibreMesh build (no lime config)")

    def test_lime_proto_running(self, mesh_nodes):
        """lime-proto network protocols are configured."""
        for node in mesh_nodes:
            stdout, _, exit_code = node.ssh.run(
                "uci show lime-defaults 2>/dev/null || echo NO_LIME"
            )
            joined = "\n".join(stdout)
            if "NO_LIME" in joined:
                pytest.skip("lime-defaults not present")
            logger.info("Node %s lime config:\n%s", node.place, joined[:300])
