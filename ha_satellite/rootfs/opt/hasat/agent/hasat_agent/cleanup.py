"""Remove every trace of HA Satellite from the device.

The installer (and the Docker install button) write install-record.json listing
exactly what they added. Removal undoes that list and nothing else:

host mode (Raspberry Pi OS / Debian / Ubuntu)
  * agent service, unit file, code, certificates, settings and state
  * nftables table, ser2net config (original restored, or ours deleted),
    drives mounted under /media/hasat, temporary files
  * apt packages the installer added, but only those nothing else still needs:
    they are marked "auto" and purged only if apt would autoremove them
  * optionally Docker, if HA Satellite installed it (with all containers/images)

container mode (Synology DSM, ...)
  * the hasat-agent container, its image (and python:3.12-slim if the installer
    downloaded it) and the data folder (e.g. /volume1/docker/hasat-agent)
  * Container Manager / Docker itself is never touched

The final steps run as a transient systemd unit on the host, so they finish after
the agent has stopped.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from typing import Any, Dict, List, Optional

from .settings import STATE

RECORD = STATE / "install-record.json"
HOST_PATHS = [
    "/opt/hasat-agent", "/opt/hasat-agent.old", "/opt/hasat-agent.new",
    "/etc/hasat-agent", "/var/lib/hasat-agent", "/media/hasat",
    "/etc/ser2net.yaml.hasat-orig", "/etc/ser2net.tmp", "/tmp/hasat-get-docker.sh",
]
# Packages the official get.docker.com script installs.
DOCKER_CE_PACKAGES = ["docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin",
                      "docker-compose-plugin", "docker-ce-rootless-extras", "docker-model-plugin"]
DOCKER_PATHS = ["/var/lib/docker", "/var/lib/containerd", "/etc/docker",
                "/etc/apt/sources.list.d/docker.list", "/etc/apt/sources.list.d/docker.sources",
                "/etc/apt/keyrings/docker.asc", "/etc/apt/keyrings/docker.gpg"]


def safe_base(base: Optional[str]) -> bool:
    """Only ever delete a dedicated agent folder, never a volume or system directory."""
    return bool(base) and base.startswith("/") and base.rstrip("/") not in (
        "", "/opt", "/volume1", "/volume1/docker", "/var", "/home", "/root") and base.count("/") >= 2


def load_record() -> Dict[str, Any]:
    try:
        return json.loads(RECORD.read_text())
    except (OSError, ValueError):
        return {}


def save_record(rec: Dict[str, Any]) -> None:
    RECORD.parent.mkdir(parents=True, exist_ok=True)
    RECORD.write_text(json.dumps(rec, indent=2))


def installed_packages() -> List[str]:
    """Currently installed dpkg packages ([] where dpkg is not available)."""
    try:
        out = subprocess.run(["dpkg-query", "-W", "-f=${db:Status-Abbrev} ${Package}\n"],
                             capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({line.split()[1] for line in out.splitlines() if line.startswith("ii") and len(line.split()) > 1})


def note_docker_installed(method: str, packages: List[str]) -> None:
    """Called by the Docker install job (host mode) so removal can undo it on request."""
    rec = load_record()
    rec["docker_installed"] = method
    rec["docker_packages"] = sorted(set(rec.get("docker_packages", [])) | set(packages))
    save_record(rec)


def plan(container: bool, remove_docker: bool, host_base: Optional[str] = None) -> Dict[str, Any]:
    """Human-readable list of what removal will do (shown before confirming)."""
    rec = load_record()
    remove: List[str] = []
    keep: List[str] = []
    if container:
        base = host_base or rec.get("host_base") or "the agent data folder"
        images = rec.get("images") or ["hasat-agent:local"]
        remove += ["the hasat-agent container", "image(s): " + ", ".join(images)]
        if safe_base(base):
            remove.append(f"folder {base} (code, certificates, settings)")
        else:
            keep.append(f"agent data folder {base} (not a dedicated folder; delete it by hand)")
        if "python:3.12-slim" not in images:
            keep.append("python:3.12-slim image (it was already on this device)")
        keep.append("Container Manager / Docker and all your other containers")
    else:
        remove += [
            "hasat-agent service and unit file",
            "agent code, certificates, settings and state (/opt/hasat-agent, /etc/hasat-agent, /var/lib/hasat-agent)",
            "firewall table inet hasat",
            "ser2net radio bridges ("
            + ("original config restored" if rec.get("ser2net_conf_existed", True) else "config removed") + ")",
            "drives mounted by HA Satellite under /media/hasat (unmounted)",
        ]
        pkgs = rec.get("packages_added") or []
        if pkgs:
            remove.append("packages added by the installer, unless something else now needs them: " + ", ".join(pkgs))
        elif not rec:
            keep.append("apt packages (this satellite was installed before HA Satellite recorded what it added)")
        if rec.get("docker_installed"):
            if remove_docker:
                remove.append("Docker (installed by HA Satellite) with ALL containers, images and volumes")
            else:
                keep.append("Docker and your containers (tick the Docker option to remove them too)")
    keep.append("system log entries already written by the agent (shared system journal)")
    return {
        "mode": "container" if container else "host",
        "remove": remove,
        "keep": keep,
        "docker_installed_by_hasat": rec.get("docker_installed") if not container else None,
        "recorded": bool(rec),
    }


def _q(items: List[str]) -> str:
    return " ".join(shlex.quote(i) for i in items)


def _purge_unneeded(pkgs: List[str]) -> List[str]:
    """Shell lines: purge our packages only if apt considers them unneeded."""
    if not pkgs:
        return []
    return [
        f"ours={shlex.quote(chr(10).join(pkgs))}",
        'printf "%s\\n" "$ours" > /tmp/hasat-pkgs',
        "apt-mark auto $ours >/dev/null 2>&1 || true",
        "removable=$(apt-get -s autoremove --purge 2>/dev/null | awk '/^Purg /{sub(/:.*/, \"\", $2); print $2}'"
        " | grep -Fx -f /tmp/hasat-pkgs | tr '\\n' ' ')",
        'if [ -n "$removable" ]; then apt-get purge -y -q $removable; fi',
        "rm -f /tmp/hasat-pkgs",
    ]


def host_script(remove_docker: bool) -> str:
    rec = load_record()
    lines = [
        "set +e",
        "export DEBIAN_FRONTEND=noninteractive",
        "sleep 2",
        "systemctl disable --now hasat-agent 2>/dev/null",
        "rm -f /etc/systemd/system/hasat-agent.service",
        "systemctl daemon-reload",
        "systemctl reset-failed hasat-agent 2>/dev/null",
        "for m in $(awk '$2 ~ \"^/media/hasat/\" {print $2}' /proc/mounts); do umount \"$m\"; done",
        "nft delete table inet hasat 2>/dev/null",
        "if [ -f /etc/ser2net.yaml.hasat-orig ]; then mv -f /etc/ser2net.yaml.hasat-orig /etc/ser2net.yaml;",
    ]
    if rec and not rec.get("ser2net_conf_existed", True):
        lines.append("else rm -f /etc/ser2net.yaml;")
    lines += ["fi", "systemctl try-restart ser2net 2>/dev/null"]
    if remove_docker and rec.get("docker_installed"):
        base_pkgs = DOCKER_CE_PACKAGES if rec["docker_installed"] == "official" else ["docker.io"]
        docker_pkgs = sorted(set(rec.get("docker_packages", [])) | set(base_pkgs))
        lines += [
            "systemctl disable --now docker.socket docker containerd 2>/dev/null",
            f"apt-get purge -y -q {_q(docker_pkgs)} 2>/dev/null",
            f"rm -rf {_q(DOCKER_PATHS)}",
            "groupdel docker 2>/dev/null",
            "apt-get update -q >/dev/null 2>&1",
        ]
    lines += _purge_unneeded(rec.get("packages_added") or [])
    lines.append(f"rm -rf {_q(HOST_PATHS)}")
    return "\n".join(lines) + "\n"


def container_script(container_name: str, host_base: Optional[str]) -> str:
    rec = load_record()
    base = host_base or rec.get("host_base")
    images = rec.get("images") or ["hasat-agent:local"]
    lines = [
        "set +e",
        'export PATH="$PATH:/usr/local/bin:/usr/local/sbin:/usr/syno/bin:/usr/syno/sbin"',
        "sleep 2",
        f"docker rm -f {shlex.quote(container_name)} >/dev/null 2>&1",
        f"docker rmi {_q(images)} >/dev/null 2>&1",
    ]
    if safe_base(base):
        lines.append(f"rm -rf {shlex.quote(base)}")
    return "\n".join(lines) + "\n"
