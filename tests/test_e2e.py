"""End-to-end test: manager <-> agent over real mutual TLS, no Home Assistant needed.

Covers: CA bootstrap, pinned enrollment (script, bundle, CSR, token burn), polling,
UI proxy + allowlist, security rejections, agent self-update, MQTT discovery
payloads and (on Linux) the interactive terminal.

Run:  python tests/test_e2e.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import shutil
import socket
import ssl
import sys
import tarfile
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER_SRC = ROOT / "ha_satellite/rootfs/opt/hasat/server"
AGENT_SRC = ROOT / "ha_satellite/rootfs/opt/hasat/agent"

TMP = Path(tempfile.mkdtemp(prefix="hasat-e2e-"))
for sub in ("data", "etc", "state", "install"):
    (TMP / sub).mkdir()
os.environ.update(
    HASAT_DATA_DIR=str(TMP / "data"),
    HASAT_AGENT_DIR=str(AGENT_SRC),
    HASAT_ETC=str(TMP / "etc"),
    HASAT_STATE=str(TMP / "state"),
    HASAT_INSTALL_DIR=str(TMP / "install" / "hasat-agent"),
)
sys.path[:0] = [str(SERVER_SRC), str(AGENT_SRC)]

IS_POSIX = os.name == "posix"
if not IS_POSIX:  # the PTY terminal is Linux-only; stub its imports so the rest can run
    for name in ("fcntl", "pty", "pwd", "termios"):
        sys.modules.setdefault(name, types.ModuleType(name))

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from hasat import ha_bridge  # noqa: E402
from hasat.config import load_options  # noqa: E402
from hasat.enroll import build_enroll_app  # noqa: E402
from hasat.manager import Manager  # noqa: E402
from hasat.web import build_ui_app  # noqa: E402
from hasat_agent import firewall, serial_bridge  # noqa: E402
from hasat_agent import server as agent_server  # noqa: E402

PASSED: list[str] = []


def check(cond: bool, what: str) -> None:
    if not cond:
        raise AssertionError(what)
    PASSED.append(what)
    print(f"  ok  {what}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def serve(app: web.Application, port: int, ssl_ctx=None) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port, ssl_context=ssl_ctx).start()
    return runner


def peer_spki_pin(port: int) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", port)) as raw, ctx.wrap_socket(raw) as tls:
        der = tls.getpeercert(binary_form=True)
    spki = x509.load_der_x509_certificate(der).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(hashlib.sha256(spki).digest()).decode()


async def main() -> None:
    agent_port, enroll_port, ui_port = free_port(), free_port(), free_port()
    (TMP / "data" / "options.json").write_text(json.dumps({
        "enrollment_host": "127.0.0.1",
        "allowed_networks": ["127.0.0.0/8"],
        "extra_trusted_ips": [],
        "agent_port": agent_port,
        "poll_interval": 3600,
        "auto_update_agents": False,
        "publish_to_ha": False,
        "log_level": "warning",
    }))

    print("manager")
    mgr = Manager(load_options())
    await mgr.client.start()
    mgr.enroll_port = enroll_port
    check((TMP / "data/pki/ca.key").exists(), "CA created in /data/pki")
    check(oct((TMP / "data/pki/ca.key").stat().st_mode)[-3:] in ("600", "666"), "CA key written private")
    enroll_runner = await serve(build_enroll_app(mgr), enroll_port, mgr.pki.enroll_ssl_context())
    ui_runner = await serve(build_ui_app(mgr), ui_port)

    print("enrollment")
    pin = await asyncio.get_running_loop().run_in_executor(None, peer_spki_pin, enroll_port)
    check(pin == mgr.pki.enroll_pin, "enrollment endpoint matches advertised pin")
    tok = mgr.store.new_token("Test Pi")
    cmd = mgr.install_command(tok.token)
    check(f"sha256//{mgr.pki.enroll_pin}" in cmd and "| sudo bash" in cmd, "install command carries pin")

    insecure = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=insecure) as http:
        base = f"https://127.0.0.1:{enroll_port}/enroll"
        async with http.get(f"{base}/not-a-token/install.sh") as r:
            check(r.status == 403, "unknown token rejected")
        async with http.get(f"{base}/{tok.token}/install.sh") as r:
            script = await r.text()
            check(r.status == 200 and "@@" not in script and tok.token in script, "install.sh rendered")
        async with http.get(f"{base}/{tok.token}/agent.tar.gz") as r:
            bundle = await r.read()
        with tarfile.open(fileobj=io.BytesIO(bundle)) as tar:
            names = tar.getnames()
        check("hasat_agent/server.py" in names and "hasat-agent.service" in names
              and "install.sh" not in names, "agent bundle contents")

        key = ec.generate_private_key(ec.SECP256R1())
        csr = (x509.CertificateSigningRequestBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "hasat-pending")]))
               .sign(key, hashes.SHA256()))
        csr_pem = csr.public_bytes(serialization.Encoding.PEM)
        # A reported address outside allowed_networks must be ignored.
        async with http.post(f"{base}/{tok.token}/csr?hostname=testpi&ip=8.8.8.8", data=csr_pem) as r:
            check(r.status == 200, "CSR accepted")
            resp = await r.json()
        async with http.post(f"{base}/{tok.token}/csr?hostname=testpi", data=csr_pem) as r:
            check(r.status == 403, "token is single-use")

    sat = mgr.store.satellites[resp["id"]]
    check(sat.host == "127.0.0.1" and sat.port == agent_port and sat.name == "Test Pi", "satellite registered (foreign reported IP ignored)")
    check(json.loads((TMP / "data/satellites.json").read_text())["satellites"][0]["id"] == sat.id, "registry persisted")
    check(resp["trusted_ips"] == ["127.0.0.1"], "trusted IPs handed to agent")
    cert = x509.load_pem_x509_certificate(resp["cert"].encode())
    check(cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == sat.id, "agent cert CN = satellite id")

    etc = TMP / "etc"
    (etc / "agent.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                      serialization.NoEncryption()))
    (etc / "agent.crt").write_text(resp["cert"])
    (etc / "ca.crt").write_text(resp["ca"])
    (etc / "config.json").write_text(json.dumps({k: resp[k] for k in ("id", "name", "controller_cn", "trusted_ips", "agent_port")}))

    print("agent")
    applied: dict = {}
    serial_bridge.apply = lambda bridges: applied.setdefault("serial", []).append(bridges)  # no systemctl here
    firewall.apply = lambda enabled, trusted, ports: applied.setdefault("firewall", []).append((enabled, list(trusted), sorted(ports)))
    agent = agent_server.Agent()
    restarted = asyncio.Event()
    agent.restart = restarted.set
    agent.apply_network()
    check(applied["firewall"][-1] == (True, ["127.0.0.1"], [agent_port]), "firewall limited to HA host + agent port")
    agent_runner = await serve(agent.build_app(), agent_port, agent_server._ssl_context())

    await mgr.poll_one(sat.id)
    check(sat.online and sat.last_error == "", f"manager polls agent over mTLS ({sat.last_error})")
    m = sat.metrics
    check(m["satellite_id"] == sat.id and "cpu_percent" in m and "mem_percent" in m, "metrics returned")

    print("security")
    async with aiohttp.ClientSession() as http:
        no_cert = ssl.create_default_context(cafile=str(etc / "ca.crt"))
        no_cert.check_hostname = False
        try:
            async with http.get(f"https://127.0.0.1:{agent_port}/api/status", ssl=no_cert) as r:
                rejected = r.status >= 400
        except aiohttp.ClientError:
            rejected = True
        check(rejected, "agent rejects clients without a certificate")

        wrong_cn = ssl.create_default_context(cafile=str(etc / "ca.crt"))
        wrong_cn.check_hostname = False
        wrong_cn.load_cert_chain(str(etc / "agent.crt"), str(etc / "agent.key"))  # CA-signed, but not the controller
        try:
            async with http.get(f"https://127.0.0.1:{agent_port}/api/status", ssl=wrong_cn) as r:
                rejected = r.status == 403
        except aiohttp.ClientError:
            rejected = True
        check(rejected, "agent rejects CA-signed certs that are not the controller")

    impostor = type(sat)(id="sat-deadbeef", name="x", host="127.0.0.1", port=agent_port)
    try:
        await mgr.client.request(impostor, "GET", "/api/status")
        ok = False
    except Exception as err:  # noqa: BLE001
        ok = "certificate" in str(err).lower() or "hostname" in str(err).lower()
    check(ok, "manager refuses an agent whose id does not match")

    print("ui api (ingress)")
    ui = f"http://127.0.0.1:{ui_port}"
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{ui}/api/state") as r:
            st = await r.json()
            check(st["satellites"][0]["online"] and st["controller"]["agent_version"], "state endpoint")
        async with http.get(f"{ui}/") as r:
            check(r.status == 200 and "Satellites" in await r.text(), "UI page served")
        async with http.get(f"{ui}/static/app.js") as r:
            check(r.status == 200, "static assets served")
        async with http.post(f"{ui}/api/enroll", json={"name": "Another"}) as r:
            check("pinnedpubkey" in (await r.json())["command"], "UI generates install command")
        async with http.get(f"{ui}/api/sat/{sat.id}/proxy/usb") as r:
            check(r.status == 200 and "serial" in await r.json(), "proxy: usb listing")
        bridge = {"name": "zwave", "device": "/dev/serial/by-id/usb-Zooz_800_Z-Wave_Stick-if00", "port": 20108, "baud": 115200}
        async with http.put(f"{ui}/api/sat/{sat.id}/proxy/serial", json={"bridges": [bridge]}) as r:
            body = await r.json()
            check(r.status == 200 and body["bridges"][0]["port"] == 20108, "proxy: serial bridge saved")
        check(applied["serial"][-1][0]["device"] == bridge["device"], "ser2net config applied")
        check(applied["firewall"][-1][2] == sorted([agent_port, 20108]), "firewall extended to bridge port")
        bad = dict(bridge, device="/dev/ttyUSB0,9600 ; rm -rf /")
        async with http.put(f"{ui}/api/sat/{sat.id}/proxy/serial", json={"bridges": [bad]}) as r:
            check(r.status == 502 and "invalid device" in (await r.json())["error"], "bridge input validated")
        async with http.put(f"{ui}/api/sat/{sat.id}/proxy/settings", json={"auto_update": {"time": "03:30", "days": [5, 6], "auto_reboot": True}}) as r:
            s = await r.json()
            check(s["auto_update"]["time"] == "03:30" and s["auto_update"]["days"] == [5, 6], "update schedule saved")
        async with http.post(f"{ui}/api/sat/{sat.id}/proxy/agent/uninstall") as r:
            check(r.status == 403, "proxy blocks non-allowlisted agent endpoints")
        if IS_POSIX:
            async with http.post(f"{ui}/api/sat/{sat.id}/proxy/exec", json={"command": "echo hello-$((40+2))"}) as r:
                out = await r.json()
                check(out["rc"] == 0 and "hello-42" in out["output"], "command runner")
            async with http.ws_connect(f"{ui}/api/sat/{sat.id}/terminal") as ws:
                await ws.send_str(json.dumps({"t": "r", "c": 100, "r": 30}))
                await ws.send_str(json.dumps({"t": "i", "d": "echo term-$((6*7))\n"}))
                seen = ""
                async def read():
                    nonlocal seen
                    async for msg in ws:
                        seen += msg.data.decode(errors="ignore") if isinstance(msg.data, bytes) else str(msg.data)
                        if "term-42" in seen:
                            return
                await asyncio.wait_for(read(), 15)
                check("term-42" in seen, "interactive terminal through ingress proxy")

    print("agent self-update")
    result = await mgr.push_agent_update(sat)
    await asyncio.wait_for(restarted.wait(), 5)
    installed = TMP / "install" / "hasat-agent" / "hasat_agent" / "__init__.py"
    check(result.get("restarting") and installed.exists(), "agent bundle installed and restart triggered")

    print("home assistant mqtt discovery")
    published: dict = {}

    class FakeMqtt:
        def publish(self, topic, payload, retain=False):
            published[topic] = payload

    bridge_obj = ha_bridge.HABridge(lambda *_: asyncio.sleep(0))
    bridge_obj._mqtt, bridge_obj._mqtt_connected = FakeMqtt(), True
    bridge_obj._publish_mqtt(sat)
    cfg = json.loads(published[f"homeassistant/sensor/hasat_{sat.id}/cpu_temp/config"])
    check(cfg["unique_id"] == f"hasat_{sat.id}_cpu_temp" and cfg["device_class"] == "temperature", "discovery config")
    check(f"homeassistant/button/hasat_{sat.id}/reboot/config" in published, "reboot button announced")
    state = json.loads(published[f"hasat/{sat.id}/state"])
    check("cpu_percent" in state and published[f"hasat/{sat.id}/availability"] == "online", "state + availability")

    await agent_runner.cleanup()
    await mgr.poll_one(sat.id)
    check(not sat.online, "offline detected when agent stops")

    await ui_runner.cleanup()
    await enroll_runner.cleanup()
    await mgr.client.close()
    print(f"\nALL {len(PASSED)} CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
