# How to Contribute Your Lab to libremesh-tests

This guide is for lab administrators who want to add their DUTs to **libremesh-tests** (LibreMesh device testing). Most contributor labs will be **libremesh-only**: DUTs always in mesh topology (shared VLAN). The hybrid lab (FCEFYN) supports both OpenWrt (isolated) and LibreMesh (mesh) modes.

**Deployment model:** Maintainers deploy Ansible to your lab host via SSH. Your work is minimal: create lab config files in the repo and provide SSH access. We run the playbook from our control node.

## Prerequisites

- Labgrid host (coordinator + exporter) with VLAN-capable switch
- Serial console access to DUTs
- Power control (relay/PDU or PoE switch)
- TFTP server for firmware boot

## Steps to Contribute

### 1. Add your lab to labnet.yaml

Edit `labnet.yaml` and add your lab under `labs`, `devices`, and `device_instances`. Use existing labs (e.g. `labgrid-fcefyn`) as reference. See [docs/sharing-target-files.md](sharing-target-files.md) for multiple devices of the same model.

### 2. Create exporter configuration

Create `ansible/files/exporter/<your-lab-name>/` with at least:

| File | Purpose |
|------|---------|
| `exporter.yaml` | Labgrid exporter: place definitions with RawSerialPort, PDUDaemonPort, TFTPProvider, NetworkService |
| `netplan.yaml` | Host VLAN interfaces (one per DUT or shared mesh VLAN) |
| `pdudaemon.conf` | Power control: PDU host, port mapping |
| `dnsmasq.conf` | DHCP/TFTP for DUT provisioning |

Use `ansible/files/exporter/labgrid-fcefyn/` as a template.

### 3. Optional: labgrid-dut-proxy for SSH aliases

If you want SSH access via aliases (e.g. `ssh dut-belkin-1`), add `dut-proxy.yaml` in your exporter directory. Ansible deploys it to `/etc/labgrid/dut-proxy.yaml` on the host.

```yaml
devices:
  belkin-1:
    mesh: { vlan: 200, ip: "10.13.200.11" }
```

For mesh-only labs, include only the `mesh` block. For dual-mode (isolated + mesh), include both `isolated` and `mesh`.

If `dut-proxy.yaml` exists, Ansible installs `labgrid-dut-proxy` and configures sudoers. If not, use `labgrid-bound-connect` directly in your SSH config with the correct VLAN and IP.

### 4. Provide SSH access for maintainer deployment

Maintainers deploy the playbook to your lab host from our control node. You must allow SSH access:

1. **Host reachability:** Your lab host must be reachable from the internet (public IP, VPN, or tailscale/wireguard). Coordinate with maintainers if your network is behind NAT or firewalls.

2. **Authorized key:** Add our SSH public key to your lab host. Add this line to `~/.ssh/authorized_keys` for the user Ansible will connect as (a user that can run `sudo`, e.g. your admin account):

   ```
   ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICgvjp6jVUqMT0S+H7MoMifz3+M/rTLBS0P4MtQUCshG labgrid-fcefyn
   ```

3. **After your PR is merged:** We add your host to the inventory and run:

   ```bash
   ansible-playbook -i inventory.ini playbook_labgrid.yml -l <your-lab-name> -K
   ```

You do **not** need to run Ansible yourself. We handle deployment.

### 5. Add yourself as a developer

In `labnet.yaml`, add your SSH public key under `developers` for your lab so you can run tests.

### 6. Verify

Once deployment is complete, lock a place, power-cycle, and run a test:

```bash
uv run labgrid-client lock
uv run labgrid-client power cycle
pytest tests/test_base.py -v --log-cli-level=CONSOLE
uv run labgrid-client unlock
```

## libremesh-only vs hybrid

| Lab type | Topology | Exporter | dut-proxy |
|----------|----------|----------|-----------|
| **libremesh-only** | All DUTs in mesh VLAN (e.g. 200) | Single exporter with mesh IPs | Optional, single mode |
| **hybrid** | Switch between isolated (100–108) and mesh (200) | Two exporters, switched by mode | Optional, dual mode |

* The FCEFYN lab is prepared to operate in hybrid mode with scripts that handle automatic switch port re-assignment to the required VLAN (note that this adds a decent amount of complexity to the full lab setup). 
* If you want to set up your own hybrid lab just contact us. We can tell you more about the approach we use for this and provide help with the setup.
