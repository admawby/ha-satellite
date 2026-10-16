"""GPU usage, temperature, memory and power.

Sources, all read-only:
  * NVIDIA      nvidia-smi
  * Intel       i915 PMU busy counters via perf_event_open (render, video/QuickSync,
                video-enhance, blitter engines); utilisation is measured between two polls,
                like CPU usage. Integrated GPUs have no own temperature sensor.
  * AMD         amdgpu sysfs (gpu_busy_percent, hwmon temperature/power, VRAM)
  * Raspberry   V3D gpu_stats (per-queue runtime) where the kernel provides it; the GPU
    Pi          shares the SoC temperature sensor with the CPU

Each GPU gets a stable key (vendor + index ordered by PCI address, e.g. "nvidia0"),
used for Home Assistant entity IDs.
"""

from __future__ import annotations

import ctypes
import glob
import logging
import os
import platform
import shlex
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from .mode import host_cmd

_LOGGER = logging.getLogger(__name__)
DRM = "/sys/class/drm"


def _read(path: str) -> Optional[str]:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _read_int(path: str) -> Optional[int]:
    raw = _read(path)
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


_NAMES: Dict[str, Optional[str]] = {}


def _pci_name(addr: str) -> Optional[str]:
    """Marketing name from lspci when available (e.g. 'UHD Graphics 630'); cached."""
    if addr not in _NAMES:
        _NAMES[addr] = _lspci_name(addr)
    return _NAMES[addr]


