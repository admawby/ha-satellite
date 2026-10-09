# HA Satellite

Turn any Raspberry Pi on your network into a **satellite** of this Home Assistant
instance. From the **Satellites** panel in the sidebar you can:

- **Adopt** a Pi with one click (SSH) or with a one-line install command
- **Share USB radios** (Z-Wave, Zigbee, Thread/Matter sticks) over the network so
  Z-Wave JS, Zigbee2MQTT or ZHA running on HA can use them
- **Monitor** CPU temperature, CPU/memory/disk usage, load, uptime, under-voltage
  and throttling, all as Home Assistant entities
- Use a **web terminal** (full interactive shell) and a quick command runner
- **Update packages** on demand or automatically in a maintenance window
- **Reboot / shut down**, restart services and read the system journal
- Keep the satellite **agent up to date automatically** when the add-on is updated

## Requirements

| Satellite | |
| --- | --- |
| Hardware | Any Raspberry Pi (3, 4, 5, Zero 2 W) or other Debian-based Linux box |
| OS | Raspberry Pi OS / Debian 11+ / Ubuntu 22.04+ with systemd |
| Network | Same LAN as Home Assistant; a **fixed IP** (DHCP reservation) is strongly recommended |
| Access | SSH with a sudo-capable user (for one-click adoption), or a console to paste one command |

The Pi needs internet access during installation (it installs `python3-aiohttp`,
`python3-psutil`, `ser2net` and `nftables` with `apt`).

## Installation

1. In Home Assistant go to **Settings → Add-ons → Add-on store → ⋮ → Repositories**
   and add `https://github.com/OWNER/ha-satellite`.
2. Install **HA Satellite**, start it and enable **Show in sidebar**.
3. Open **Satellites** and click **Add satellite**.

### Option A — adopt over SSH

Enter the Pi's address, an SSH user (e.g. `pi`) and its password, then click
**Adopt**. The add-on logs in once, runs the installer with `sudo` and shows its
output live. The password is used only for that session and is never stored. The
SSH host key fingerprint is shown in the log so you can verify it.

### Option B — install command

Switch to **Install command**, click **Generate command** and run the shown line on
the Pi. It looks like:

```bash
curl -fsSk --pinnedpubkey 'sha256//…' 'https://192.168.1.10:8766/enroll/<token>/install.sh' | sudo bash
```

The command contains a one-time token (valid for 30 minutes) and the SHA-256 pin of
the add-on's certificate, so the Pi only accepts the script from *your* Home
Assistant.

## Using USB radios from Home Assistant

Open the satellite → **USB radios**. Detected sticks are listed with their stable
`/dev/serial/by-id/…` path and, for common models, what they are. Click **Share
over network**, adjust the port/baud rate if needed and **Save & apply**. The page
then shows the exact connection strings:

| Integration | Setting | Value |
| --- | --- | --- |
| Z-Wave JS / Z-Wave JS UI add-on | Device path | `tcp://<pi-ip>:20108` |
| Zigbee2MQTT | `serial.port` | `tcp://<pi-ip>:20108` (also set `serial.adapter`, e.g. `ember`, `zstack`, `deconz`) |
| ZHA | Serial device → *Enter manually* | `socket://<pi-ip>:20108` |

Baud rates: most Zigbee coordinators use `115200`; Silicon Labs EZSP/Ember sticks
sometimes need **RTS/CTS** enabled; Z-Wave sticks use `115200`.

> Use one bridge per stick and only one integration per stick. Networked radios
> work very well on a wired or stable Wi-Fi LAN; give the Pi a fixed IP.

## Home Assistant entities

If the MQTT integration is set up (e.g. the Mosquitto add-on), every satellite
appears as a **device** with these entities (otherwise they are published as plain
`sensor.hasat_*` states without a device):

- Sensors: CPU temperature, CPU usage, memory usage, disk usage, load (1m),
  last boot, package updates, agent version, IP address
- Binary sensors: throttled, under-voltage, reboot required
- Buttons: reboot, check for updates, install updates

Example automation — alert when a satellite overheats:

```yaml
triggers:
  - trigger: numeric_state
    entity_id: sensor.hasat_garage_pi_cpu_temp
    above: 75
    for: "00:05:00"
actions:
  - action: notify.notify
    data:
      message: "Garage Pi is running hot ({{ states('sensor.hasat_garage_pi_cpu_temp') }} °C)"
```

