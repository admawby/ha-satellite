# HA Satellite

Turn any Raspberry Pi on your network into a **satellite** of this Home Assistant
instance. From the **Satellites** panel in the sidebar you can:

- **Launch** a satellite on a Pi with one click (SSH) or with a one-line install command
- **Share USB radios** (Z-Wave, Zigbee, Thread/Matter sticks) over the network so
  Z-Wave JS, Zigbee2MQTT or ZHA running on HA can use them
- **Monitor** CPU temperature, CPU/memory/disk usage, load, uptime, under-voltage
  and throttling, all as Home Assistant entities
- Use a **web terminal** (full interactive shell) and a quick command runner
- **Update packages** on demand or automatically in a maintenance window
- **Manage Docker containers** (start/stop/restart, logs) and keep their images
  updated automatically on a separate schedule, or install Docker with one click
- **Browse and edit files**, upload/download, and mount USB drives
- **Reboot / shut down**, restart services and read the system journal
- Keep the satellite **agent up to date automatically** when the add-on is updated

## Requirements

| Satellite | |
| --- | --- |
| Hardware | Any Raspberry Pi (3, 4, 5, Zero 2 W) or other Debian-based Linux box; Synology NAS in [container mode](#synology-nas-container-mode) |
| OS | Raspberry Pi OS / Debian 11+ / Ubuntu 22.04+ with systemd |
| Network | Same LAN as Home Assistant; a **fixed IP** (DHCP reservation) is strongly recommended |
| Access | SSH with a sudo-capable user (for one-click launching), or a console to paste one command |

The Pi needs internet access during installation (it installs `python3-aiohttp`,
`python3-psutil`, `ser2net` and `nftables` with `apt`).

## Synology NAS (container mode)

Synology DSM cannot be modified like Raspberry Pi OS (no `apt`, DSM updates replace
system files, and DSM 7 has no USB-serial drivers for Z-Wave/Zigbee sticks). The
installer detects DSM and instead runs the agent as a Docker container called
`hasat-agent` in **Container Manager**. On a NAS only these features are enabled:

- **Health stats**: CPU temperature, CPU/memory usage, load, uptime and usage of
  the first storage volume (`/volume1`), as sensors in Home Assistant
- **Terminal**: the web terminal and command runner open a shell **on the NAS
  itself** (not inside the container)
- **Docker management**: the Docker tab for all containers in Container Manager,
  including scheduled image updates

Package updates, reboot/shutdown, USB radios, storage/file browser, system logs and
the firewall rule are switched off. The agent refuses those requests itself, the tabs
are hidden, and the matching Home Assistant entities are not created. Use Home
Assistant's built-in **Synology DSM** integration for NAS-specific data such as disk
health and DSM updates.

**Requirements:** DSM 7 with **Container Manager** installed (Package Center), and
SSH enabled (*Control Panel → Terminal & SNMP*) for one-click launching with an
administrator account. Otherwise run the install command from an SSH session with
`sudo`.

**How it runs:**
- The agent runs on the stock `python:3.12-slim` image. Its code, Python libraries,
  certificates and settings are kept in `/volume1/docker/hasat-agent`, so DSM
  updates do not remove them and nothing is built on the NAS.
- The container uses host networking, the host PID/UTS namespaces, `--privileged`
  and the Docker socket. This lets it report the NAS's own stats, open the terminal
  on the NAS and manage its containers. Treat it like root access to the NAS.
- It is labelled `hasat.update=false`, so the Docker tab never recreates or stops
  the agent itself.
- If DSM's firewall is enabled, allow TCP `8765` from your Home Assistant host.

To remove it without a trace, see [Removing a satellite](#removing-a-satellite).

## Installation

1. In Home Assistant go to **Settings → Add-ons → Add-on store → ⋮ → Repositories**
   and add `https://github.com/admawby/ha-satellite`.
2. Install **HA Satellite**, start it and enable **Show in sidebar**.
3. Open **Satellites** and click **Launch satellite**.

### Option A — launch over SSH

Enter the Pi's address, an SSH user (e.g. `pi`) and its password, then click
**Launch**. The add-on logs in once, runs the installer with `sudo` and shows its
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

## USB and Storage tab

Besides the radio list and network bridges described above, this tab shows:

**Drives & USB storage**: every disk and partition with its file system, size,
mount point and free space. USB drives that are plugged in but not mounted get a
**Mount** button and are mounted under `/media/hasat/<label>` (FAT/exFAT/NTFS
drives are mounted owned by root, readable by everyone). **Unmount** flushes and
unmounts a drive so it is safe to unplug. Only removable media under `/media` or
`/mnt` can be unmounted here, and mounts made here do not persist across reboots.
Use **Browse** to open a drive in the file browser.

**Files**: a file manager for the whole Pi (it runs as root, like the
terminal):

- navigate by clicking folders, the breadcrumb, the path box or the quick links
- click a text file to open it in the editor; **Save** (or Ctrl+S) writes it
  atomically and keeps its permissions and owner. If the file changed on the Pi
  since you opened it, the save is refused so nothing is overwritten by accident.
  Files over 2 MB or binary files can be downloaded instead
- **Upload** (multiple files, streamed, asks before overwriting), **Download**,
  **New folder**, **New file**, **Rename**/move and **Delete**

Guard rails: paths must be absolute, nothing under `/proc`, `/sys` or `/dev` can
be changed, and top-level system folders (`/`, `/etc`, `/usr`, `/home`, ...) cannot
be deleted or renamed. Their contents can be, so be careful.

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

## Docker containers

If the Pi runs Docker (for example containers you manage with Portainer), the
**Docker** tab lists every container with its image, state, ports and compose
stack, and lets you start, stop, restart, view logs and update it.

**Checking** compares each image tag with the registry's current digest without
downloading anything, so checks are cheap and do not use up Docker Hub pull limits.

**Updating** a container pulls the new image and recreates the container with the
same configuration: volumes and binds, published ports, environment, devices (e.g.
USB sticks), networks and aliases, labels and restart policy. Settings that only
came from the old image (its default env, command, labels) are dropped, so the
new image's defaults apply. Compose and Portainer labels are kept, so stacks keep
recognising their containers. If creating or starting the new container fails,
the original container is renamed back and restarted. Optionally the old image is
removed afterwards.

**Installing Docker**: if Docker is not installed, the tab offers an **Install Docker**
button with a choice between Docker's official script (`get.docker.com`, latest
Docker CE with the compose plugin) and the Raspberry Pi OS / Debian `docker.io`
package. The installer output is shown live and the Docker service is enabled at
boot. If Docker is installed but not running, the button is **Start Docker**.

**Automatic image updates** have their own schedule, separate from package
updates, and are **off by default**:

| Setting | Meaning |
| --- | --- |
| Run on a schedule | Enable the maintenance window |
| What to do | *Check and update* (pull + recreate) or *Only check* (report only) |
| Start time / days | Start of a two-hour window on the selected weekdays |
| Remove the old image | Delete the previous image after a successful update |
| Auto (per container) | Untick to exclude a container from scheduled updates |

Containers are never updated automatically when they:

- are unticked (excluded) in the Docker tab
- carry the label `hasat.update=false` (Watchtower's
  `com.centurylinklabs.watchtower.enable=false` is honoured too)
- use an image pinned by digest (`image@sha256:…`) or by image ID
- were started with `--rm`, or share their network namespace with other containers

Home Assistant gets a **Container updates** sensor, a **Containers running**
sensor and an **Update containers** button for each satellite.

> Tip: label containers you want to update by hand only, such as Portainer
> itself or databases, with `hasat.update=false`, or untick them.
>
> Private registries work when their credentials are in `/root/.docker/config.json`
> on the Pi (`sudo docker login <registry>`). Credential helpers are not supported.

## Configuration

| Option | Default | Description |
| --- | --- | --- |
| `enrollment_host` | *auto* | IP/hostname satellites use to reach Home Assistant. Detected from the Supervisor if empty. |
| `allowed_networks` | RFC1918 ranges | Satellites may only be launched/enrolled from these networks. Tighten to your LAN, e.g. `192.168.1.0/24`. |
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
- **No secrets stored.** SSH credentials used for launching are kept in memory for
  that session only.

> **The radio bridges themselves are plain TCP** (that is what Z-Wave JS, Z2M and
> ZHA speak). They are protected by the firewall rule above — keep satellites on a
> trusted LAN/VLAN and do not port-forward them.

The agent runs as `root` because it manages packages, devices, the firewall and
provides a root shell. Treat access to the Home Assistant admin UI accordingly.

## Removing a satellite

Open the satellite → **Settings → Remove satellite…**. With **Also remove HA
Satellite from the device, leaving no trace** ticked, the dialog first asks the
device exactly what it will remove and keep, then:

**Raspberry Pi / Debian (host mode)**
- stops and deletes the `hasat-agent` service and unit file
- deletes the agent code, certificates, settings and state
  (`/opt/hasat-agent`, `/etc/hasat-agent`, `/var/lib/hasat-agent`)
- deletes the firewall table `inet hasat`
- restores your original `/etc/ser2net.yaml` (or deletes it if the installer
  created it), and unmounts drives mounted under `/media/hasat`
- uninstalls the apt packages the installer added, but only those nothing
  else on the device needs any more. Packages that were already installed are
  never touched
- optionally removes **Docker**, if it was installed with the Docker tab's
  *Install Docker* button. This also deletes all containers, images and volumes,
  so it has its own checkbox and a second confirmation

**Synology / container mode**
- deletes the `hasat-agent` container, the `python:3.12-slim` image if the installer
  downloaded it, and the `/volume1/docker/hasat-agent` folder (which also holds the
  agent's Python libraries). Nothing is ever built on the NAS, so there's no build cache to leave behind
- Container Manager, DSM and your other containers are not touched

The installer records what it adds (`install-record.json`), and removal undoes
exactly that. The final steps run as a short-lived system job after the agent has
stopped, so nothing of the agent itself is left running. The only things that stay
are log lines already written to the shared system journal. Home Assistant also
forgets the satellite's entities, both MQTT devices and plain `sensor.hasat_*`
states.

If the device does not confirm the cleanup (for example it went offline halfway),
the satellite stays in Home Assistant so you can retry. Untick the option to only
remove it from Home Assistant.

Satellites installed with versions before 0.5.0 have no install record. Their
agent, files, service and firewall are still removed, but apt packages are kept.
For a satellite that is **offline**, the dialog shows the commands to run on the
device instead:

```bash
# Raspberry Pi / Debian
sudo systemctl disable --now hasat-agent
sudo rm -f /etc/systemd/system/hasat-agent.service && sudo systemctl daemon-reload
sudo nft delete table inet hasat
[ -f /etc/ser2net.yaml.hasat-orig ] && sudo mv /etc/ser2net.yaml.hasat-orig /etc/ser2net.yaml
sudo rm -rf /opt/hasat-agent* /etc/hasat-agent /var/lib/hasat-agent /media/hasat

# Synology (SSH)
sudo docker rm -f hasat-agent
sudo rm -rf /volume1/docker/hasat-agent
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Satellite shows *offline* with `certificate rejected` | The add-on's `/data` was reset (new CA). Re-run the install command on the Pi. |
| Offline with `Connect call failed` | Pi is down, its IP changed (edit the address in **Settings**) or the firewall does not include the IP HA uses — add it to `extra_trusted_ips`. |
| Docker tab says Docker is not available | Docker is not installed or `/var/run/docker.sock` is missing. |
| Image check shows *check failed* | The registry could not be reached, or the image needs a login (`sudo docker login` on the Pi). |
| Install command hangs at download | The Pi cannot reach `enrollment_host:8766`. Set `enrollment_host` to HA's LAN IP. |
| Bridge says *not listening* | Check the **Logs** tab for `ser2net`; make sure no other program uses the stick. |
| Z-Wave JS / Z2M cannot connect | Use the exact `tcp://`/`socket://` string shown, and only one integration per stick. |

On the Pi: `journalctl -u hasat-agent -f` shows the agent log.
