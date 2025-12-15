# Devices in aparcars Testlab

## Setup

### Coordinator/Exporter

- **Model:** Raspberry Pi 5
- **IP:** `192.168.128.1`

### Switch

- **Model:** Zyxel GS1900-24EP Switch
- **IP:** `192.168.128.2`

| Port | PoE         | Device               |
| ---- | ----------- | -------------------- |
| 1    | 🟩 Active   | OpenWrt One WAN      |
| 2    | 🟩 Active   | BananaPi R4-Lite WAN |
| 13   | ⬜ Inactive | OpenWrt One LAN      |
| 14   | ⬜ Inactive | BananaPi R4-Lite LAN |
| 24   | ⬜ Inactive | RaspberryPi 5        |
