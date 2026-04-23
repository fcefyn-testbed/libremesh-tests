# How to contribute a lab to LibreMesh testing

This guide is for lab administrators who want DUTs to appear in **libremesh-tests** CI and mesh matrices.

**Split of responsibilities**

| Topic | Where it lives |
|-------|----------------|
| Labgrid coordinator, exporter, TFTP, Ansible playbooks, per-lab `exporter/` files | [aparcar/openwrt-tests](https://github.com/aparcar/openwrt-tests) (`ansible/playbook_labgrid.yml`, `ansible/files/exporter/<lab>/`) |
| Global `labnet.yaml` (devices, labs, `developers`, inventory for coordinators) | **openwrt-tests** [`labnet.yaml`](https://github.com/aparcar/openwrt-tests/blob/main/labnet.yaml) |
| FCEFyN host-only roles (Arduino relay, PoE, ZeroTier, etc.) | [fcefyn_testbed_utils](https://github.com/fcefyn-testbed/fcefyn_testbed_utils) (`ansible/playbook_testbed.yml`) |
| This repo | `targets/*.yaml` only for boards LibreMesh tests run against here |

**libremesh-tests** does not ship `ansible/` or a copy of `labnet.yaml`. Deploy infrastructure from **openwrt-tests** first. The suite reads inventory via `LABNET_PATH`, `OPENWRT_TESTS_DIR`, or a **sibling** `../openwrt-tests/labnet.yaml` (see [README](../README.md) setup).

**Switch / VLAN automation:** labs with isolated-per-DUT VLANs (hybrid mode) need [labgrid-switch-abstraction](https://github.com/fcefyn-testbed/labgrid-switch-abstraction) on the lab host. Once the [openwrt-tests `playbook_labgrid.yml`](https://github.com/aparcar/openwrt-tests/blob/main/ansible/playbook_labgrid.yml) is applied, the `switch-vlan` CLI is installed via pipx automatically for all labs (labs that do not configure `switch.conf` simply leave it unused). Switch credentials should live in `/etc/switch.conf` (system-wide, group-readable) so any SSH user can run the command - see [fcefyn_testbed_utils switch-config: Multi-user setup](https://github.com/fcefyn-testbed/fcefyn_testbed_utils/blob/main/docs/configuracion/switch-config.md#multi-user-setup-recommended-for-labs-with-remote-devs). The test suite invokes `switch-vlan` remotely via `LG_PROXY` SSH; developers do not need the package, a `dut-config.yaml`, nor switch credentials locally.

**Place-to-DUT name mapping:** by default the suite strips the `labgrid-fcefyn-` prefix (FCEFyN convention). Set `PLACE_PREFIX` to match your own lab (for example `export PLACE_PREFIX="labgrid-mylab-"`); set `PLACE_PREFIX=""` to fall back to stripping up to the second hyphen (`labgrid-<lab>-<dut>` -> `<dut>`). The names that remain after stripping must match the DUT keys in the lab host's `dut-config.yaml`.

## Prerequisites

- Labgrid host (coordinator + exporter) with VLAN-capable switch
- Serial console access to DUTs
- Power control (relay/PDU or PoE switch)
- TFTP server for firmware boot

Manual SSH to DUTs on the FCEFyN lab (VLAN binding, aliases) is documented in [fcefyn_testbed_utils: SSH access to DUTs](https://github.com/fcefyn-testbed/fcefyn_testbed_utils/blob/main/docs/operar/dut-ssh-access.md).

## Steps

### 1. Register in upstream openwrt-tests

Add your lab and devices to [openwrt-tests `labnet.yaml`](https://github.com/aparcar/openwrt-tests/blob/main/labnet.yaml) and open a PR there so the global coordinator and upstream CI can see your DUTs.

Follow upstream docs for exporter files and Ansible:

- [Sharing target files / device_instances](https://github.com/aparcar/openwrt-tests/blob/main/docs/sharing-target-files.md)

### 2. Target YAML in this repo when needed

Add a `targets/<device>.yaml` **only** if **libremesh-tests** runs on that board; otherwise rely on upstream targets and openwrt-tests for generic runs.

Place names must match the exporter (`labgrid-<lab>-<instance>`).

### 3. SSH keys for developers

In **openwrt-tests** `labnet.yaml`, under `developers`, add SSH public keys for people who should run tests against your lab (same pattern as upstream coordinator users).

### 4. Verify

With `openwrt-tests` cloned next to **libremesh-tests** (or `OPENWRT_TESTS_DIR` set):

```bash
uv run labgrid-client lock
uv run labgrid-client power cycle
uv run pytest tests/test_libremesh.py -v --log-cli-level=CONSOLE
uv run labgrid-client unlock
```

## libremesh-only vs hybrid

| Lab type | Topology | Notes |
|----------|----------|-------|
| **libremesh-only** | Mesh VLAN shared | Simpler exporter |
| **hybrid** | Isolated VLANs + mesh VLAN 200 | FCEFyN-style; switch automation via `labgrid-switch-abstraction` in tests |

For hybrid setup details, contact maintainers or see **fcefyn_testbed_utils** design docs.
