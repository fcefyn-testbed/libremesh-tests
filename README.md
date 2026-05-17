# libremesh-tests

Pytest suite for **LibreMesh** on real hardware (labgrid) and **virtual mesh** (QEMU + vwifi).

Covers LibreMesh single-node validation, multi-node mesh connectivity, and virtual QEMU mesh.
The shared device/lab inventory (`labnet.yaml`) is maintained upstream in
[aparcar/openwrt-tests](https://github.com/aparcar/openwrt-tests) and is not duplicated here.

## Requirements

- Python 3.13+ and [`uv`](https://docs.astral.sh/uv/)
- On the lab host: labgrid client, coordinator, exporter
- For QEMU: `qemu-system-x86_64` and related packages (see virtual mesh docs)

## Setup

```shell
cd /path/to/libremesh-tests
uv sync
```

**`labnet.yaml` location:** pytest resolves the inventory file in this order:
`LABNET_PATH` (explicit file path), `OPENWRT_TESTS_DIR/labnet.yaml`, or a **sibling clone**
`../openwrt-tests/labnet.yaml` (recommended local layout: check out
[aparcar/openwrt-tests](https://github.com/aparcar/openwrt-tests) next to this repository).
For CI or ad-hoc use without that layout, set `LABNET_PATH` directly.

**Dynamic VLAN switching:** Multi-node mesh tests only (`LG_MESH_PLACES` set) run `switch-vlan`
to move DUTs to mesh VLAN (200) on the **lab host** via SSH (the same host as `LG_PROXY`).
Single-node tests keep each DUT on its isolated exporter VLAN. No local
`dut-config.yaml` or `labgrid-switch-abstraction` install is needed on the developer
machine - only on the lab host. If VLANs are already correct, `VLAN_SWITCH_DISABLED=1`
skips switch commands.

Place-to-DUT name mapping defaults to the FCEFyN convention (strips `labgrid-fcefyn-`). For
other labs, override with `PLACE_PREFIX` (e.g. `export PLACE_PREFIX="labgrid-mylab-"`, or
`PLACE_PREFIX=""` to fall back to stripping up to the second hyphen).

Lab infrastructure (coordinator, exporter, TFTP, Ansible) is documented in
**fcefyn_testbed_utils** and upstream **aparcar/openwrt-tests** (`ansible/`);
this repo does not ship `ansible/`.

## Targets shipped here

Only DUTs used for LibreMesh / FCEFyN workflows:

| File | Role |
|------|------|
| `targets/openwrt_one.yaml` | OpenWrt One |
| `targets/bananapi_bpi-r4.yaml` | Banana Pi R4 |
| `targets/linksys_e8450.yaml` | Belkin RT3200 / Linksys E8450 |
| `targets/librerouter_librerouter-v1.yaml` | LibreRouter v1 |
| `targets/qemu_x86-64_libremesh.yaml` | QEMU x86-64 LibreMesh |

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

`LG_IMAGE` must be a real file on the machine running labgrid-client (see remote access docs
in fcefyn_testbed_utils).

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

This repository is a **pure test library**. The active LibreMesh CI
(per-PR build + QEMU + opt-in physical, plus the daily lab
validation cron) lives in
[`fcefyn-testbed/lime-packages/.github/workflows/build-firmware.yml`](https://github.com/fcefyn-testbed/lime-packages/blob/master/.github/workflows/build-firmware.yml)
and **checks out this repo at runtime** to get the pytest suites,
labgrid env files, virtual-mesh launcher and the helper modules
under `tests/`.

Only one workflow runs against this repository on push/PR:

- `.github/workflows/formal.yml` - `ruff` + `isort` + cross-version
  `uv sync`. Pure Python lint, no lab access.

The legacy `daily.yml` and `pull_requests.yml` workflows that used
to ride the lab from this repo were retired in May 2026 when the
CI was unified in `fcefyn-testbed/lime-packages`. The shared
`configs/firmware-catalog.yaml` and `scripts/resolve_firmware_from_catalog.py`
remain available for local manual runs.

## Contributing a lab

See [docs/CONTRIBUTING_LAB.md](docs/CONTRIBUTING_LAB.md).

## Docs

- [docs/CONTRIBUTING_LAB.md](docs/CONTRIBUTING_LAB.md) - lab onboarding
- [docs/labs/fcefynlab.md](docs/labs/fcefynlab.md) - FCEFyN hardware reference
