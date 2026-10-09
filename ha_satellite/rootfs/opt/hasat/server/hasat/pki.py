"""Private certificate authority used to authenticate the manager and its satellites.

Trust model:
  * The add-on owns a CA whose key never leaves /data/pki.
  * The manager talks to agents with a client certificate (CN=hasat-controller, clientAuth).
  * Each agent generates its own key on the Pi and receives a serverAuth certificate
    (CN=<satellite id>) by submitting a CSR with a one-time enrollment token.
  * Agents only accept connections presenting a CA-signed controller certificate;
    the manager only accepts agents whose certificate CN matches the expected id.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import ipaddress
import os
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CONTROLLER_CN = "hasat-controller"
ENROLL_CN = "hasat-enroll"


def _name(cn: str) -> x509.Name:
    return x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "HA Satellite"),
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
    ])


def _write_private(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    os.chmod(path, 0o600)


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


class PKI:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.ca_key_path = self.dir / "ca.key"
        self.ca_cert_path = self.dir / "ca.crt"
        self.ctl_key_path = self.dir / "controller.key"
        self.ctl_cert_path = self.dir / "controller.crt"
        self.enroll_key_path = self.dir / "enroll.key"
        self.enroll_cert_path = self.dir / "enroll.crt"
        self._ensure()

    # ------------------------------------------------------------------ setup
    def _ensure(self) -> None:
        if not (self.ca_key_path.exists() and self.ca_cert_path.exists()):
            key = ec.generate_private_key(ec.SECP256R1())
            now = dt.datetime.now(dt.timezone.utc)
            cert = (
                x509.CertificateBuilder()
                .subject_name(_name("HA Satellite Root CA"))
                .issuer_name(_name("HA Satellite Root CA"))
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(minutes=5))
                .not_valid_after(now + dt.timedelta(days=365 * 20))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(
                    x509.KeyUsage(
                        digital_signature=True, key_cert_sign=True, crl_sign=True,
                        content_commitment=False, key_encipherment=False,
                        data_encipherment=False, key_agreement=False,
                        encipher_only=False, decipher_only=False,
                    ),
                    critical=True,
                )
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .sign(key, hashes.SHA256())
            )
            _write_private(self.ca_key_path, _key_pem(key))
            self.ca_cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            # A new CA invalidates every leaf certificate.
            for p in (self.ctl_key_path, self.ctl_cert_path, self.enroll_key_path, self.enroll_cert_path):
                p.unlink(missing_ok=True)

        self.ca_key = serialization.load_pem_private_key(self.ca_key_path.read_bytes(), None)
        self.ca_cert = x509.load_pem_x509_certificate(self.ca_cert_path.read_bytes())

        if not self.ctl_cert_path.exists():
            key = ec.generate_private_key(ec.SECP256R1())
            cert = self._issue(key.public_key(), CONTROLLER_CN, ExtendedKeyUsageOID.CLIENT_AUTH)
            _write_private(self.ctl_key_path, _key_pem(key))
            self.ctl_cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

        if not self.enroll_cert_path.exists():
            key = ec.generate_private_key(ec.SECP256R1())
            cert = self._issue(key.public_key(), ENROLL_CN, ExtendedKeyUsageOID.SERVER_AUTH)
            _write_private(self.enroll_key_path, _key_pem(key))
            self.enroll_cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    def _issue(self, public_key, cn: str, eku, ips: list[str] | None = None, days: int = 365 * 10) -> x509.Certificate:
        now = dt.datetime.now(dt.timezone.utc)
        builder = (
            x509.CertificateBuilder()
            .subject_name(_name(cn))
            .issuer_name(self.ca_cert.subject)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self.ca_cert.public_key()), critical=False
            )
        )
        sans: list[x509.GeneralName] = [x509.DNSName(cn)]
        for ip in ips or []:
            try:
                sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
            except ValueError:
                pass
        builder = builder.add_extension(x509.SubjectAlternativeName(sans), critical=False)
        return builder.sign(self.ca_key, hashes.SHA256())

    # ------------------------------------------------------------- operations
    def sign_agent_csr(self, csr_pem: bytes, satellite_id: str, ip: str | None) -> bytes:
        csr = x509.load_pem_x509_csr(csr_pem)
        if not csr.is_signature_valid:
            raise ValueError("CSR signature invalid")
        pub = csr.public_key()
        if isinstance(pub, rsa.RSAPublicKey):
            if pub.key_size < 2048:
                raise ValueError("RSA key too small")
        elif not isinstance(pub, ec.EllipticCurvePublicKey):
            raise ValueError("Unsupported key type")
        cert = self._issue(pub, satellite_id, ExtendedKeyUsageOID.SERVER_AUTH, [ip] if ip else None)
        return cert.public_bytes(serialization.Encoding.PEM)

    @property
    def ca_pem(self) -> bytes:
        return self.ca_cert_path.read_bytes()

    @property
    def ca_fingerprint(self) -> str:
        return self.ca_cert.fingerprint(hashes.SHA256()).hex(":").upper()

    @property
    def enroll_pin(self) -> str:
        """curl --pinnedpubkey value (sha256 of the enrollment server's SubjectPublicKeyInfo)."""
        cert = x509.load_pem_x509_certificate(self.enroll_cert_path.read_bytes())
        spki = cert.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return base64.b64encode(hashlib.sha256(spki).digest()).decode()

    def client_ssl_context(self) -> ssl.SSLContext:
        """Context the manager uses to call agents (mutual TLS, CA-pinned)."""
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_verify_locations(cafile=str(self.ca_cert_path))
        ctx.load_cert_chain(str(self.ctl_cert_path), str(self.ctl_key_path))
        # Agents are addressed by IP (which DHCP may change), so AgentClient passes
        # server_hostname=<satellite id> and TLS verifies the id in the agent's SAN.
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    def enroll_ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(self.enroll_cert_path), str(self.enroll_key_path))
        return ctx
