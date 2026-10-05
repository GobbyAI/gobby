"""Self-signed server certificates for tests that dial a TLS hub."""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def write_self_signed_pair(directory: Path, sans: list[str], *, stem: str = "server") -> Path:
    """Write ``<stem>.crt``/``<stem>.key`` covering ``sans``; return the certificate path.

    Mirrors gdaemon's generated pair: ECDSA P-256, serverAuth, names in SANs only.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    names: list[x509.GeneralName] = []
    for name in sans:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            names.append(x509.DNSName(name))
    now = datetime.datetime.now(datetime.UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "gobby test hub")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / f"{stem}.crt"
    key_path = directory / f"{stem}.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return cert_path


def pem_fingerprint(pem: str) -> str:
    """`sha256:` plus 64 lowercase hex digits over the certificate's DER."""
    return f"sha256:{hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()}"
