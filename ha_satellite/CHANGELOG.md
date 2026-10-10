# Changelog

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
