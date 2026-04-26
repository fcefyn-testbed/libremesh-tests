# Devices in FCEFYN Lab

## Coordinator/Exporter

- Lenovo T430 (Ubuntu)
    - Ethernet: VLAN trunk to switch
    - USB serial adapters via hub (udev symlinks)
    - Arduino relay control (barrel-jack DUTs)
    - PoE switch control (PoE DUTs via TP-Link SG2016P)
    - TFTP/DHCP: dnsmasq per VLAN

## Switch

- **Model:** TP-Link SG2016P (16-port Gigabit, 8 PoE)

| Port | Device           | Power   |
|------|------------------|---------|
| 1    | OpenWRT One      | PoE     |
| 2    | LibreRouter #1   | Relay   |
| 3    | LibreRouter #2   | Relay   |
| 9    | Host             | Trunk   |
| 10   | MikroTik         | Trunk   |
| 11   | Belkin RT3200 #1 | Relay   |
| 12   | Belkin RT3200 #2 | Relay   |
| 13   | Belkin RT3200 #3 | Relay   |
| 14   | Banana Pi R4     | Relay   |

## DUTs

| Place           | Switch | VLAN | Power  | Serial symlink    | SSH alias        | Target              |
|-----------------|--------|------|--------|-------------------|------------------|---------------------|
| belkin_rt3200_1 | 11     | 100  | Relay  | belkin-rt3200-1   | dut-belkin-1     | mediatek-mt7622     |
| belkin_rt3200_2 | 12     | 101  | Relay  | belkin-rt3200-2   | dut-belkin-2     | mediatek-mt7622     |
| belkin_rt3200_3 | 13     | 102  | Relay  | belkin-rt3200-3   | dut-belkin-3     | mediatek-mt7622     |
| bananapi_bpi-r4 | 14     | 103  | Relay  | bpi-r4            | dut-bananapi     | mediatek-filogic    |
| openwrt_one     | 1      | 104  | PoE    | openwrt-one      | dut-openwrt-one  | mediatek-filogic    |
| librerouter_1    | 2      | 105  | Relay  | librerouter-1     | dut-librerouter-1| ath79-generic       |


## Dual-mode topology (isolated + mesh)

The lab supports two network topologies controlled by the PoE switch and labgrid exporter:

| Mode    | VLANs       | Use case              |
|---------|-------------|------------------------|
| isolated | 100–105 (one per DUT) | OpenWrt tests + LibreMesh single-node, each DUT in its own VLAN |
| mesh    | 200 (shared) | LibreMesh multi-node tests, all DUTs in one VLAN |

## Misc Hardware / Notes

- Default: each DUT in isolated VLAN (100–108). Mesh mode: VLAN 200 shared.
- LibreRouter #1: 12V DC via Arduino relay channel 4. Uses UBootTFTPStrategy (TFTP boot initramfs in RAM).

## Maintainers

- @javierbrk
- @francoriba
- @ccasanueva7

## Consistency testing

For: Lab admin. Run single-node LibreMesh tests in a loop to detect sporadic failures (example):

```bash
for i in $(seq 1 10); do
  uv run pytest tests/test_libremesh.py -v --log-cli-level=CONSOLE || break
done
```

Set `LG_PLACE`, `LG_PROXY`, and `LG_IMAGE` as usual. Multi-node tests are in `tests/test_mesh.py`. Image paths can follow [firmware-catalog.yaml](../../configs/firmware-catalog.yaml) and `scripts/resolve_firmware_from_catalog.py`.

## Location

- Universidad Nacional de Córdoba. Facultad de Ciencias Exactas, Físicas y Naturales
- Córdoba, Argentina
