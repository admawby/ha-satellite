"""GPU metrics: parsers (with real output captured from an NVIDIA RTX 2000E Ada + Intel UHD 630
host), Intel busy-counter maths, Raspberry Pi V3D stats, and the Home Assistant entities."""

from __future__ import annotations

import json
import subprocess
from typing import Callable

UUID = "GPU-2b9b7c1e-0d4f-4a57-9e2a-5c2d1f0e8a11"
NVIDIA_LINE = (f"0, NVIDIA RTX 2000E Ada Generation, {UUID}, 00000000:01:00.0, 37, 12, 25, 42, 4782, 16380, "
               "18.44, 50.00, 30, 1710\n")
# --query-compute-apps output (shape from the real host): a containerised python, a host ffmpeg
# run by a systemd service, and a snap.
NVIDIA_APPS = (f"{UUID}, 4101, /opt/venv/bin/python, 8420\n"
               f"{UUID}, 4202, /usr/lib/jellyfin-ffmpeg/ffmpeg, 864\n"
               f"{UUID}, 4303, Plex Transcoder, 312\n")
CGROUPS = {
    4101: "0::/system.slice/docker-ab12cd34ef56" + "0" * 52 + ".scope",
    4202: "0::/system.slice/jellyfin.service",
    4303: "0::/system.slice/snap.plexmediaserver.plexmediaserver.service",
}


def run(check: Callable[[bool, str], None], sat) -> None:
    from hasat import ha_bridge
    from hasat_agent import gpu

    # --- NVIDIA via nvidia-smi
    real_which, real_run, real_read = gpu.shutil.which, gpu.subprocess.run, gpu._read
    gpu.shutil.which = lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None
    gpu.subprocess.run = lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 0, NVIDIA_APPS if any("compute-apps" in c for c in cmd) else NVIDIA_LINE, "")
    gpu._read = lambda path: next((v for pid, v in CGROUPS.items() if path == f"/proc/{pid}/cgroup"), None)
    try:
        n = gpu.nvidia()
    finally:
        gpu.shutil.which, gpu.subprocess.run, gpu._read = real_which, real_run, real_read
    g = n[0]
    check(g["name"] == "NVIDIA RTX 2000E Ada Generation" and g["usage"] == 37 and g["temp"] == 42
          and g["power"] == 18.44 and g["engines"] == {"encoder": 12, "decoder": 25},
          "gpu: nvidia-smi parsed (usage, temp, power, encoder/decoder)")
    procs = g["processes"]
    check([p["name"] for p in procs] == ["python", "ffmpeg", "Plex Transcoder"] and procs[0]["mem_used"] == 8420 * 1024 * 1024,
          "gpu: NVIDIA processes listed, biggest VRAM user first")
    check(procs[0].get("container_id") == "ab12cd34ef56" and procs[1].get("service") == "jellyfin"
          and procs[2].get("service") == "plexmediaserver",
          "gpu: process owners resolved from cgroups (Docker container, systemd service, snap)")
    check(g["mem_total"] == 16380 * 1024 * 1024 and g["pci"] == "0000:01:00.0", "gpu: NVIDIA VRAM and PCI address")

    # --- Intel: busy-time counters -> utilisation between two reads
    pmu = gpu._IntelPMU.__new__(gpu._IntelPMU)
    pmu.fds, pmu.last = {"rcs0": 101, "vcs0": 102}, {}
    counters = {101: 0, 102: 0}
    real_read, real_mono = gpu.os.read, gpu.time.monotonic
    clock = [1000.0]
    gpu.os.read = lambda fd, n: counters[fd].to_bytes(8, "little")
    gpu.time.monotonic = lambda: clock[0]
    try:
        first = pmu.sample()
        clock[0] += 2.0
        counters[101] += int(0.5e9)   # render busy 0.5 s of 2 s
        counters[102] += int(1.5e9)   # video (QuickSync) busy 1.5 s of 2 s
        second = pmu.sample()
    finally:
        gpu.os.read, gpu.time.monotonic = real_read, real_mono
    check(first == {} and second == {"render": 25.0, "video": 75.0}, f"gpu: Intel engine busy % from counters ({second})")

    # --- Raspberry Pi V3D gpu_stats
    files = {"/proc/device-tree/model": "Raspberry Pi 5 Model B Rev 1.0",
             "STATS": "queue   timestamp       jobs    runtime\nbin 1000000000 10 100000000\nrender 1000000000 10 200000000\n"}
    real_read_file = gpu._read
    gpu._read = lambda path: files.get(path)
    gpu._V3D_STATS[:] = ["STATS"]
    gpu._V3D_LAST.clear()
    try:
        gpu.raspberry_pi(51.2)
        files["STATS"] = "queue timestamp jobs runtime\nbin 3000000000 20 300000000\nrender 3000000000 20 1200000000\n"
        pi = gpu.raspberry_pi(51.2)
    finally:
        gpu._read = real_read_file
        gpu._V3D_STATS.clear()
    check(pi[0]["name"] == "VideoCore VII" and pi[0]["usage"] == 50.0 and pi[0]["temp_shared"],
          "gpu: Raspberry Pi V3D usage from gpu_stats, temperature shared with SoC")

    # --- Home Assistant entities per GPU
    saved = sat.metrics
    sat.metrics = dict(saved, gpus=[
        dict(g, key="nvidia0", mem_used=4782 * 1024 * 1024, mem_percent=29.2),
        {"vendor": "intel", "name": "Intel UHD Graphics 630", "key": "intel0", "usage": 75.0,
         "engines": {"video": 75.0}, "temp": None, "temp_shared": True, "freq_mhz": 1150},
    ])
    try:
        keys = [e.key for e in ha_bridge.gpu_entities(sat)]
        check(keys == ["gpu_nvidia0_usage", "gpu_nvidia0_temp", "gpu_nvidia0_mem_percent", "gpu_nvidia0_power",
                       "gpu_intel0_usage"], f"gpu: entities per GPU, no fake temperature for the iGPU ({keys})")
        published: dict = {}

        class FakeMqtt:
            def publish(self, topic, payload, retain=False):
                published[topic] = payload

        b = ha_bridge.HABridge(lambda *_: None)
        b._mqtt, b._mqtt_connected = FakeMqtt(), True
        b._publish_mqtt(sat)
        cfg = json.loads(published[f"homeassistant/sensor/hasat_{sat.id}/gpu_nvidia0_temp/config"])
        check(cfg["unique_id"] == f"hasat_{sat.id}_gpu_nvidia0_temp" and cfg["device_class"] == "temperature"
              and cfg["default_entity_id"] == f"sensor.hasat_{sat.slug}_gpu_nvidia0_temp"
              and "NVIDIA" in cfg["name"], "gpu: HA temperature entity announced with stable ID")
        state = json.loads(published[f"hasat/{sat.id}/state"])
        check(state["gpu_nvidia0_usage"] == 37 and state["gpu_intel0_usage"] == 75.0 and state["gpu_nvidia0_power"] == 18.44,
              "gpu: values published to HA")

        sat.metrics = dict(saved, gpus=[sat.metrics["gpus"][0]])  # Intel GPU disappeared
        b._publish_mqtt(sat)
        check(published[f"homeassistant/sensor/hasat_{sat.id}/gpu_intel0_usage/config"] == "",
              "gpu: entities of a removed GPU are deleted from HA")
        single = [e.name for e in ha_bridge.gpu_entities(sat)]
        check(single[0] == "GPU usage", "gpu: a single GPU gets plain names (GPU usage)")
    finally:
        sat.metrics = saved
