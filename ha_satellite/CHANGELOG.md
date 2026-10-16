# Changelog

## 0.12.0

- **History graphs** on the Overview tab: temperatures (CPU + GPUs), graphics memory, disk usage
  and network download/upload, for 1h / 6h / 24h / 7d / 30d
- Data comes from Home Assistant's database: recorded states up to 24 hours, hourly long-term
  statistics for 7 and 30 days; downsampled in the add-on, with gaps where a device was offline
- Crosshair tooltip with every series, legends, and a now/min/avg/max table per chart; colours
  validated for colour-blind readers in light and dark mode

## 0.11.0

- **Offline vs. broken agent**: when a satellite's agent does not answer, the add-on pings the device
  (TCP probe as fallback) and shows either **agent not responding** (orange: the device is up, the
  HA Satellite agent is not installed, stopped, hung, blocked or has the wrong certificate) or
  **offline** (red: the device does not answer at all)
- The satellite page explains the cause in plain English and how to fix it
- Home Assistant's Connection sensor gains the value `agent_down`

## 0.10.0

- **Fix: Raspberry Pis stopped reporting after the 0.9.0 update.** The new GPU code searched
  `/sys` recursively for the Pi's GPU statistics; sysfs contains symlink loops, so the search
  never finished and every status request hung (Pis showed offline with a TimeoutError).
  The file is now found through the GPU's driver entry, with no recursive search
- GPU probing runs with a 10-second time limit, so a stuck probe can no longer freeze a
  device's health report
- **Automatic recovery**: when a satellite accepts connections but its status request hangs,
  the add-on reinstalls the agent (at most every 10 minutes). Satellites stuck on 0.9.0 recover
  on their own once the add-on is updated
- **Network throughput**: download/upload rates per physical interface (Ethernet, Wi-Fi), link
  speed and data transferred, on tiles and in a Network card; HA sensors for download/upload
  (Mbit/s) and data received/sent (GB), plus per-interface sensors on multi-interface devices

## 0.9.0

- **GPU usage and temperatures**: NVIDIA (nvidia-smi: usage, encoder/decoder, temperature, VRAM,
  power, fan, clock), Intel (i915 per-engine busy counters incl. the QuickSync video engine; clock),
  AMD (amdgpu sysfs) and Raspberry Pi (V3D stats)
- Shown as a GPU chip on each satellite tile and a **Graphics** card on the Overview tab; all values
  open their history in Home Assistant
- Home Assistant sensors per GPU (usage, temperature, VRAM, power where available), with stable IDs
  like `sensor.hasat_<name>_gpu_nvidia0_usage`; entities of a removed GPU are deleted
- Works in container mode (Synology) too

## 0.8.2

- Fix: 0.8.1 switched to "MQTT through Home Assistant" whenever the MQTT integration existed,
  even if it had no working broker (e.g. after removing the Mosquitto add-on). The old sensors
  were removed and the new ones could never be created, so satellites had no entities and
  history links stopped working
- The add-on now sends a test message first and only switches to MQTT (and removes the old
  plain states) when Home Assistant can really publish
- If publishing over MQTT starts failing, it falls back to plain states automatically so the
  entities stay visible. It re-checks MQTT every 5 minutes and switches back once it works again

## 0.8.1

- **Entities are editable in the UI when your MQTT broker isn't an add-on**: if Home Assistant's
  MQTT integration is set up but the broker runs elsewhere, the add-on now publishes MQTT
  discovery through HA's own `mqtt.publish` service (and receives button presses through HA's
  websocket) instead of falling back to plain states without unique IDs
- On the switch, the old ID-less `sensor.hasat_*` states are removed first, so the new entities
  keep the same entity IDs and their history
- The log now says clearly when entities have no unique ID and why

## 0.8.0

- **Click a value to see its history**: CPU temp, CPU, memory, disk, load, uptime, update
  counts, Docker, power warnings, IP address, connection and agent version open Home Assistant's
  own more-info dialog with the history graph (on the satellite tiles and the Overview tab)
- Entity IDs are read from Home Assistant's entity registry by unique ID, so renamed entities
  still work; disabled entities are ignored. The REST fallback uses the fixed `sensor.hasat_*` IDs

## 0.7.0

- **Agent updates no longer look like outages**: while the add-on installs a new agent and the
  device restarts it, the satellite shows **updating** (amber dot, "updating agent to x.y.z…")
  instead of offline. Home Assistant keeps its sensors available and no "went offline"
  warning is logged. A device that doesn't return within 3 minutes is reported offline with the reason
