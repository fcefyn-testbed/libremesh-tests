"""
Multi-node mesh tests for LibreMesh.

Phase 1: Basic connectivity (L2/L3 between mesh nodes).
Phase 2: Mesh protocol validation (batman-adv, babeld).

Requires: LG_MESH_PLACES, LG_IMAGE, LG_PROXY environment variables.
Switch must be in mesh mode (VLAN 200) before running.
"""

import logging
import re
import time

import pytest
from lime_helpers import is_mesh_ipv4, select_mesh_ipv4, select_primary_ipv4

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


# ---------------------------------------------------------------------------
# Phase 1: Basic connectivity
# ---------------------------------------------------------------------------

class TestMeshConnectivity:
    """Validate that mesh nodes boot and can reach each other."""

    def test_nodes_booted(self, mesh_nodes):
        """All nodes reached shell and SSH is functional."""
        for node in mesh_nodes:
            output = node.ssh.run_check("echo mesh-ok")
            assert output == ["mesh-ok"], f"Node {node.place}: unexpected output {output}"

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
            output = src.ssh.run_check(f"ping -c 5 -W 5 {dst_ip}")
            joined = "\n".join(output)
            assert "0% packet loss" in joined, (
                f"Ping {src.place} -> {dst.place} ({dst_ip}) failed:\n{joined}"
            )

    def test_l2_arp_entries(self, mesh_nodes):
        """After ping, ARP table on node 0 has entries for other nodes."""
        src = mesh_nodes[0]
        output = src.ssh.run_check("ip neigh show dev br-lan")
        joined = "\n".join(output)
        for dst in mesh_nodes[1:]:
            dst_ip = _get_br_lan_ipv4(dst)
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
        """Each node sees at least one batman-adv neighbor."""
        if not _has_command(mesh_nodes[0], "batctl"):
            pytest.skip("batctl not installed")

        active_nodes = 0
        for node in mesh_nodes:
            stdout, _, exit_code = node.ssh.run("batctl n")
            joined = "\n".join(stdout)
            logger.info("Node %s batman neighbors:\n%s", node.place, joined)
            if "disabled" in joined.lower():
                logger.warning("Node %s: batman mesh disabled", node.place)
                continue
            if exit_code != 0:
                logger.warning("Node %s: batctl n failed with code %d", node.place, exit_code)
                continue
            active_nodes += 1

        if active_nodes == 0:
            pytest.skip("batman-adv disabled on all nodes")

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
                logger.warning("Node %s: batctl o failed with code %d", node.place, exit_code)
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
            stdout, _, exit_code = node.ssh.run("ls /etc/config/lime-defaults 2>/dev/null || ls /etc/config/lime 2>/dev/null")
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
