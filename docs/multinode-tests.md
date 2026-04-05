# Multi-node tests

Run tests across multiple physical DUTs simultaneously. Useful for WiFi speed regression testing, L2/L3 connectivity validation, and any scenario requiring device-to-device communication.

## Quick start

```bash
export LG_MULTI_PLACES="labgrid-mylab-device_a,labgrid-mylab-device_b"
export LG_IMAGE="/path/to/openwrt-initramfs.itb"
export LG_COORDINATOR="coordinator.example.com:20408"

uv run pytest tests/test_multinode.py -v --log-cli-level=INFO
```

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `LG_MULTI_PLACES` | yes | - | Comma-separated labgrid place names |
| `LG_IMAGE` | yes* | - | Firmware image path (used for all nodes) |
| `LG_IMAGE_MAP` | no | - | Per-place images: `place1=/path/img1,place2=/path/img2` |
| `LG_COORDINATOR` | no | `localhost:20408` | Labgrid coordinator address |
| `LG_MULTI_VLAN_IFACE` | no | `vlan200` | VLAN interface for SSH proxy (fallback) |
| `LG_MULTI_TEST_SUBNET` | no | `192.168.1` | Subnet prefix for test IPs |
| `LG_MULTI_KEEP_POWERED` | no | `0` | Set to `1` to leave DUTs on after tests |

*Either `LG_IMAGE` or `LG_IMAGE_MAP` must be set.

## How it works

1. If `conftest_vlan` is loaded and `VLAN_SWITCH_ENABLED=1`, all DUT ports are switched to a shared VLAN
2. `conftest_multinode.py` reads `LG_MULTI_PLACES` and spawns one `multinode_boot.py` subprocess per place
3. Each subprocess acquires the labgrid place, boots the DUT to shell via serial, and assigns a unique test IP (`192.168.1.<10+N>` on `br-lan`) via the serial console. The VLAN interface is auto-detected from NetworkService
4. The parent fixture collects all statuses, creates `MultiNode` objects with per-node SSH proxy (VLAN interface + test IP)
5. Tests receive the `multi_nodes` fixture (list of `MultiNode`)
6. On teardown, each subprocess powers off its DUT and releases the place

```
conftest_multinode.py (parent)
├── [optional] shared_vlan_multi → switches DUT ports to shared VLAN
├── multinode_boot.py --place device_a --node-index 0  (subprocess 1)
│   └── assigns 192.168.1.10/24 on br-lan, detects vlan_iface from NetworkService
├── multinode_boot.py --place device_b --node-index 1  (subprocess 2)
│   └── assigns 192.168.1.11/24 on br-lan, detects vlan_iface from NetworkService
└── ...
```

Each node replaces OpenWrt's default `192.168.1.1` with a unique IP (`192.168.1.<10+N>`) so multiple nodes on the same VLAN don't conflict. The VLAN interface for SSH proxy is auto-detected from Labgrid's NetworkService resource (zone ID suffix like `%vlan200`), falling back to `LG_MULTI_VLAN_IFACE`. Subnet is configurable via `LG_MULTI_TEST_SUBNET`.

## Golden device pattern

For WiFi speed testing, the first node in `LG_MULTI_PLACES` acts as the **iperf3 server** (a "golden" reference device), and the second node is the DUT being benchmarked.

```bash
# Golden device first, DUT second
export LG_MULTI_PLACES="labgrid-mylab-golden_ap,labgrid-mylab-dut_router"
```

The `TestWifiPerformance` tests in `test_multinode.py` follow this convention automatically.

## Mixed device types

When nodes use different hardware (different target YAMLs or firmware images), use `LG_IMAGE_MAP`:

```bash
export LG_MULTI_PLACES="labgrid-mylab-openwrt_one,labgrid-mylab-bpi_r4"
export LG_IMAGE_MAP="labgrid-mylab-openwrt_one=/srv/tftp/ow1.itb,labgrid-mylab-bpi_r4=/srv/tftp/bpi.itb"
```

Target YAMLs are resolved automatically from `labnet.yaml` based on the place name.

## VLAN switching (optional)

If your lab uses per-DUT VLAN isolation, install [labgrid-switch-abstraction](https://github.com/fcefyn-testbed/labgrid-switch-abstraction) and see [switch-abstraction.md](switch-abstraction.md) for dynamic VLAN configuration during tests.

## Available tests

| Test | Class | Description |
|---|---|---|
| `test_nodes_booted` | `TestMultiNodeConnectivity` | SSH echo works on all nodes |
| `test_nodes_have_ip` | `TestMultiNodeConnectivity` | Each node has IPv4 |
| `test_l3_connectivity` | `TestMultiNodeConnectivity` | Ping between nodes |
| `test_iperf3_available` | `TestWifiPerformance` | iperf3 installed (skips if not) |
| `test_wifi_interfaces_up` | `TestWifiPerformance` | Wireless interface is UP |
| `test_wifi_speed` | `TestWifiPerformance` | iperf3 throughput above threshold |

WiFi tests are marked with `@pytest.mark.lg_feature("wifi")` and can be selected with `-m lg_feature`.
