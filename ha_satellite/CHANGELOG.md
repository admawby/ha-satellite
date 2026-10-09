# Changelog

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
- One-click adoption over SSH and pinned one-line installer
- Private CA and mutual-TLS agent API
- USB radio discovery and ser2net bridges with nftables protection
- Health metrics (CPU temperature, usage, memory, disk, throttling) via MQTT discovery or REST
- Web terminal, command runner, journal viewer
- On-demand and scheduled package updates, automatic agent updates
