# Devices in wigyorilab

## Coordinator/Exporter
- Raspberry Pi 4
    - Ethernet (eth0): Uplink in untagged VLAN
    - VLAN201-206: VLANs for DUTs
    - Serial consoles: 2x 4-port USB hub with various cp2102/pl2303
    - UUGear 4-port switchable USB3 hub
    - ClusterHat 4-port switchable hat via i2c (clusterctrl)

## DUTs
- Olimex A10 Lime
    - LAN-port: connected into vlan201
    - Serial: USB hub port 1
    - Power: UUGear port 1

- Olimex A20 Micro
    - LAN-port: connected into vlan202
    - Serial: USB hub port 2
    - Power: UUGear port 2

- Orange Pi Zero2 (cortexa53)
    - LAN-port: connected into vlan203
    - Serial: USB hub port 3
    - Power: UUGear port 3

- Orange Pi 2 (cortexa7)
    - LAN-port: connected into vlan204
    - Serial: USB hub port 4
    - Power: UUGear port 4

- Linksprite pcDuino 2 (cortexa8)
    - LAN-port: connected into vlan205
    - Serial: 2nd USB hub port 1
    - Power: clusterHAT port 1

- Pine64 Plus (cortexa53)
    - LAN-port: connected into vlan206
    - Serial: 2nd USB hub port 2
    - Power: clusterHAT port 2

## Misc Hardware / Notes

- TP-Link SG108E used as manageable switch
- No SDMux available, thus the boards boot off an SD-card with
  the latest snapshot u-boot
