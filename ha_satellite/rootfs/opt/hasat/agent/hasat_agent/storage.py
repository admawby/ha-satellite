"""Block devices, mounts and USB storage (mount / unmount)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

import psutil

MOUNT_BASE = os.environ.get("HASAT_MOUNT_BASE", "/media/hasat")
LSBLK_COLS = "NAME,PATH,SIZE,TYPE,FSTYPE,LABEL,UUID,MOUNTPOINT,TRAN,RM,HOTPLUG,MODEL,VENDOR,PKNAME"
_SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.-]+")
# Mount points the UI may unmount: removable media only, never system mounts.
UNMOUNT_PREFIXES = ("/media/", "/mnt/", "/run/media/")


class StorageError(ValueError):
    pass


def _run(*cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)


def _lsblk() -> List[Dict[str, Any]]:
    if not shutil.which("lsblk"):
        return []
    proc = _run("lsblk", "-J", "-b", "-o", LSBLK_COLS)
    if proc.returncode != 0:
        return []
    return json.loads(proc.stdout).get("blockdevices", [])


def _flatten(devs: List[Dict[str, Any]], parent: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    out = []
    for d in devs:
        usb = (d.get("tran") == "usb") or bool(parent and parent.get("tran") == "usb")
        removable = bool(d.get("rm") or d.get("hotplug") or (parent and (parent.get("rm") or parent.get("hotplug"))))
        item = {
            "name": d.get("name"),
            "path": d.get("path") or f"/dev/{d.get('name')}",
            "type": d.get("type"),
            "size": int(d.get("size") or 0),
            "fstype": d.get("fstype"),
            "label": d.get("label"),
            "uuid": d.get("uuid"),
            "mountpoint": d.get("mountpoint"),
            "model": (d.get("model") or (parent or {}).get("model") or "").strip() or None,
            "vendor": (d.get("vendor") or (parent or {}).get("vendor") or "").strip() or None,
            "usb": usb,
            "removable": removable or usb,
            "parent": parent.get("path") if parent else None,
        }
        if item["type"] not in ("loop", "zram", "ram"):
            out.append(item)
        out += _flatten(d.get("children") or [], d if not parent else dict(parent, **d))
    return out


def devices() -> List[Dict[str, Any]]:
    devs = _flatten(_lsblk())
    for d in devs:
        if d["mountpoint"]:
            try:
                u = psutil.disk_usage(d["mountpoint"])
                d["used"], d["free"], d["percent"] = u.used, u.free, u.percent
            except OSError:
                pass
        d["can_mount"] = bool(
            d["removable"] and d["type"] in ("part", "disk") and d["fstype"] and not d["mountpoint"]
            and d["fstype"] not in ("swap", "crypto_LUKS", "LVM2_member", "linux_raid_member")
        )
        d["can_unmount"] = bool(d["mountpoint"]) and d["mountpoint"].startswith(UNMOUNT_PREFIXES)
    return devs


def mounts() -> List[Dict[str, Any]]:
    out = []
    for p in psutil.disk_partitions(all=False):
        if p.fstype in ("squashfs",) or p.mountpoint.startswith("/snap"):
            continue
        item = {"device": p.device, "mountpoint": p.mountpoint, "fstype": p.fstype, "options": p.opts}
        try:
            u = psutil.disk_usage(p.mountpoint)
            item.update(total=u.total, used=u.used, free=u.free, percent=u.percent)
        except OSError:
            pass
        out.append(item)
    return out


def summary() -> Dict[str, Any]:
    return {"devices": devices(), "mounts": mounts(), "mount_base": MOUNT_BASE}


def mount(device: str, read_only: bool = False) -> Dict[str, Any]:
    dev = next((d for d in devices() if d["path"] == device), None)
    if not dev:
        raise StorageError(f"unknown device {device}")
    if not dev["can_mount"]:
        raise StorageError("only unmounted removable/USB partitions with a file system can be mounted here")
    name = _SAFE_LABEL.sub("_", dev["label"] or dev["uuid"] or dev["name"]).strip("_") or dev["name"]
    target = os.path.join(MOUNT_BASE, name)
    n = 2
    while os.path.ismount(target) or (os.path.exists(target) and os.listdir(target)):
        target = os.path.join(MOUNT_BASE, f"{name}_{n}")
        n += 1
    os.makedirs(target, exist_ok=True)
    opts = ["ro"] if read_only else []
    if dev["fstype"] in ("vfat", "exfat", "ntfs", "ntfs3"):
        opts += ["uid=0", "gid=0", "umask=022"]
    cmd = ["mount"] + (["-o", ",".join(opts)] if opts else []) + [device, target]
    proc = _run(*cmd)
    if proc.returncode != 0:
        try:
            os.rmdir(target)
        except OSError:
            pass
        raise StorageError(proc.stderr.strip() or "mount failed")
    return {"device": device, "mountpoint": target}


def unmount(mountpoint: str) -> Dict[str, Any]:
    mountpoint = os.path.normpath(mountpoint)
    if not mountpoint.startswith(UNMOUNT_PREFIXES):
        raise StorageError("only removable media under /media or /mnt can be unmounted here")
    if not os.path.ismount(mountpoint):
        raise StorageError(f"{mountpoint} is not mounted")
    _run("sync")
    proc = _run("umount", mountpoint)
    if proc.returncode != 0:
        raise StorageError(proc.stderr.strip() or "unmount failed (is it in use?)")
    if mountpoint.startswith(MOUNT_BASE + "/"):
        try:
            os.rmdir(mountpoint)
        except OSError:
            pass
    return {"ok": True, "mountpoint": mountpoint}