## Automatic package updates

Each satellite has its own schedule (**Updates** tab): enable/disable, start time
of the two-hour maintenance window, days of the week, `full-upgrade`, `autoremove`
and **reboot automatically when required**. The update count is refreshed every
six hours and the output of the last run is kept.

## Configuration

| Option | Default | Description |
| --- | --- | --- |
| `enrollment_host` | *auto* | IP/hostname satellites use to reach Home Assistant. Detected from the Supervisor if empty. |
| `allowed_networks` | RFC1918 ranges | Satellites may only be adopted/enrolled from these networks. Tighten to your LAN, e.g. `192.168.1.0/24`. |
| `extra_trusted_ips` | `[]` | Additional addresses allowed through each satellite's firewall (e.g. a second HA host or the IP HA really uses if it sits behind NAT). |
| `agent_port` | `8765` | TCP port the agent listens on for newly enrolled satellites. |
| `poll_interval` | `30` | Seconds between health polls. |
| `auto_update_agents` | `true` | Push the agent bundled with this add-on to satellites running an older version. |
| `publish_to_ha` | `true` | Create Home Assistant entities (MQTT discovery or REST states). |
| `log_level` | `info` | `debug`, `info`, `warning` or `error`. |

The **network** port `8766/tcp` is the enrollment endpoint. It only answers
requests carrying a valid one-time token. You may change the host port; the
install command follows automatically.

## Security model

- **Private CA.** On first start the add-on creates a certificate authority in
  `/data/pki` (included in HA backups). Its key never leaves the add-on.
- **Mutual TLS.** The agent only accepts connections that present the add-on's
  CA-signed *controller* certificate; the add-on only talks to an agent whose
  certificate carries that satellite's id. Every call — metrics, terminal,
  updates, reboot — goes over this channel. There are no passwords or API keys.
- **Keys stay put.** The satellite generates its private key locally and only sends
  a certificate request.
- **One-time enrollment.** Tokens are random (256-bit), expire after 30 minutes, are
  burned on use and are only accepted from `allowed_networks`. The installer is
  fetched with public-key pinning.
- **Firewall.** The agent installs an `nftables` table (`inet hasat`) that only
  allows the Home Assistant host (and `extra_trusted_ips`) to reach the agent port
  and radio-bridge ports. SSH and every other port are untouched. It can be
  turned off per satellite.
- **Ingress only.** The UI is served through Home Assistant's authenticated
  ingress; direct connections to the UI port are refused.
- **No secrets stored.** SSH credentials used for adoption are kept in memory for
  that session only.

> **The radio bridges themselves are plain TCP** (that is what Z-Wave JS, Z2M and
> ZHA speak). They are protected by the firewall rule above — keep satellites on a
> trusted LAN/VLAN and do not port-forward them.

The agent runs as `root` because it manages packages, devices, the firewall and
provides a root shell. Treat access to the Home Assistant admin UI accordingly.

## Removing a satellite

**Settings → Remove satellite…** deletes it from Home Assistant and (optionally)
uninstalls the agent, restores the original `ser2net` configuration and deletes
the firewall table. To remove the agent by hand:

```bash
sudo systemctl disable --now hasat-agent
sudo rm -rf /opt/hasat-agent /etc/hasat-agent /var/lib/hasat-agent /etc/systemd/system/hasat-agent.service
sudo nft delete table inet hasat
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Satellite shows *offline* with `certificate rejected` | The add-on's `/data` was reset (new CA). Re-run the install command on the Pi. |
| Offline with `Connect call failed` | Pi is down, its IP changed (edit the address in **Settings**) or the firewall does not include the IP HA uses — add it to `extra_trusted_ips`. |
| Install command hangs at download | The Pi cannot reach `enrollment_host:8766`. Set `enrollment_host` to HA's LAN IP. |
| Bridge says *not listening* | Check the **Logs** tab for `ser2net`; make sure no other program uses the stick. |
| Z-Wave JS / Z2M cannot connect | Use the exact `tcp://`/`socket://` string shown, and only one integration per stick. |

On the Pi: `journalctl -u hasat-agent -f` shows the agent log.
