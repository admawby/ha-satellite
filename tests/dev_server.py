"""Local UI preview: runs the manager plus one loop-back agent (no HA, no systemctl/nft).

    python tests/dev_server.py      ->  http://127.0.0.1:8099/
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import test_e2e as t  # noqa: E402  (sets up temp dirs, env and import paths)


async def main() -> None:
    import json

    agent_port, enroll_port = t.free_port(), t.free_port()
    (t.TMP / "data" / "options.json").write_text(json.dumps({
        "enrollment_host": "127.0.0.1", "allowed_networks": ["127.0.0.0/8"], "agent_port": agent_port,
        "poll_interval": 10, "auto_update_agents": False, "publish_to_ha": False, "log_level": "info",
    }))
    mgr = t.Manager(t.load_options())
    await mgr.client.start()
    mgr.enroll_port = enroll_port
    await t.serve(t.build_enroll_app(mgr), enroll_port, mgr.pki.enroll_ssl_context())
    await t.serve(t.build_ui_app(mgr), 8099)

    # Enroll a loop-back agent exactly like install.sh would.
    tok = mgr.store.new_token("Garage Pi")
    key = t.ec.generate_private_key(t.ec.SECP256R1())
    csr = (t.x509.CertificateSigningRequestBuilder()
           .subject_name(t.x509.Name([t.x509.NameAttribute(t.NameOID.COMMON_NAME, "x")]))
           .sign(key, t.hashes.SHA256()).public_bytes(t.serialization.Encoding.PEM))
    async with t.aiohttp.ClientSession(connector=t.aiohttp.TCPConnector(ssl=False)) as http:
        async with http.post(f"https://127.0.0.1:{enroll_port}/enroll/{tok.token}/csr?hostname=garagepi", data=csr) as r:
            resp = await r.json()
    etc = t.TMP / "etc"
    (etc / "agent.key").write_bytes(key.private_bytes(t.serialization.Encoding.PEM, t.serialization.PrivateFormat.PKCS8,
                                                      t.serialization.NoEncryption()))
    (etc / "agent.crt").write_text(resp["cert"])
    (etc / "ca.crt").write_text(resp["ca"])
    (etc / "config.json").write_text(json.dumps({k: resp[k] for k in ("id", "name", "controller_cn", "trusted_ips", "agent_port")}))
    t.serial_bridge.apply = lambda bridges: None
    t.firewall.apply = lambda *a: None
    agent = t.agent_server.Agent()
    await t.serve(agent.build_app(), agent_port, t.agent_server._ssl_context())
    await mgr.poll_one(resp["id"])
    print("UI ready at http://127.0.0.1:8099/")
    while True:
        await asyncio.sleep(10)
        await mgr.poll_one(resp["id"])


if __name__ == "__main__":
    asyncio.run(main())