- Home Assistant: new **Satellite agent** update entity per device (installed vs. latest version,
  native *Installing…* progress, *Install* button) and a **Connection** sensor
  (`online` / `updating` / `offline`, always available)

## 0.6.1

- **Dismiss** on the update progress panel now sticks: the add-on remembers it, so the panel
  no longer reappears when Home Assistant reloads the page (e.g. switching panels or the
  mobile app) or on another device

## 0.6.0

- **Live polling**: while the page is visible and you are using it, satellites are polled every
  3 s (and the page refreshes at that pace); hidden/closed tabs or 3 minutes without input fall
  back to the normal interval. Home Assistant sensors keep the normal pace
- **Update All**, **Update All Packages** and **Update All Docker Images** buttons in the
  satellite list, plus **Update Packages** / **Update Docker Images** on each tile, with live
  per-device progress. Jobs run in the add-on and continue when the page is closed
- Satellites are polled independently, so one unreachable device no longer delays the others
- Package updates now report a failure instead of staying "running" if apt cannot start

## 0.5.1

- Terminal: fail fast with a clear message when the satellite cannot be reached
  (previously the session could sit on a blank screen for up to 5 minutes, e.g. after
  a reboot, an IP change or when the satellite's firewall drops traffic)
- Terminal: shows "Connecting to …" and the reason a session closed; output from an old
  session can no longer leak into a new one after **Reconnect**
- Terminal: resizes itself when its box changes size, so it no longer gets stuck at a
  tiny size when opened while the page was hidden
- Tests: reconnect scenarios (repeated reconnects, after `exit`, two sessions, idle) against
  the UI proxy, a real installed agent and a real container agent

## 0.5.0

- **Remove without a trace**: removing a satellite can now erase everything HA Satellite
  put on the device. That covers the agent, service, files, certificates, firewall table,
  ser2net config and mounts, plus apt packages the installer added (only if nothing else
  needs them), and on Synology the container, image and data folder
  - the dialog shows the device's exact removal plan before you confirm
  - optional removal of Docker if it was installed with the *Install Docker* button
  - the satellite stays in Home Assistant if the device cannot confirm the cleanup
  - Home Assistant entities are removed for MQTT and REST setups
- The installer and *Install Docker* record what they add (`install-record.json`)
- "Adopting" a device is now called **launching** a satellite throughout the UI and docs
- CI: a real install + removal on Ubuntu verifies no packages, files, units or firewall rules remain

## 0.4.0

- **Synology NAS support (container mode)**: the installer detects DSM and runs the agent
  as the `hasat-agent` container in Container Manager (data in `/volume1/docker/hasat-agent`)
  - only health stats, terminal and Docker management are enabled; the agent refuses all
    other endpoints, the UI hides their tabs/buttons and HA only gets matching entities
  - terminal and command runner open a shell on the NAS itself (nsenter), not in the container
  - reports the NAS model, DSM version and data-volume usage
  - the agent never stops, updates or recreates its own container
- Other Linux systems without `apt` but with Docker also install in container mode
- Agents now report their mode and features; older agents keep every feature

## 0.3.0

- **USB radios** tab renamed **USB and Storage**; it still shows radios and bridges, and adds:
  - drives & USB storage with mount / unmount / browse
  - a file browser and text editor (upload, download, new file/folder, rename, delete),
    with conflict detection on save and protection for system folders
- Docker tab: **Install Docker** button (official get.docker.com script or Debian `docker.io`),
  or **Start Docker** when it is installed but not running, with live install log
- CPU temperature also shown in °F (small, grey) on the satellite cards and Overview
- Browsers now always load the new UI after an add-on update (cache-busted CSS/JS)

## 0.2.0

- New **Docker** tab: list containers (state, image, ports, compose stack), start / stop /
  restart, view logs, update a single container or all of them
- Image update checks via registry digests (no pulls), updates recreate containers with
  identical configuration and roll back automatically on failure
- Separate schedule for automatic container updates (off by default), check-only mode,
  per-container exclusions, `hasat.update=false` label, optional old-image cleanup
- Home Assistant: *Container updates* and *Containers running* sensors, *Update containers* button
- Package and container schedules share the same maintenance-window logic
- Satellites get agent 0.2.0 automatically when the add-on is updated

## 0.1.0

- Initial release
- One-click launching over SSH and pinned one-line installer
- Private CA and mutual-TLS agent API
- USB radio discovery and ser2net bridges with nftables protection
- Health metrics (CPU temperature, usage, memory, disk, throttling) via MQTT discovery or REST
- Web terminal, command runner, journal viewer
- On-demand and scheduled package updates, automatic agent updates
