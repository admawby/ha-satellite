# HA Satellite — Home Assistant add-on repository

[![Open your Home Assistant instance and show the add add-on repository dialog with this repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fadmawby%2Fha-satellite)

Turn Raspberry Pis around your home into managed **satellites** of your main Home
Assistant instance — one add-on, installed from this repository.

## Add-ons

### [HA Satellite](./ha_satellite)

- **Remote USB radios** — share Z-Wave / Zigbee / Thread sticks plugged into a Pi
  with Z-Wave JS, Zigbee2MQTT or ZHA running on your main HA
- **Monitoring** — CPU temperature, usage, memory, disk, under-voltage and
  throttling as HA entities (MQTT discovery), with reboot/update buttons
- **Built-in CLI** — full web terminal plus a one-shot command runner and journal viewer
- **Docker** — manage containers (start/stop/restart/logs) and keep images updated on
  their own schedule, safely recreating containers with the same configuration
- **Auto updates** — scheduled `apt` upgrades with optional reboot; the satellite
  agent itself updates with the add-on
- **Secure, LAN-only** — private CA, mutual TLS, one-time pinned enrollment and an
  nftables rule that only lets your HA host in

## Installation

1. **Settings → Add-ons → Add-on store → ⋮ → Repositories**, add
   `https://github.com/admawby/ha-satellite`
2. Install **HA Satellite**, start it, enable **Show in sidebar**
3. Open **Satellites → Add satellite** and adopt your Pi

Full documentation: [ha_satellite/DOCS.md](./ha_satellite/DOCS.md)

## How it works

```
 Home Assistant host                                   Raspberry Pi satellite
┌──────────────────────────────┐                    ┌──────────────────────────────┐
│ HA Satellite add-on          │  mutual TLS :8765  │ hasat-agent (systemd)        │
│  • ingress UI + terminal     │ ─────────────────▶ │  • metrics, USB, apt, PTY    │
│  • private CA (/data/pki)    │                    │  • writes ser2net + nftables │
│  • MQTT discovery            │                    │                              │
│  • enrollment :8766 (TLS,    │ ◀── one-time CSR ─ │ installer (pinned curl)      │
│    one-time tokens)          │                    │                              │
├──────────────────────────────┤   raw TCP :20108   │ ser2net ── /dev/serial/by-id │
│ Z-Wave JS / Z2M / ZHA        │ ─────────────────▶ │            (USB radio)       │
└──────────────────────────────┘  (firewalled)      └──────────────────────────────┘
```

## Repository layout

```
repository.yaml              add-on repository metadata
ha_satellite/                the add-on (built locally by the Supervisor)
  config.yaml build.yaml Dockerfile DOCS.md CHANGELOG.md translations/
  rootfs/opt/hasat/server/   manager: aiohttp app, PKI, enrollment, MQTT bridge, UI
  rootfs/opt/hasat/agent/    satellite agent + install.sh template (served to Pis)
tests/                       offline end-to-end test (manager ⇄ agent over mTLS)
```

## Development

```bash
python -m pip install aiohttp cryptography asyncssh paho-mqtt psutil
python tests/test_e2e.py
```

## License

MIT
