"""File-system browser and editor for the satellite.

The agent runs as root (like the web terminal), so these endpoints can reach the
whole file system. Guard rails: absolute paths only, no writes into /proc, /sys or
/dev, top-level system directories cannot be deleted or renamed, text editing is
limited to small UTF-8 files and saves detect concurrent changes.
"""

from __future__ import annotations

import grp
import os
import pwd
import shutil
import stat
import tempfile
from typing import Any, Dict, List, Optional

MAX_EDIT_BYTES = 2 * 1024 * 1024
MAX_LIST = 5000
PSEUDO_FS = ("/proc", "/sys", "/dev")
PROTECTED = {
    "/", "/bin", "/boot", "/boot/firmware", "/dev", "/etc", "/home", "/lib", "/lib64", "/media",
    "/mnt", "/opt", "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/tmp", "/usr", "/var",
}


class FileError(ValueError):
    """Raised for user errors; mapped to HTTP 400 by the server."""


def norm(path: str) -> str:
    if not path or not (path.startswith("/") or os.path.isabs(path)) or "\x00" in path:
        raise FileError("an absolute path is required")
    return os.path.normpath(path).replace("\\", "/") if os.sep == "\\" else os.path.normpath(path)


def _owner(uid: int, gid: int) -> str:
    try:
        user = pwd.getpwuid(uid).pw_name
    except (KeyError, AttributeError):
        user = str(uid)
    try:
        group = grp.getgrgid(gid).gr_name
    except (KeyError, AttributeError):
        group = str(gid)
    return f"{user}:{group}"


def _entry(path: str, name: str) -> Dict[str, Any]:
    full = os.path.join(path, name)
    try:
        st = os.lstat(full)
    except OSError:
        return {"name": name, "type": "unknown", "size": 0, "mtime": 0, "mode": "?", "owner": ""}
    kind = "dir" if stat.S_ISDIR(st.st_mode) else "link" if stat.S_ISLNK(st.st_mode) else "file"
    item: Dict[str, Any] = {
        "name": name, "type": kind, "size": st.st_size, "mtime": st.st_mtime,
        "mode": stat.filemode(st.st_mode), "owner": _owner(st.st_uid, st.st_gid),
    }
    if kind == "link":
        try:
            item["target"] = os.readlink(full)
            item["target_is_dir"] = os.path.isdir(full)
        except OSError:
            item["target"] = "?"
    return item


def list_dir(path: str) -> Dict[str, Any]:
    path = norm(path)
    if not os.path.isdir(path):
        raise FileError(f"not a directory: {path}")
    try:
        names = os.listdir(path)
    except PermissionError as err:
        raise FileError(f"permission denied: {path}") from err
    truncated = len(names) > MAX_LIST
    entries = [_entry(path, n) for n in sorted(names)[:MAX_LIST]]
    entries.sort(key=lambda e: (not (e["type"] == "dir" or e.get("target_is_dir")), e["name"].lower()))
    usage: Optional[Dict[str, int]] = None
    try:
        du = shutil.disk_usage(path)
        usage = {"total": du.total, "used": du.used, "free": du.free}
    except OSError:
        pass
    return {"path": path, "parent": os.path.dirname(path) if path != "/" else None,
            "entries": entries, "truncated": truncated, "usage": usage}


def read_text(path: str) -> Dict[str, Any]:
    path = norm(path)
    if not os.path.isfile(path):
        raise FileError(f"not a regular file: {path}")
    st = os.stat(path)
    info: Dict[str, Any] = {"path": path, "size": st.st_size, "mtime": st.st_mtime, "mode": stat.filemode(st.st_mode)}
    if st.st_size > MAX_EDIT_BYTES:
        return dict(info, editable=False, reason="file is larger than 2 MB; download it instead")
    with open(path, "rb") as fh:
        data = fh.read(MAX_EDIT_BYTES + 1)
    if b"\x00" in data[:8192]:
        return dict(info, editable=False, reason="binary file; download it instead")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return dict(info, editable=False, reason="not UTF-8 text; download it instead")
    return dict(info, editable=True, content=text)


def _check_writable(path: str) -> None:
    if any(path == p or path.startswith(p + "/") for p in PSEUDO_FS):
        raise FileError(f"refusing to modify {path} (kernel pseudo file system)")


def write_text(path: str, content: str, expect_mtime: Optional[float] = None, create: bool = False) -> Dict[str, Any]:
    path = norm(path)
    _check_writable(path)
    exists = os.path.exists(path)
    if exists and not os.path.isfile(path):
        raise FileError("not a regular file")
    if create and exists:
        raise FileError(f"{os.path.basename(path)} already exists")
    if not create and not exists:
        raise FileError("file no longer exists")
    if exists and expect_mtime is not None and abs(os.stat(path).st_mtime - float(expect_mtime)) > 1e-3:
        raise RuntimeError("the file was changed on the satellite since you opened it; reload before saving")
    directory = os.path.dirname(path)
    st = os.stat(path) if exists else None
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".hasat-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content.encode("utf-8"))
        if st is not None:
            os.chmod(tmp, stat.S_IMODE(st.st_mode))
            if hasattr(os, "chown"):
                try:
                    os.chown(tmp, st.st_uid, st.st_gid)
                except OSError:
                    pass
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    new = os.stat(path)
    return {"path": path, "size": new.st_size, "mtime": new.st_mtime}


def mkdir(path: str) -> Dict[str, Any]:
    path = norm(path)
    _check_writable(path)
    if os.path.exists(path):
        raise FileError(f"{path} already exists")
    os.mkdir(path, 0o755)
    return {"path": path}


def rename(src: str, dst: str) -> Dict[str, Any]:
    src, dst = norm(src), norm(dst)
    for p in (src, dst):
        _check_writable(p)
    if src in PROTECTED:
        raise FileError(f"refusing to rename system directory {src}")
    if not os.path.lexists(src):
        raise FileError(f"{src} does not exist")
    if os.path.lexists(dst):
        raise FileError(f"{dst} already exists")
    os.rename(src, dst)
    return {"path": dst}


def delete(path: str, recursive: bool = False) -> Dict[str, Any]:
    path = norm(path)
    _check_writable(path)
    if path in PROTECTED:
        raise FileError(f"refusing to delete system directory {path}")
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        if recursive:
            shutil.rmtree(path)
        else:
            try:
                os.rmdir(path)
            except OSError as err:
                raise FileError("directory is not empty") from err
    else:
        raise FileError(f"{path} does not exist")
    return {"ok": True}


def upload_target(path: str, overwrite: bool) -> str:
    path = norm(path)
    _check_writable(path)
    if not os.path.isdir(os.path.dirname(path)):
        raise FileError("target directory does not exist")
    if os.path.isdir(path):
        raise FileError("a directory with that name exists")
    if os.path.exists(path) and not overwrite:
        raise FileError(f"{os.path.basename(path)} already exists")
    return path


def download_source(path: str) -> str:
    path = norm(path)
    if not os.path.isfile(path):
        raise FileError("not a regular file")
    return path


def quick_links() -> List[str]:
    links = ["/", "/home", "/etc", "/opt", "/var/log", "/media", "/mnt"]
    return [p for p in links if os.path.isdir(p)]