def _lspci_name(addr: str) -> Optional[str]:
    if not shutil.which("lspci"):
        return None
    try:
        out = subprocess.run(["lspci", "-mm", "-s", addr], capture_output=True, text=True, timeout=5).stdout
        # 00:02.0 "VGA compatible controller" "Intel Corporation" "CometLake-S GT2 [UHD Graphics 630]" -r03 ...
        fields = shlex.split(out.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if len(fields) < 4:
        return None
    vendor, device = fields[2], fields[3]
    if "[" in device and device.endswith("]"):
        device = device[device.rindex("[") + 1:-1]  # marketing name in brackets
    brand = vendor.split()[0] if vendor else ""
    return f"{brand} {device}".strip() if brand and not device.startswith(brand) else device


def _drm_cards() -> List[Tuple[str, str, str]]:
    """[(card path, driver, pci address)] for every DRM card with a PCI/platform device."""
    cards = []
    for card in sorted(glob.glob(f"{DRM}/card[0-9]*")):
        if "-" in os.path.basename(card):
            continue  # connector, not a card
        dev = os.path.realpath(f"{card}/device")
        driver = os.path.basename(os.path.realpath(f"{card}/device/driver")) if os.path.exists(f"{card}/device/driver") else ""
        cards.append((card, driver, os.path.basename(dev)))
    return sorted(cards, key=lambda c: c[2])


# --------------------------------------------------------------- NVIDIA
NVIDIA_FIELDS = ("index", "name", "pci.bus_id", "utilization.gpu", "utilization.encoder", "utilization.decoder",
                 "temperature.gpu", "memory.used", "memory.total", "power.draw", "power.limit", "fan.speed",
                 "clocks.gr")


def _num(v: str) -> Optional[float]:
    v = v.strip()
    if not v or v.startswith("[") or v.upper() in ("N/A", "NOT SUPPORTED"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def nvidia() -> List[Dict[str, Any]]:
    exe = shutil.which("nvidia-smi")
    cmd = ["nvidia-smi", f"--query-gpu={','.join(NVIDIA_FIELDS)}", "--format=csv,noheader,nounits"]
    if not exe:
        if os.environ.get("HASAT_MODE") != "container":
            return []
        cmd = host_cmd(["sh", "-c", "command -v nvidia-smi >/dev/null && exec " + " ".join(cmd)])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    gpus = []
    for line in proc.stdout.strip().splitlines():
        f = dict(zip(NVIDIA_FIELDS, [p.strip() for p in line.split(",")]))
        if len(f) < len(NVIDIA_FIELDS):
            continue
        mem_used, mem_total = _num(f["memory.used"]), _num(f["memory.total"])
        gpus.append({
            "vendor": "nvidia",
            "name": f["name"],
            "pci": f["pci.bus_id"].lower()[-12:],
            "usage": _num(f["utilization.gpu"]),
            "engines": {k: v for k, v in (("encoder", _num(f["utilization.encoder"])),
                                          ("decoder", _num(f["utilization.decoder"]))) if v is not None},
            "temp": _num(f["temperature.gpu"]),
            "mem_used": int(mem_used * 1024 * 1024) if mem_used is not None else None,
            "mem_total": int(mem_total * 1024 * 1024) if mem_total is not None else None,
            "power": _num(f["power.draw"]),
            "power_limit": _num(f["power.limit"]),
            "fan": _num(f["fan.speed"]),
            "freq_mhz": _num(f["clocks.gr"]),
        })
    return gpus


# ---------------------------------------------------------------- Intel
class _PerfAttr(ctypes.Structure):
    # PERF_ATTR_SIZE_VER0 layout (64 bytes) is enough for uncore PMU counters.
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("config", ctypes.c_uint64),
                ("sample_period", ctypes.c_uint64), ("sample_type", ctypes.c_uint64),
                ("read_format", ctypes.c_uint64), ("flags", ctypes.c_uint64),
                ("wakeup_events", ctypes.c_uint32), ("bp_type", ctypes.c_uint32), ("config1", ctypes.c_uint64)]


PERF_SYSCALL = {"x86_64": 298, "aarch64": 241, "armv7l": 364, "armv6l": 364}
INTEL_ENGINES = {"rcs0": "render", "vcs0": "video", "vcs1": "video2", "vecs0": "video_enhance", "bcs0": "blitter",
                 "ccs0": "compute"}


class _IntelPMU:
    """Busy-time counters of one i915 PMU; utilisation = busy ns / wall ns between reads."""

    def __init__(self, pmu_dir: str) -> None:
        self.fds: Dict[str, int] = {}
        self.last: Dict[str, Tuple[int, float]] = {}
        nr = PERF_SYSCALL.get(platform.machine())
        pmu_type = _read_int(f"{pmu_dir}/type")
        if nr is None or pmu_type is None:
            return
        libc = ctypes.CDLL(None, use_errno=True)
        cpu = int((_read(f"{pmu_dir}/cpumask") or "0").split(",")[0].split("-")[0] or 0)
        for event in sorted(glob.glob(f"{pmu_dir}/events/*-busy")):
            engine = os.path.basename(event)[:-5]
            cfg = _read(event) or ""
            if not cfg.startswith("config="):
                continue
            attr = _PerfAttr(type=pmu_type, size=ctypes.sizeof(_PerfAttr), config=int(cfg[7:], 16))
            fd = libc.syscall(nr, ctypes.byref(attr), -1, cpu, -1, 0)
            if fd >= 0:
                self.fds[engine] = fd
            else:
                _LOGGER.debug("perf_event_open(%s) failed: errno %s", engine, ctypes.get_errno())

    def sample(self) -> Dict[str, float]:
        now = time.monotonic()
        out = {}
        for engine, fd in self.fds.items():
            try:
                busy = int.from_bytes(os.read(fd, 8), "little")
            except OSError:
                continue
            prev = self.last.get(engine)
            self.last[engine] = (busy, now)
            if prev and now > prev[1]:
                pct = (busy - prev[0]) / ((now - prev[1]) * 1e9) * 100
                out[INTEL_ENGINES.get(engine, engine)] = round(max(0.0, min(100.0, pct)), 1)
        return out


_INTEL_PMUS: Dict[str, _IntelPMU] = {}


def intel(cards: List[Tuple[str, str, str]]) -> List[Dict[str, Any]]:
    gpus = []
    for card, driver, pci in cards:
        if driver not in ("i915", "xe"):
            continue
        # The PMU is named "i915" for the integrated GPU, "i915_<pci>" for others.
        pmu_dir = next((d for d in (f"/sys/bus/event_source/devices/i915_{pci.replace(':', '_')}",
                                    "/sys/bus/event_source/devices/i915") if os.path.isdir(d)), None)
        engines: Dict[str, float] = {}
        if pmu_dir:
            pmu = _INTEL_PMUS.get(pmu_dir) or _INTEL_PMUS.setdefault(pmu_dir, _IntelPMU(pmu_dir))
            engines = pmu.sample()
        gpus.append({
            "vendor": "intel",
            "name": _pci_name(pci) or "Intel graphics",
            "pci": pci,
            "usage": max(engines.values()) if engines else None,
            "engines": engines,
            "temp": None,  # integrated: shares the CPU package sensor
            "temp_shared": True,
            "freq_mhz": _read_int(f"{card}/gt_act_freq_mhz"),
            "freq_max_mhz": _read_int(f"{card}/gt_max_freq_mhz"),
        })
    return gpus


# ------------------------------------------------------------------ AMD
def amd(cards: List[Tuple[str, str, str]]) -> List[Dict[str, Any]]:
    gpus = []
    for card, driver, pci in cards:
        if driver != "amdgpu":
            continue
        dev = f"{card}/device"
        hw = next(iter(sorted(glob.glob(f"{dev}/hwmon/hwmon*"))), None)
        temp = _read_int(f"{hw}/temp1_input") if hw else None
        power = (_read_int(f"{hw}/power1_average") or _read_int(f"{hw}/power1_input")) if hw else None
        gpus.append({
            "vendor": "amd",
            "name": _read(f"{dev}/product_name") or _pci_name(pci) or "AMD graphics",
            "pci": pci,
            "usage": _read_int(f"{dev}/gpu_busy_percent"),
            "engines": {},
            "temp": round(temp / 1000, 1) if temp is not None else None,
            "mem_used": _read_int(f"{dev}/mem_info_vram_used"),
            "mem_total": _read_int(f"{dev}/mem_info_vram_total"),
            "power": round(power / 1e6, 1) if power else None,
        })
    return gpus


# ------------------------------------------------------------ Raspberry Pi
_V3D_LAST: Dict[str, Tuple[int, int]] = {}
_V3D_STATS: List[Optional[str]] = []  # cached path (searched once)


def raspberry_pi(soc_temp: Optional[float]) -> List[Dict[str, Any]]:
    model = _read("/proc/device-tree/model") or ""
    if "Raspberry Pi" not in model:
        return []
    if not _V3D_STATS:
        _V3D_STATS.append(next(iter(glob.glob("/sys/devices/platform/**/gpu_stats", recursive=True)), None))
    stats = _V3D_STATS[0]
    usage = None
    if stats:
        # "queue timestamp jobs runtime" per V3D queue, times in ns since boot.
        best = None
        for line in (_read(stats) or "").splitlines()[1:]:
            parts = line.split()
            if len(parts) < 4 or not parts[1].isdigit():
                continue
            queue, ts, runtime = parts[0], int(parts[1]), int(parts[3])
            prev = _V3D_LAST.get(queue)
            _V3D_LAST[queue] = (ts, runtime)
            if prev and ts > prev[0]:
                pct = (runtime - prev[1]) / (ts - prev[0]) * 100
                best = max(best or 0.0, max(0.0, min(100.0, pct)))
        usage = round(best, 1) if best is not None else None
    return [{
        "vendor": "pi",
        "name": "VideoCore " + ("VII" if "Pi 5" in model else "VI" if "Pi 4" in model else "GPU"),
        "pci": "soc",
        "usage": usage,
        "engines": {},
        "temp": soc_temp,  # one sensor for the whole SoC
        "temp_shared": True,
    }]


# ---------------------------------------------------------------- public
def collect(soc_temp: Optional[float] = None) -> List[Dict[str, Any]]:
    cards = _drm_cards()
    gpus: List[Dict[str, Any]] = []
    for source in (nvidia, lambda: intel(cards), lambda: amd(cards)):
        try:
            gpus += source()
        except Exception:  # noqa: BLE001 - one broken source must not hide the others
            _LOGGER.exception("GPU source failed")
    if not gpus:
        try:
            gpus += raspberry_pi(soc_temp)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Raspberry Pi GPU stats failed")
    counters: Dict[str, int] = {}
    for g in sorted(gpus, key=lambda g: (g["vendor"], g.get("pci") or "")):
        n = counters.get(g["vendor"], 0)
        counters[g["vendor"]] = n + 1
        g["key"] = f"{g['vendor']}{n}"
        if g.get("mem_used") is not None and g.get("mem_total"):
            g["mem_percent"] = round(g["mem_used"] / g["mem_total"] * 100, 1)
    return sorted(gpus, key=lambda g: g["key"])
