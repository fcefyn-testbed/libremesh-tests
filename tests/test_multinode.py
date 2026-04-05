"""
Multi-node tests for vanilla OpenWrt.

Phase 1: Basic connectivity (SSH, IPv4, L3 ping between nodes).
Phase 2: WiFi performance (iperf3 speed test with golden device).

Requires: LG_MULTI_PLACES, LG_IMAGE/LG_IMAGE_MAP environment variables.
For VLAN switching, also set VLAN_SWITCH_ENABLED=1 (see conftest_vlan.py).
"""

import logging
import re

import pytest

logger = logging.getLogger(__name__)

IPERF_DURATION = 10
IPERF_MIN_MBPS = 10.0


def _get_ipv4_addresses(node) -> list[str]:
    """Get all IPv4 addresses from a node (excluding loopback)."""
    output = node.ssh.run_check("ip -4 -o addr show")
    ips = []
    for line in output:
        if " lo " in line:
            continue
        match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
        if match:
            ips.append(match.group(1))
    return ips


def _get_first_ipv4(node) -> str:
    """Get the first non-loopback IPv4 address from a node."""
    ips = _get_ipv4_addresses(node)
    if not ips:
        pytest.fail(f"Node {node.place}: no IPv4 address found")
    return ips[0]


def _has_command(node, cmd: str) -> bool:
    _, _, exit_code = node.ssh.run(f"which {cmd}")
    return exit_code == 0


class TestMultiNodeConnectivity:
    """Validate that multi-node DUTs boot and can communicate."""

    def test_nodes_booted(self, multi_nodes):
        """All nodes reached shell and SSH is functional."""
        for node in multi_nodes:
            output = node.ssh.run_check("echo node-ok")
            assert output == ["node-ok"], (
                f"Node {node.place}: unexpected output {output}"
            )

    def test_nodes_have_ip(self, multi_nodes):
        """Each node has at least one non-loopback IPv4 address."""
        for node in multi_nodes:
            ips = _get_ipv4_addresses(node)
            logger.info("Node %s: IPs = %s", node.place, ips)
            assert ips, f"Node {node.place}: no IPv4 address"

    def test_l3_connectivity(self, multi_nodes):
        """First node can ping every other node via test subnet."""
        if len(multi_nodes) < 2:
            pytest.skip("Need at least 2 nodes for connectivity test")

        src = multi_nodes[0]
        for dst in multi_nodes[1:]:
            dst_ip = dst.ip
            if not dst_ip:
                pytest.fail(f"Node {dst.place}: no test IP assigned")
            logger.info("Ping %s (%s) -> %s (%s)", src.place, src.ip, dst.place, dst_ip)
            output = src.ssh.run_check(f"ping -c 5 -W 5 {dst_ip}")
            joined = "\n".join(output)
            assert "0% packet loss" in joined, (
                f"Ping {src.place} -> {dst.place} ({dst_ip}) failed:\n{joined}"
            )


@pytest.mark.lg_feature("wifi")
class TestWifiPerformance:
    """WiFi speed regression tests using iperf3.

    Uses the "golden device" pattern: the first node in LG_MULTI_PLACES
    acts as the iperf3 server (golden/reference device), and the second
    node is the DUT being tested.

    Both devices must be on the same VLAN and have WiFi connectivity.
    """

    def test_iperf3_available(self, multi_nodes):
        """iperf3 is installed on all nodes."""
        for node in multi_nodes:
            if not _has_command(node, "iperf3"):
                pytest.skip(f"iperf3 not installed on {node.place}")

    def test_wifi_interfaces_up(self, multi_nodes):
        """At least one wireless interface is UP on each node."""
        for node in multi_nodes:
            stdout, _, rc = node.ssh.run(
                "iw dev | grep -E '^\\s+Interface' | awk '{print $2}'"
            )
            if rc != 0 or not stdout:
                pytest.skip(f"No wireless interfaces on {node.place}")
            for iface in stdout:
                iface = iface.strip()
                out, _, _ = node.ssh.run(f"ip link show {iface}")
                joined = "\n".join(out)
                if "UP" in joined:
                    logger.info("Node %s: wifi iface %s is UP", node.place, iface)
                    break
            else:
                pytest.fail(f"Node {node.place}: no wireless interface is UP")

    def test_wifi_speed(self, multi_nodes):
        """iperf3 throughput between golden device (server) and DUT (client).

        The first node runs iperf3 in server mode, the second connects as
        client. Asserts minimum throughput threshold.
        """
        if len(multi_nodes) < 2:
            pytest.skip("Need at least 2 nodes for WiFi speed test")

        server = multi_nodes[0]
        client = multi_nodes[1]

        if not (_has_command(server, "iperf3") and _has_command(client, "iperf3")):
            pytest.skip("iperf3 not available on both nodes")

        server.ssh.run("killall iperf3 2>/dev/null; true")
        server.ssh.run("iperf3 -s -D")

        server_ip = server.ip
        if not server_ip:
            pytest.fail(f"Server node {server.place}: no test IP assigned")
        logger.info(
            "iperf3: %s (client) -> %s (server @ %s), %ds",
            client.place,
            server.place,
            server_ip,
            IPERF_DURATION,
        )

        try:
            output = client.ssh.run_check(
                f"iperf3 -c {server_ip} -t {IPERF_DURATION} -J"
            )
        finally:
            server.ssh.run("killall iperf3 2>/dev/null; true")

        import json

        raw = "\n".join(output)
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            pytest.fail(f"iperf3 output not valid JSON:\n{raw[:500]}")

        bits_per_second = (
            result.get("end", {}).get("sum_received", {}).get("bits_per_second", 0)
        )
        mbps = bits_per_second / 1_000_000

        logger.info("iperf3 result: %.2f Mbps", mbps)
        assert mbps >= IPERF_MIN_MBPS, (
            f"WiFi throughput {mbps:.2f} Mbps below threshold {IPERF_MIN_MBPS} Mbps"
        )
