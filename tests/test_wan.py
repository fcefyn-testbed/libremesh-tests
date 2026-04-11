"""WAN / upstream connectivity tests for LibreMesh.

In vanilla OpenWrt, the WAN port is the upstream internet connection.
In LibreMesh running from initramfs (RAM boot), WAN is typically not
configured as a separate interface — internet access goes through
whatever upstream route is available in the mesh.

These tests verify upstream connectivity rather than a specific WAN
interface, which is more appropriate for LibreMesh's architecture.
"""

import pytest


@pytest.mark.lg_feature("online")
def test_internet_reachability(ssh_command):
    """Verify the node can reach the internet via any available route.

    In LibreMesh, internet access is routed through the mesh (babeld/batman)
    rather than a dedicated WAN port. This test checks basic internet
    connectivity without assuming a specific WAN interface.
    """
    stdout, stderr, rc = ssh_command.run("ping -c 3 -W 5 8.8.8.8")
    assert rc == 0, (
        f"Cannot reach internet (8.8.8.8). stdout: {stdout}, stderr: {stderr}"
    )


@pytest.mark.lg_feature("online")
def test_dns_resolution(ssh_command):
    """Verify DNS resolution works through the mesh."""
    stdout, stderr, rc = ssh_command.run("nslookup openwrt.org")
    if rc != 0:
        stdout, stderr, rc = ssh_command.run("host openwrt.org")
    assert rc == 0, f"DNS resolution failed. stdout: {stdout}, stderr: {stderr}"


@pytest.mark.lg_feature("online")
def test_https_download(ssh_command):
    """Verify HTTPS download works (validates routing + TLS + DNS)."""
    try:
        stdout, stderr, rc = ssh_command.run(
            "wget -q -O /tmp/test_download.txt "
            "https://downloads.openwrt.org/releases/index.html"
        )
        assert rc == 0, f"wget failed with rc={rc}. stderr: {stderr}"

        content, _, cat_rc = ssh_command.run("head -1 /tmp/test_download.txt")
        assert cat_rc == 0 and content, "Downloaded file is empty or unreadable"
    finally:
        ssh_command.run("rm -f /tmp/test_download.txt")
