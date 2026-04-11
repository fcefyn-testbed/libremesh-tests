from contextlib import contextmanager
import threading
import time

import conftest_mesh
from strategies import tftpstrategy


class _FakeConfig:
    def get_image_path(self, name):
        assert name == "root"
        return "/tmp/fake-image.bin"


class _FakeEnv:
    config = _FakeConfig()


class _FakeTarget:
    def __init__(self):
        self.env = _FakeEnv()
        self.activated = []

    def activate(self, driver):
        self.activated.append(driver)


class _FakeTFTP:
    def stage(self, path):
        return f"staged:{path}"


class _FakePower:
    def __init__(self):
        self.cycles = 0

    def cycle(self):
        self.cycles += 1


class _FakeStrategy:
    def __init__(self):
        self.target = _FakeTarget()
        self.tftp = _FakeTFTP()
        self.console = object()
        self.uboot = object()
        self.power = _FakePower()
        self.status = None
        self.transition_calls = []
        self.prepared = []

    def transition(self, status):
        self.transition_calls.append(status)

    def _resolve_tftp_server_ip(self):
        return "192.168.200.1"

    def _prepare_uboot_commands(self, staged_file, tftp_server_ip):
        self.prepared.append((staged_file, tftp_server_ip))


def test_uboot_gate_serializes_access(tmp_path):
    lock_path = tmp_path / "uboot.lock"
    events = []
    release_first = threading.Event()
    second_entered = threading.Event()

    def first():
        with tftpstrategy._uboot_gate(str(lock_path)):
            events.append("first-enter")
            release_first.wait(timeout=2)
            events.append("first-exit")

    def second():
        time.sleep(0.05)
        with tftpstrategy._uboot_gate(str(lock_path)):
            events.append("second-enter")
            second_entered.set()

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()

    time.sleep(0.2)
    assert events == ["first-enter"]
    assert not second_entered.is_set()

    release_first.set()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert events == ["first-enter", "first-exit", "second-enter"]


def test_transition_to_uboot_once_uses_configured_gate(monkeypatch):
    seen = []

    @contextmanager
    def fake_gate(path):
        seen.append(path)
        yield

    fake = _FakeStrategy()
    monkeypatch.setattr(tftpstrategy, "_uboot_gate", fake_gate)
    monkeypatch.setenv("LG_MESH_UBOOT_LOCK_FILE", "/tmp/test-uboot.lock")

    tftpstrategy.UBootTFTPStrategy._transition_to_uboot_once(fake)

    assert seen == ["/tmp/test-uboot.lock"]
    assert fake.transition_calls == [tftpstrategy.Status.off]
    assert fake.power.cycles == 1
    assert fake.prepared == [("staged:/tmp/fake-image.bin", "192.168.200.1")]
    assert fake.status == tftpstrategy.Status.uboot


def test_read_status_file_tolerates_incomplete_json(tmp_path):
    status_file = tmp_path / "status.json"
    status_file.write_text("{", encoding="utf-8")

    assert conftest_mesh._read_status_file(str(status_file)) is None

    status_file.write_text('{"ok": true, "ssh_ip": "10.13.200.1"}', encoding="utf-8")

    assert conftest_mesh._read_status_file(str(status_file)) == {
        "ok": True,
        "ssh_ip": "10.13.200.1",
    }


def test_tail_boot_log_returns_last_non_empty_line(tmp_path):
    log_file = tmp_path / "boot.log"
    log_file.write_text("line one\n\nline two\n", encoding="utf-8")

    assert conftest_mesh._tail_boot_log(str(log_file)) == "line two"
