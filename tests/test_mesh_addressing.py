import hashlib

import pytest

import conftest_mesh
import lime_helpers


class DummyShell:
    def __init__(self):
        self.added = {}
        self.commands = []
        self.rc_overrides = {}

    def run(self, command):
        self.commands.append(command)

        if command in self.rc_overrides:
            rc = self.rc_overrides[command]
            return [], [], rc

        if command.startswith("ip addr show ") and "grep -q" in command:
            prefix = "ip addr show "
            iface = command[len(prefix):].split(" |", 1)[0]
            ip_addr = command.rsplit("'", 2)[1]
            return [], [], 0 if self.added.get(iface) == ip_addr else 1

        if command.startswith("ip addr add "):
            parts = command.split()
            ip_addr = parts[3].split("/")[0]
            iface = parts[5]
            self.added[iface] = ip_addr
            return [], [], 0

        if command.startswith("ip addr show ") and "grep '" in command:
            prefix = "ip addr show "
            iface = command[len(prefix):].split(" |", 1)[0]
            ip_addr = command.rsplit("'", 2)[1]
            return [f"inet {ip_addr}/16"], [], 0 if self.added.get(iface) == ip_addr else 1

        return [], [], 0


def _expected_hash_ip(place_name: str) -> str:
    md5_hash = hashlib.md5(place_name.encode()).hexdigest()
    hash_value = int(md5_hash[:8], 16) % 253 + 1
    return f"{lime_helpers.MESH_SSH_IP_PREFIX}.{hash_value}"


def test_configure_fixed_ip_prefers_explicit_fixed_ip(monkeypatch):
    shell = DummyShell()
    monkeypatch.setattr(lime_helpers.time, "sleep", lambda _: None)
    monkeypatch.setattr(lime_helpers, "find_lan_interface", lambda _: "br-lan")
    monkeypatch.setattr(lime_helpers, "get_ssh_target_ip", lambda _: "192.168.1.1")
    monkeypatch.setattr(lime_helpers, "_start_ip_watchdog", lambda *args, **kwargs: None)

    result = lime_helpers.configure_fixed_ip(
        shell,
        target=object(),
        place_name="labgrid-fcefyn-openwrt_one",
        fixed_ip="10.13.200.77",
        prefer_networkservice=False,
    )

    assert result == "10.13.200.77"
    assert shell.added["br-lan"] == "10.13.200.77"


def test_configure_fixed_ip_prefers_networkservice_by_default(monkeypatch):
    shell = DummyShell()
    monkeypatch.setattr(lime_helpers.time, "sleep", lambda _: None)
    monkeypatch.setattr(lime_helpers, "find_lan_interface", lambda _: "br-lan")
    monkeypatch.setattr(lime_helpers, "get_ssh_target_ip", lambda _: "192.168.1.1")
    monkeypatch.setattr(lime_helpers, "_start_ip_watchdog", lambda *args, **kwargs: None)

    result = lime_helpers.configure_fixed_ip(
        shell,
        target=object(),
        place_name="labgrid-fcefyn-openwrt_one",
    )

    assert result == "192.168.1.1"
    assert shell.added["br-lan"] == "192.168.1.1"


def test_configure_fixed_ip_can_bypass_networkservice(monkeypatch):
    shell = DummyShell()
    place = "labgrid-fcefyn-openwrt_one"
    monkeypatch.setattr(lime_helpers.time, "sleep", lambda _: None)
    monkeypatch.setattr(lime_helpers, "find_lan_interface", lambda _: "br-lan")
    monkeypatch.setattr(lime_helpers, "get_ssh_target_ip", lambda _: "192.168.1.1")
    monkeypatch.setattr(lime_helpers, "_start_ip_watchdog", lambda *args, **kwargs: None)

    result = lime_helpers.configure_fixed_ip(
        shell,
        target=object(),
        place_name=place,
        prefer_networkservice=False,
    )

    assert result == _expected_hash_ip(place)
    assert shell.added["br-lan"] == _expected_hash_ip(place)


def test_select_mesh_ipv4_prefers_real_mesh_address():
    addresses = [
        "10.13.200.15",
        "192.168.1.1",
        "10.13.146.192",
    ]

    assert lime_helpers.select_mesh_ipv4(addresses) == "10.13.146.192"


def test_select_mesh_ipv4_ignores_fixed_debug_range():
    addresses = [
        "10.13.200.15",
        "10.13.200.20",
    ]

    assert lime_helpers.select_mesh_ipv4(addresses) is None


def test_select_primary_ipv4_returns_first_unique_address():
    addresses = [
        "10.13.200.15",
        "10.13.200.15",
        "10.13.146.192",
    ]

    assert lime_helpers.select_primary_ipv4(addresses) == "10.13.200.15"


def test_build_mesh_ssh_ip_map_rejects_collisions(monkeypatch):
    monkeypatch.setattr(
        conftest_mesh,
        "generate_mesh_ssh_ip",
        lambda place: "10.13.200.42" if "one" in place or "two" in place else "10.13.200.43",
    )

    with pytest.raises(ValueError, match="Duplicate mesh SSH IP"):
        conftest_mesh._build_mesh_ssh_ip_map(["labgrid-one", "labgrid-two"])


def test_enable_batman_bridge_loop_avoidance_uses_first_supported_command():
    shell = DummyShell()
    shell.rc_overrides = {
        "batctl meshif bat0 bridge_loop_avoidance 1": 1,
        "batctl meshif bat0 bl 1": 0,
    }

    result = lime_helpers.enable_batman_bridge_loop_avoidance(shell)

    assert result is True
    assert shell.commands[:2] == [
        "batctl meshif bat0 bridge_loop_avoidance 1",
        "batctl meshif bat0 bl 1",
    ]


def test_compute_network_settle_timeout_scales_with_topology_size():
    assert conftest_mesh._compute_network_settle_timeout(2) == 90
    assert conftest_mesh._compute_network_settle_timeout(3) == 90
    assert conftest_mesh._compute_network_settle_timeout(4) == 120
    assert conftest_mesh._compute_network_settle_timeout(5) == 150
