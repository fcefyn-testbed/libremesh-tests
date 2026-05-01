"""LAN interface tests adapted for LibreMesh.

In LibreMesh, br-lan gets a dynamic 10.13.x.x IP (derived from MAC) instead of
the fixed 192.168.1.1 used by vanilla OpenWrt. Tests are adapted accordingly.
"""

import ipaddress
from time import sleep

import pytest


def test_lan_wait_for_link_ready(shell_command):
    for _ in range(60):
        if shell_command.run("dmesg | grep br-lan | grep forwarding")[2] == 0:
            return
        sleep(1)

    assert False, "LAN interface did not come up within 60 seconds"


def test_lan_wait_for_network(shell_command):
    """Wait for br-lan to have an IP address.

    LibreMesh assigns a 10.13.x.x IP to br-lan based on the device MAC.
    We wait for any IPv4 address to appear rather than checking a specific
    ubus interface name, since LibreMesh may use different interface names.
    """
    for _ in range(60):
        stdout, _, rc = shell_command.run("ip -4 -o addr show br-lan")
        if rc == 0 and stdout:
            for line in stdout:
                if "inet " in line:
                    return
        sleep(1)

    assert False, "br-lan did not get an IPv4 address within 60 seconds"


def test_lan_interface_address(shell_command):
    """Verify br-lan has a valid LibreMesh mesh IP (10.13.0.0/16)."""
    stdout, _, rc = shell_command.run("ip -4 -o addr show br-lan")
    assert rc == 0, "Failed to query br-lan addresses"

    mesh_network = ipaddress.IPv4Network("10.13.0.0/16")
    found_mesh_ip = False

    for line in stdout:
        parts = line.split()
        for part in parts:
            if part.startswith("10.13."):
                try:
                    addr = ipaddress.IPv4Interface(part)
                    if addr.ip in mesh_network:
                        found_mesh_ip = True
                        break
                except ValueError:
                    continue

    assert found_mesh_ip, (
        f"br-lan does not have a 10.13.x.x LibreMesh IP. Got: {stdout}"
    )


def test_lan_interface_has_neighbor(shell_command):
    """Verify there is at least one neighbor on the LAN segment.

    Pings the IPv6 link-local all-nodes multicast address. The DUT
    always answers itself, so any *additional* responder (different
    link-local source address) or a DUP! marker proves a peer is
    present on br-lan.

    This is inherently a multi-node check: in single-node QEMU runs
    (``test-firmware-qemu-single``) the only responder is the DUT
    itself and there is no peer to discover. In that case we
    ``pytest.skip`` so the test is meaningful when at least one peer
    exists (test-mesh-qemu, test-mesh, test-mesh-pairs) and a
    no-op otherwise, instead of a hard failure that flags healthy
    single-node images as broken.
    """
    stdout, _, rc = shell_command.run("ping -c 3 ff02::1%br-lan")
    output = "\n".join(stdout)

    responders = set()
    for line in stdout:
        if "bytes from" not in line:
            continue
        parts = line.split("from ")
        if len(parts) <= 1:
            continue
        addr_full = (
            parts[1].split("%br-lan")[0]
            if "%br-lan" in parts[1]
            else parts[1].split(":")[0]
        )
        responders.add(addr_full)

    has_neighbor = len(responders) >= 2 or "DUP!" in output

    if not has_neighbor:
        if len(responders) <= 1:
            pytest.skip(
                "No br-lan peer present (single-node deployment). "
                "Multi-node coverage lives in test-mesh-* jobs."
            )
        pytest.fail(
            f"Multiple ping responses but only {len(responders)} unique "
            f"source(s) and no DUP! marker. Output: {output}"
        )
