# OpenWrt Testing

> With great many support devices comes great many tests

OpenWrt Testing is a framework to run tests on OpenWrt devices, emulated or
real. Using [`labgrid`](https://labgrid.readthedocs.io/en/latest/) to control
the devices, the framework offers a simple way to write tests and run them on
different hardware.

## About this fork (libremesh-tests)

This repository is a fork aimed at **LibreMesh** testing. It is intended for
contributors who want to add their DUTs to **libremesh-tests** rather than
upstream openwrt-tests.

**Documentation index:** [CONTRIBUTING_LAB.md](docs/CONTRIBUTING_LAB.md) (lab admins) · [sharing-target-files.md](docs/sharing-target-files.md) (multiple devices) · [labs/fcefynlab.md](docs/labs/fcefynlab.md) (FCEFYN hardware)

Currently, only the **labgrid-fcefyn** lab contributes DUTs to both libremesh-tests
and openwrt-tests. In the future, we may facilitate a hybrid setup so that
other labs can contribute to both projects—for now, new labs added to this project would contribute
to libremesh-tests only.

## Requirements

- An OpenWrt firmware image
- Python and [`uv`](https://docs.astral.sh/uv/)
- QEMU


## Setup

For maximum convenience, clone this repository inside the `openwrt.git`
repository as `tests/`:

```shell
cd /path/to/openwrt.git/
git clone https://github.com/francoriba/libremesh-tests.git tests/
```

Install required packages to use Labgrid and QEMU:

```shell
curl -LsSf https://astral.sh/uv/install.sh | sh

sudo apt-get update
sudo apt-get -y install \
    qemu-system-mips \
    qemu-system-x86 \
    qemu-system-aarch64 \
    make
```

Verify the installation by running the tests:

```shell
make tests/setup V=s
```

## Running tests

Tests support both **physical DUTs** (via labgrid) and **QEMU virtual machines**.
Single-node and multi-node (mesh) tests can each run in either mode.

| | Physical DUT | QEMU VM |
|---|---|---|
| **Single-node** | `LG_PLACE` + labgrid | `--lg-env targets/qemu_*.yaml` + labgrid |
| **Multi-node mesh** | `LG_MESH_PLACES` + labgrid | `LG_VIRTUAL_MESH=1` (no labgrid) |

### Single-node tests on QEMU (virtual)

Run LibreMesh single-node tests against a QEMU x86-64 VM. No hardware required.

```shell
uv run pytest tests/test_base.py tests/test_lan.py tests/test_libremesh.py \
    --lg-env targets/qemu_x86-64_libremesh.yaml \
    --firmware /path/to/lime-x86-64-generic-ext4-combined.img \
    --lg-log --log-cli-level=CONSOLE --lg-colored-steps -v
```

For vanilla OpenWrt (non-LibreMesh) images, use the upstream targets instead:

```shell
uv run pytest tests/test_base.py \
    --lg-env targets/qemu_x86-64.yaml \
    --firmware /path/to/openwrt-x86-64-generic-squashfs-combined.img \
    --lg-log --log-cli-level=CONSOLE --lg-colored-steps -v
```

### Single-node tests on physical DUTs

```shell
export LG_PLACE=labgrid-fcefyn-belkin_rt3200_2
export LG_PROXY=labgrid-fcefyn
export LG_IMAGE=/srv/tftp/firmwares/belkin_rt3200/lime-...itb

uv run labgrid-client lock
uv run pytest tests/test_libremesh.py --log-cli-level=CONSOLE -v
uv run labgrid-client unlock
```

### Multi-node mesh tests on QEMU (virtual)

Launches N QEMU VMs with vwifi for mesh simulation. No labgrid needed.

```shell
export LG_VIRTUAL_MESH=1
export VIRTUAL_MESH_IMAGE=/path/to/lime-vwifi-x86-64-ext4-combined.img
export VIRTUAL_MESH_NODES=3

uv run pytest tests/test_mesh.py -v --log-cli-level=INFO
```

### Multi-node mesh tests on physical DUTs

```shell
export LG_MESH_PLACES="labgrid-fcefyn-openwrt_one,labgrid-fcefyn-bananapi_bpi-r4"
export LG_IMAGE_MAP="labgrid-fcefyn-openwrt_one=/path/to/img1,labgrid-fcefyn-bananapi_bpi-r4=/path/to/img2"

uv run pytest tests/test_mesh.py -v --log-cli-level=INFO
```

### Using the Makefile (OpenWrt upstream)

For running upstream OpenWrt tests from inside the `openwrt.git` tree:

```shell
cd /path/to/openwrt.git
make tests/x86-64 V=s
```

## Writing tests

The framework uses `pytest` to execute commands and evaluate the output. Test
cases use the two _fixture_ `ssh_command` or `shell_command`. The object offers
the function `run(cmd)` and returns _stdout_, _stderr_ (SSH only) and the exit
code.

The example below runs `uname -a` and checks that the device is running
_GNU/Linux_

```python
def test_uname(shell_command):
    assert "GNU/Linux" in shell_command.run("uname -a")[0][0]
```

## Remote Access

With *labgrid*, you can remotely access devices. Key capabilities include
**power control**, **console**, and **SSH access**.

To enable remote access, you need SSH access with forwarding enabled on the host
exporting the device. For example, to reach the device `openwrt_one` located in
the lab `labgrid-fcefyn`, you must have access to the `labgrid-fcefyn` host
(coordinator and exporter run on the same host):

```shell
labgrid-fcefyn -> openwrt_one
```

You can request access to existing labs or contribute your own. To do this,
submit a pull request modifying the `labnet.yaml` file. If you have multiple
devices of the same model, see [docs/sharing-target-files.md](docs/sharing-target-files.md)
for how to avoid duplicate target files.

**Lab setup**: See [docs/CONTRIBUTING_LAB.md](docs/CONTRIBUTING_LAB.md) for how to contribute your lab. The `ansible/` directory contains the playbook; labs add host-specific configs in `ansible/files/exporter/<host>/`. Optionally add `dut-proxy.yaml` for SSH aliases (installed only when present).

To access a remote device, configure the following environment variables.
Notably, `LG_PROXY` sets the proxy host (always the lab name):

```shell
export LG_IMAGE=~/firmware/openwrt-mediatek-mt7622-linksys_e8450-ubi-initramfs-kernel.bin # Firmware to boot
export LG_PLACE=labgrid-fcefyn-belkin_rt3200_1 # Target device, formatted as <lab>-<device>
export LG_PROXY=labgrid-fcefyn # Proxy to use, typically the lab name
export LG_ENV=targets/linksys_e8450.yaml # Environment definition (optional when using device_instances)
```

**Note**: `LG_ENV` is optional when using `device_instances`. If not set, pytest automatically resolves the target file from `LG_PLACE`. Explicitly setting `LG_ENV` takes precedence over automatic resolution. See [docs/sharing-target-files.md](docs/sharing-target-files.md) for details.

To avoid interference from CI or other developers, lock the device before use:

```shell
uv run labgrid-client lock
```

Once locked, you can power-cycle the device and access its console:

```shell
uv run labgrid-client power cycle
uv run labgrid-client console
```

To bring the device into a specific state—such as booting your firmware defined
by `LG_IMAGE`—use a state definition:

```shell
uv run labgrid-client --state shell console
```

You can also run local tests directly on the remote device:

```shell
pytest tests/ --log-cli-level=CONSOLE
```

Lastly, unlock your device when you're done:

```shell
uv run labgrid-client unlock
```
