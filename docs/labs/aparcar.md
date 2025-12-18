# Devices in aparcars Testlab

## Setup

### Coordinator/Exporter

- **Model:** Raspberry Pi 5
- **IP:** `192.168.128.1`

### Switch

- **Model:** Zyxel GS1900-24EP Switch
- **IP:** `192.168.128.2`

| Port | PoE         | Type | Device                                   |
| ---- | ----------- | ---- | ---------------------------------------- |
| 1    | 🟩 Active   | WAN  | OpenWrt One                              |
| 2    | 🟩 Active   | WAN  | BananaPi R4-Lite                         |
| 3    | 🟩 Active   | LAN  | RaspberryPi 4 LAN                        |
| 4    | 🟩 Active   | LAN  | Genexis Pulse EX400 / Inteno Pulse EX400 |
| 5    | 🟩 Active   | LAN  | TP-Link TL-WDR3600 v1                    |
| 6    | 🟩 Active   | LAN  | Bananapi BPi-R4                          |
| 13   | ⬜ Inactive | LAN  | OpenWrt One                              |
| 14   | ⬜ Inactive | LAN  | BananaPi R4-Lite                         |
| 15   | ⬜ Inactive | WAN  | Genexis Pulse EX400 / Inteno Pulse EX400 |
| 16   | ⬜ Inactive | WAN  | TP-Link TL-WDR3600 v1                    |
| 17   | ⬜ Inactive | WAN  | Bananapi BPi-R4                          |
| 24   | ⬜ Inactive | n/a  | Coordinator/Exporter                     |
