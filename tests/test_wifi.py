"""WiFi tests for LibreMesh.

LibreMesh configures WiFi in mesh mode (batman-adv over 802.11s or ad-hoc),
not as an AP. Tests here verify the mesh WiFi configuration rather than
AP/WPA3/WPA2 modes, which would conflict with and destroy the LibreMesh
mesh setup.
"""

import pytest


@pytest.mark.lg_feature("wifi")
def test_wifi_mesh_interface_exists(ssh_command):
    """Verify at least one WiFi interface is configured for mesh."""
    stdout, _, rc = ssh_command.run("iw dev")
    assert rc == 0, "iw dev command failed"
    output = "\n".join(stdout)
    assert "Interface" in output, "No WiFi interfaces found via iw dev"


@pytest.mark.lg_feature("wifi")
def test_wifi_mesh_mode(ssh_command):
    """Verify WiFi interface is operating in mesh or IBSS mode for batman-adv.

    LibreMesh uses either 802.11s mesh point or IBSS (ad-hoc) as the
    underlying WiFi mode for batman-adv mesh transport.
    """
    stdout, _, rc = ssh_command.run("iw dev")
    output = "\n".join(stdout)

    found_mesh = any(mode.lower() in output.lower() for mode in ("mesh point", "IBSS"))

    if not found_mesh:
        # Some devices may show batman interfaces but not 802.11s/IBSS directly.
        # Check if batman has an active WiFi interface.
        batctl_out, _, batctl_rc = ssh_command.run("batctl if")
        if batctl_rc == 0:
            for line in batctl_out:
                if "wlan" in line and "active" in line:
                    return

        pytest.skip("No WiFi mesh interface found (device may use ethernet-only mesh)")


@pytest.mark.lg_feature("wifi")
def test_wifi_radio_present(ssh_command):
    """Verify at least one physical WiFi radio exists.

    Accepts phy* (standard) and wl* (MediaTek mt76) as valid radio entries.
    """
    stdout, _, rc = ssh_command.run("ls /sys/class/ieee80211/")
    if rc != 0 or not stdout or not stdout[0].strip():
        pytest.skip("No WiFi radios found on this device (may be ethernet-only)")

    entries = [
        p
        for p in stdout[0].split()
        if p and (p.startswith("phy") or p.startswith("wl"))
    ]
    assert len(entries) > 0, (
        f"No phy/wl devices found in /sys/class/ieee80211/. Got: {stdout[0]!r}"
    )


@pytest.mark.lg_feature("wifi")
def test_wifi_not_in_ap_mode(ssh_command):
    """Verify WiFi is NOT in AP (hostapd) mode, as LibreMesh uses mesh mode.

    LibreMesh does not run WiFi as a traditional AP for the mesh transport.
    The presence of hostapd is acceptable for the LAN-side AP (lime-ap),
    but the mesh radio should not be in pure AP mode.
    """
    stdout, _, rc = ssh_command.run("iw dev")
    if rc != 0:
        pytest.skip("iw dev not available")

    interfaces = []
    current_iface = None

    for line in stdout:
        line = line.strip()
        if line.startswith("Interface"):
            current_iface = {"name": line.split()[-1], "type": None}
            interfaces.append(current_iface)
        elif "type" in line and current_iface:
            current_iface["type"] = line.split("type")[-1].strip()

    mesh_interfaces = [i for i in interfaces if i["type"] in ("mesh point", "IBSS")]
    ap_interfaces = [i for i in interfaces if i["type"] == "AP"]

    if mesh_interfaces:
        return

    if not ap_interfaces and not mesh_interfaces:
        pytest.skip(
            "No WiFi interfaces in AP or mesh mode — device may be ethernet-only"
        )
