# Switch abstraction - dynamic VLAN switching

Optional integration with [labgrid-switch-abstraction](https://github.com/fcefyn-testbed/labgrid-switch-abstraction) for dynamic per-port VLAN management during multi-node tests.

When enabled, test fixtures reconfigure the managed switch before tests (moving DUTs to a shared VLAN) and restore the original topology afterward. This is **opt-in**: labs without managed switches can skip it.

## Setup

1. Install the dependency:

```bash
pip install .[switch]
```

2. Configure the switch credentials and DUT hardware map on the lab host. See the [labgrid-switch-abstraction README](https://github.com/fcefyn-testbed/labgrid-switch-abstraction#readme) for full configuration reference (`switch.conf`, `dut-config.yaml`).

3. Enable VLAN switching:

```bash
export VLAN_SWITCH_ENABLED=1
```

Without this variable (or set to `0`), VLAN switching is completely skipped.

## How it works

When `LG_MULTI_PLACES` is set, the `shared_vlan_multi` fixture (session-scoped) switches all listed DUT ports to the shared VLAN (`switch.vlan_topology` from `dut-config.yaml`, default 200) in a single SSH session. Isolated VLANs are restored on teardown.

## Place name mapping

DUT names are extracted from labgrid place names by stripping the lab prefix (everything up to the second hyphen):

```
labgrid-mylab-router_1  ->  router_1
```

Override with `PLACE_PREFIX` env var if the lab uses a different convention:

```bash
export PLACE_PREFIX="labgrid-mylab-"
```
