# Switch abstraction - dynamic VLAN switching

Optional integration with [labgrid-switch-abstraction](https://github.com/fcefyn-testbed/labgrid-switch-abstraction) for dynamic per-port VLAN management during multi-node tests.

## Overview

Multi-node tests (WiFi speed, L2/L3 connectivity) require DUTs to share a VLAN. In labs where each DUT is isolated on its own VLAN by default, the test fixtures can dynamically reconfigure the managed switch before tests and restore the original topology afterward.

This is **opt-in**: labs without managed switches, or with statically pre-configured VLANs, can skip this entirely.

## Installation

```bash
pip install .[switch]
```

Or install the dependency directly:

```bash
pip install git+https://github.com/fcefyn-testbed/labgrid-switch-abstraction.git
```

## Configuration

Two config files are required on the lab host running the tests:

### 1. Switch credentials (`~/.config/switch.conf`)

```ini
SWITCH_HOST=192.168.0.1
SWITCH_USER=admin
SWITCH_PASSWORD=secret
SWITCH_DRIVER=tplink_jetstream      # or: openwrt
SWITCH_DEVICE_TYPE=tplink_jetstream  # or: linux
```

Supported drivers:

| Switch type | `SWITCH_DRIVER` | `SWITCH_DEVICE_TYPE` |
|---|---|---|
| TP-Link JetStream (CLI) | `tplink_jetstream` | `tplink_jetstream` |
| OpenWrt / UCI (e.g. Zyxel GS1900) | `openwrt` | `linux` |

### 2. DUT hardware map (`/etc/testbed/dut-config.yaml`)

Override path via `SWITCH_DUT_CONFIG` env var.

```yaml
switch:
  host: "192.168.0.1"
  uplink_ports: [9, 10]
  vlan_topology: 200       # shared VLAN for multi-node tests

duts:
  my_router_1:
    switch_port: 1
    switch_vlan_isolated: 101
  my_router_2:
    switch_port: 2
    switch_vlan_isolated: 102
```

DUT names must match the labgrid place names (after stripping the lab prefix).

## Enabling VLAN switching

Set the environment variable before running tests:

```bash
export VLAN_SWITCH_ENABLED=1
```

Without this variable (or set to `0`), VLAN switching is completely skipped. This is the default.

## How it works

### Multi-node tests (`LG_MULTI_PLACES`)

When `LG_MULTI_PLACES=place_a,place_b` is set, the `shared_vlan_multi` fixture:

1. Reads the shared VLAN ID from `dut-config.yaml` (`switch.vlan_topology`, default 200)
2. Switches all listed DUT ports to that VLAN in a **single SSH session** (batch operation)
3. Yields control to tests
4. Restores all ports to their isolated VLANs on teardown

### Single-DUT tests (`@pytest.mark.shared_vlan`)

For individual tests that need VLAN switching, mark them:

```python
@pytest.mark.shared_vlan
def test_something_on_shared_network(ssh_command):
    ...
```

The `shared_vlan_single` fixture activates only for marked tests when `LG_PLACE` is set.

## Place name mapping

The fixtures extract DUT names from labgrid place names by stripping the lab prefix. The default heuristic splits on `-` and takes the third part:

```
labgrid-mylab-router_1  ->  router_1
```

Override with the `PLACE_PREFIX` env var if your lab uses a different convention:

```bash
export PLACE_PREFIX="labgrid-mylab-"
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "VLAN switching disabled" in logs | `VLAN_SWITCH_ENABLED` not set | `export VLAN_SWITCH_ENABLED=1` |
| "labgrid-switch-abstraction not installed" | Missing package | `pip install .[switch]` |
| "DUT not in dut-config.yaml" | Name mismatch | Check `PLACE_PREFIX` and DUT names in config |
| Multiple SSH connections (slow) | Old switch-abstraction version | Update: `pip install -U labgrid-switch-abstraction` |
