# libremesh-tests

Pytest suite for **LibreMesh** on real hardware (labgrid) and **virtual mesh** (QEMU + vwifi). It is intentionally **not** a full fork of [openwrt-tests](https://github.com/aparcar/openwrt-tests): this repo keeps mesh-related tests, FCEFyN target YAML, and custom strategies. The shared device/lab inventory is **`labnet.yaml` in [openwrt-tests](https://github.com/aparcar/openwrt-tests)** (not duplicated here).

**Vanilla OpenWrt healthchecks** (apk/opkg, stock assumptions) run in **openwrt-tests** against the same global coordinator. **LibreMesh single-node, multi-node mesh, and LiMe QEMU** run here.

## Requirements

- Python 3.13+ and [`uv`](https://docs.astral.sh/uv/)
- On the lab host: labgrid client, coordinator, exporter (deployed from **openwrt-tests** Ansible; see below)
- For QEMU: `qemu-system-x86_64` and related packages (see virtual mesh docs)

## Setup

```shell
cd /path/to/libremesh-tests
uv sync
```

**`labnet.yaml` location:** pytest resolves the inventory file in this order: `LABNET_PATH` (explicit file), `OPENWRT_TESTS_DIR/labnet.yaml`, or a **sibling clone** `../openwrt-tests/labnet.yaml` (recommended layout: check out **aparcar/openwrt-tests** next to this repository). For ad-hoc or CI use without that layout, set `LABNET_PATH` or `OPENWRT_TESTS_DIR`.

**Dynamic VLAN switching:** Multi-node mesh tests only (`LG_MESH_PLACES` set) run `switch-vlan` to move DUTs to mesh VLAN (200) on the **lab host** via SSH (the same host as `LG_PROXY`). Single-node tests keep each DUT on its isolated exporter VLAN. No local `dut-config.yaml` or `labgrid-switch-abstraction` install is needed on the developer machine—only on the lab host. If VLANs are already correct, `VLAN_SWITCH_DISABLED=1` skips switch commands.

Place-to-DUT name mapping defaults to the FCEFyN convention (strips `labgrid-fcefyn-`). For other labs, override with `PLACE_PREFIX` (e.g. `export PLACE_PREFIX="labgrid-mylab-"`, or `PLACE_PREFIX=""` to fall back to stripping up to the second hyphen).

Lab infrastructure (coordinator, exporter, TFTP, Ansible) is documented in **openwrt-tests** and **fcefyn_testbed_utils**; this repo does not ship `ansible/`.

## Targets shipped here

Only DUTs used for LibreMesh / FCEFyN workflows:

| File | Role |
|------|------|
| `targets/openwrt_one.yaml` | OpenWrt One |
| `targets/bananapi_bpi-r4.yaml` | Banana Pi R4 |
| `targets/linksys_e8450.yaml` | Belkin RT3200 / Linksys E8450 |
| `targets/librerouter_librerouter-v1.yaml` | LibreRouter v1 |
| `targets/qemu_x86-64_libremesh.yaml` | QEMU x86-64 LibreMesh |

For other boards or vanilla OpenWrt QEMU, use **openwrt-tests** `targets/` and run upstream tests there.

## Running tests

### Single-node LibreMesh (physical DUT)

```shell
export LG_PLACE=labgrid-fcefyn-belkin_rt3200_2
export LG_PROXY=labgrid-fcefyn
export LG_IMAGE=/srv/tftp/firmwares/.../lime-....bin

uv run labgrid-client lock
uv run pytest tests/test_libremesh.py --log-cli-level=CONSOLE -v
uv run labgrid-client unlock
```

`LG_IMAGE` must be a real file on the machine running labgrid-client (see remote access docs in fcefyn_testbed_utils).

### Single-node on QEMU (LibreMesh image)

```shell
uv run pytest tests/test_base.py tests/test_lan.py tests/test_libremesh.py \
  --lg-env targets/qemu_x86-64_libremesh.yaml \
  --firmware /path/to/lime-x86-64-generic-ext4-combined.img \
  --lg-log --log-cli-level=CONSOLE --lg-colored-steps -v
```

### Multi-node mesh (physical)

Requires `LG_MESH_PLACES`, images, and mesh VLAN (see `tests/conftest_mesh.py`).

```shell
export LG_MESH_PLACES="labgrid-fcefyn-openwrt_one,labgrid-fcefyn-bananapi_bpi-r4"
export LG_IMAGE_MAP="labgrid-fcefyn-openwrt_one=/path/img1.itb,labgrid-fcefyn-bananapi_bpi-r4=/path/img2.itb"

uv run pytest tests/test_mesh.py -v --log-cli-level=INFO
```

### Virtual mesh (QEMU + vwifi, no labgrid)

```shell
export LG_VIRTUAL_MESH=1
export VIRTUAL_MESH_IMAGE=/path/to/lime-vwifi-x86-64-ext4-combined.img
export VIRTUAL_MESH_NODES=3

uv run pytest tests/test_mesh.py -v --log-cli-level=INFO
```

## Firmware catalog

`configs/firmware-catalog.yaml` lists TFTP paths for CI and local scripts. Resolve paths with:

```shell
uv run python scripts/resolve_firmware_from_catalog.py linksys_e8450
```

## CI

Workflows use the **self-hosted** runner `testbed-fcefyn` (not the global-coordinator used in openwrt-tests). See `.github/workflows/daily.yml` and `pull_requests.yml`. Jobs check out **aparcar/openwrt-tests** for `labnet.yaml`; the matrix is filtered to **`labgrid-fcefyn`** by default (override with **`LIBREMESH_CI_ALLOW_PROXY`**, comma-separated proxy names, when extending CI to other labs).

## Contributing a lab

See [docs/CONTRIBUTING_LAB.md](docs/CONTRIBUTING_LAB.md). Labgrid/Ansible exporter setup lives in **openwrt-tests**; local runs need the shared **`openwrt-tests/labnet.yaml`** (via sibling clone or `OPENWRT_TESTS_DIR` / `LABNET_PATH`).

## Docs

- [docs/CONTRIBUTING_LAB.md](docs/CONTRIBUTING_LAB.md) - lab onboarding (points to openwrt-tests for infra)
- [docs/labs/fcefynlab.md](docs/labs/fcefynlab.md) - FCEFyN hardware reference

Upstream reference for shared target files and `device_instances`: [openwrt-tests docs/sharing-target-files](https://github.com/aparcar/openwrt-tests/blob/main/docs/sharing-target-files.md).
