# GOD Mode add-on

> Centralized homelab monitoring for Home Assistant. One add-on, zero
> sidecars, native Lovelace dashboard, Telegram alerts, Ansible engine.

Collects CPU, RAM, disk, temperature, updates and uptime from your
Linux hosts (Proxmox, bare-metal, LXC, RPi…) plus Proxmox VMs/CTs
status, exposes it inside Home Assistant as native sensors, and ships a
ready-to-use Lovelace dashboard.

## Why an add-on, not a sidecar?

The previous incarnation of this project ran the collector in a separate
LXC. That works but couples the project to the operator's specific
infrastructure. As an add-on the user gets:

- One-click install from the HA UI.
- The collector lives next to HA itself; if HA is up, monitoring is up.
- Persistent state (SSH keys, inventory) goes to `/data` of the add-on,
  managed by Supervisor — no extra LVM/LXC to babysit.
- Auto-deploys the Lovelace YAML packages and dashboard into `/config`,
  so you don't have to copy files manually.
- Standard ops tooling: **Ansible** runs the polling. The same playbooks
  can be triggered manually for provisioning, troubleshooting or
  updates.

## What it does

1. On first boot, generates an SSH keypair in `/data/.ssh`.
2. Renders an Ansible inventory from the add-on `Configuration` tab.
3. Drops Lovelace YAML packages (`god_core`, `god_proxmox`,
   `god_hosts_linux`, `god_telegram`) into `/config/packages/` and a
   pre-built dashboard into `/config/dashboards/god_mode.yaml`.
4. Patches `/config/configuration.yaml` to register the dashboard
   (idempotent).
5. Runs `ansible-playbook gather.yml` every `poll_interval` seconds and
   exposes the merged result on `http://localhost:9876/api/hosts`.
6. The HA `rest:` integration in `god_hosts_linux.yaml` consumes that
   endpoint and creates one sensor per metric per host.

## Initial setup

1. **Install** the add-on (Settings → Add-ons → Add-on Store → ⋮ →
   Repositories → `https://github.com/hirofairlane/ha-god-mode`).
2. Open `Configuration` and review the `hosts` list. Defaults match the
   author's homelab; adjust to yours.
3. Open `Configuration` and paste your **Proxmox API token** under
   `pve.token_secret` if you want PVE node/VM/CT data. The token must
   have role `PVEAuditor` (read-only). Create with:

   ```bash
   pveum user add god@pve --comment "GOD Mode read-only"
   pveum acl modify / -user god@pve -role PVEAuditor
   pveum user token add god@pve godmode --privsep 0
   ```

4. **Start the add-on**.
5. Open the **GOD Mode** sidebar entry. The first panel shows the SSH
   public key the add-on just generated. Copy it.
6. Add it to `~/.ssh/authorized_keys` on every host you want to
   monitor — for the user you specified in the inventory (`root` or
   sergio or whatever).
7. Click **Install agent on all** in the panel — Ansible installs the
   metrics agent (`god-agent`) on each host and validates output.
8. Refresh and the host table will populate with live metrics.
9. Open the **GOD Mode** dashboard in the sidebar — Lovelace is
   already wired up.

## Configuration reference

```yaml
poll_interval: 60        # seconds between polling cycles (15-3600)
ssh_user: root           # default user for hosts that don't override
install_packages: true   # auto-write YAML to /config

pve:
  enabled: true
  host: 192.168.1.122
  token_id: "god@pve!godmode"
  token_secret: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

hosts:
  - name: zeratul
    addr: 192.168.1.122
    user: root
  - name: plex
    addr: 192.168.1.123
  - name: frigate
    addr: 192.168.1.170
    user: sergio
    port: 22
```

## Architecture

```
┌─────────────── Home Assistant OS ────────────────┐
│  ┌──── Add-on container (god_mode) ────┐         │
│  │  /etc/cont-init.d/*.sh              │         │
│  │      bootstrap (keys, inventory)    │         │
│  │      deploy lovelace packages       │         │
│  │  /etc/services.d/collector/run      │         │
│  │      └── god-collector.py           │  :9876  │ ← rest: HA Core
│  │           └── ansible-playbook      │         │
│  │  /etc/services.d/webui/run          │         │
│  │      └── god-webui.py               │  :8099  │ ← ingress
│  │  /data/.ssh/id_ed25519              │         │
│  │  /data/ansible/inventory.yml        │         │
│  └──────────────────────────────────────┘         │
└──────────────────────────────────────────────────┘
            ssh + ansible (parallel)
                    │
   ┌────────┬───────┼───────┬─────────┐
   ▼        ▼       ▼       ▼         ▼
zeratul   plex   frigate   h340    crafty   …
```

## Files written outside the add-on

- `/config/packages/god_core.yaml`
- `/config/packages/god_proxmox.yaml`
- `/config/packages/god_hosts_linux.yaml`
- `/config/packages/god_telegram.yaml`
- `/config/dashboards/god_mode.yaml`
- `/config/configuration.yaml` (one-shot patch to add `lovelace.dashboards.god-mode`)
- `/config/secrets.yaml` (one-shot append of `god_pve_api_authorization`)
- `/config/god_mode_pubkey.txt` (the SSH public key, for convenience)

If `install_packages: false`, none of those are touched.

## Removing the add-on

Uninstalling the add-on does **not** delete the YAMLs in `/config`. To
clean up by hand:

```bash
rm /config/packages/god_*.yaml
rm /config/dashboards/god_mode.yaml
# Remove the lovelace.dashboards.god-mode block from configuration.yaml
# Remove the line `god_pve_api_authorization` from secrets.yaml
```

## License

MIT.
