# Devices in FCEFYN Lab

## Coordinator/Exporter
- Laptop
    - Ethernet: Connected to network switch with VLAN trunk
    - USB Serial Adapters: Connected to DUTs via USB hub
    - Arduino-based relay control: Power management for all DUTs
    - TFTP/DHCP Server: dnsmasq serving `/srv/tftp/` per VLAN

## Network Infrastructure
- MikroTik router: Router providing inter-VLAN routing and internet access
- TP-Link SG2008P: 8-port Gigabit managed switch with VLAN support
- Each DUT is isolated in its own VLAN

### VLAN Configuration
| VLAN ID | Purpose | Subnet | Gateway |
|---------|---------|--------|---------|
| 100 | Belkin RT3200 #1 | 192.168.100.0/24 | 192.168.100.254 |
| 101 | Belkin RT3200 #2 | 192.168.101.0/24 | 192.168.101.254 |

## DUT Devices

### belkin_rt3200_1 (Linksys E8450 / Belkin RT3200)
- VLAN: 100 (isolated)
- Device IP: 192.168.1.1 (OpenWrt default)
- Server TFTP IP: 192.168.100.1
- Serial: `/dev/belkin-rt3200-1` @ 115200 baud
- Power: Arduino relay #2 (via PDUDaemon)
- Board: `linksys,e8450-ubi`
- Target: `mediatek-mt7622`
- Firmware: `initramfs-recovery.itb` (U-Boot TFTP recovery)

### belkin_rt3200_2 (Linksys E8450 / Belkin RT3200)
- VLAN: 101 (isolated)
- Device IP: 192.168.1.1 (OpenWrt default)
- Server TFTP IP: 192.168.101.1
- Serial: `/dev/belkin-rt3200-2` @ 115200 baud
- Power: Arduino relay #3 (via PDUDaemon)
- Board: `linksys,e8450-ubi`
- Target: `mediatek-mt7622`
- Firmware: `initramfs-recovery.itb` (U-Boot TFTP recovery)

## Power Control

Power control is managed via **PDUDaemon** using a custom Arduino-based relay controller interface:
- Relay channel 2 → Belkin RT3200 #1
- Relay channel 3 → Belkin RT3200 #2

## Server Configuration

The coordinator server has VLAN interfaces for each DUT:
- `vlan100`: 192.168.100.1/24, 192.168.1.100/24 (for Belkin #1)
- `vlan101`: 192.168.101.1/24, 192.168.1.101/24 (for Belkin #2)

Each VLAN interface has two IPs:
- `192.168.X.1/24`: TFTP server address (used by U-Boot)
- `192.168.1.X/24`: SSH access to device at 192.168.1.1

## Misc Hardware / Notes

- All devices use TFTP for firmware recovery via U-Boot
- SSH connections use `labgrid-bound-connect` to route through specific VLAN interfaces

## Maintainers

- @javierbrk
- @francoriba
- @ccasanueva7

## Location

- Universidad Nacional de Córdoba. Facultad de Ciencias Exactas, Físicas y Naturales
- Córdoba, Argentina
