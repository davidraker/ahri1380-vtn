"""
Lab PKI: `ahri-vtn certs ...` creates the certificate authority, VTN server certificates (RSA
and ECC, OpenADR 2.0b rule 68), VEN client certificates for the simulator and prototypes,
operator certificates for the UI, and maintains the VEN trust bundle.

Everything is built with `cryptography`; private keys are written with mode 0600.
"""
from __future__ import annotations

import datetime
import ipaddress
import os
import secrets
import sys
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .tls import inspect_pem_file, normalize_fingerprint, pem_certificates

DEFAULT_DIR = Path("certs")
CA_DAYS = 3650
LEAF_DAYS = 825          # browsers reject longer-lived leaf certificates

README = """\
AHRI 1380 VTN lab certificates
==============================

lab-ca.crt / lab-ca.key         Lab certificate authority for VTN and VEN certificates.
operator-ca.crt / operator-ca.key  Separate CA for operator (browser / script) certificates.
vtn-rsa.crt / vtn-rsa.key       VTN server certificate, RSA 2048  (TLS_RSA_WITH_AES_128_CBC_SHA256)
vtn-ecc.crt / vtn-ecc.key       VTN server certificate, ECC P-256 (TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA256)
ven-trust.pem                   Trust bundle for VEN client certificates: the lab CA plus any
                                vendor CA or self-signed device certificate added with
                                `ahri-vtn certs bundle add <pem>`.
ven-<name>.crt / .key / .p12    VEN client certificates (simulator, prototypes).
operator-<name>.crt / .key / .p12  Operator certificates; import the .p12 into the browser.

Give the vendor:   lab-ca.crt (so the unit trusts the VTN), or vtn-rsa.crt / vtn-ecc.crt if the
                   unit pins a certificate, plus the VTN URL. Never a .key file.
Ask the vendor for: the unit's certificate (or its CA certificate) and the unit's certificate
                   fingerprint (OpenADR 2.0b 10.5.1), and pin it on the VEN page.
"""


class CertsError(RuntimeError):
    pass


# ---------------------------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------------------------

def _name(cn: str, org: str = "AHRI 1380 test tool") -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, org),
                      x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _rsa(bits: int):
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def _ecc():
    return ec.generate_private_key(ec.SECP256R1())


def _write_key(path: Path, key, passphrase: Optional[str] = None):
    enc = serialization.BestAvailableEncryption(passphrase.encode()) if passphrase else serialization.NoEncryption()
    data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(path, 0o600)


def _write_cert(path: Path, cert: x509.Certificate):
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _load_key(path: Path, passphrase: Optional[str] = None):
    return serialization.load_pem_private_key(path.read_bytes(), passphrase.encode() if passphrase else None)


def _load_cert(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def _validity(days: int):
    now = datetime.datetime.now(datetime.timezone.utc)
    return now - datetime.timedelta(minutes=5), now + datetime.timedelta(days=days)


def _ca_cert(key, cn: str, days: int = CA_DAYS) -> x509.Certificate:
    nb, na = _validity(days)
    name = _name(cn)
    return (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(nb).not_valid_after(na)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))


def _leaf(key, cn: str, ca_key, ca_cert: x509.Certificate, *, server: bool, sans: Optional[list] = None,
          days: int = LEAF_DAYS) -> x509.Certificate:
    nb, na = _validity(days)
    builder = (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(ca_cert.subject)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(nb).not_valid_after(na)
               .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=isinstance(key, rsa.RSAPrivateKey),
                                            content_commitment=False, data_encipherment=False, key_agreement=False,
                                            key_cert_sign=False, crl_sign=False, encipher_only=False,
                                            decipher_only=False), critical=True)
               .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH if server
                                                     else ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False))
    if sans:
        entries = []
        for san in sans:
            try:
                entries.append(x509.IPAddress(ipaddress.ip_address(san)))
            except ValueError:
                entries.append(x509.DNSName(san))
        builder = builder.add_extension(x509.SubjectAlternativeName(entries), critical=False)
    return builder.sign(ca_key, hashes.SHA256())


def _p12(path: Path, name: str, key, cert: x509.Certificate, ca_cert: x509.Certificate, password: str):
    data = pkcs12.serialize_key_and_certificates(name.encode(), key, cert, [ca_cert],
                                                 serialization.BestAvailableEncryption(password.encode()))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


# ---------------------------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------------------------

class LabPki:
    def __init__(self, directory: Path = DEFAULT_DIR, out=None):
        self.dir = Path(directory)
        self.out = out          # None: whatever sys.stdout is at call time

    def _say(self, text: str):
        print(text, file=self.out or sys.stdout)

    def _require(self, name: str) -> Path:
        path = self.dir / name
        if not path.exists():
            raise CertsError(f"{path} does not exist; run `ahri-vtn certs init-ca` first")
        return path

    def init_ca(self, name: str = "AHRI 1380 Lab CA", ecc: bool = False, force: bool = False):
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        for stem, cn in (("lab-ca", name), ("operator-ca", f"{name} operators")):
            crt, key_path = self.dir / f"{stem}.crt", self.dir / f"{stem}.key"
            if crt.exists() and not force:
                self._say(f"{crt} exists, keeping it (use --force to replace)")
                continue
            key = _ecc() if ecc else _rsa(3072)
            _write_key(key_path, key)
            _write_cert(crt, _ca_cert(key, cn))
            self._say(f"created {crt} ({'ECC P-256' if ecc else 'RSA 3072'}, {CA_DAYS} days)")
        bundle = self.dir / "ven-trust.pem"
        if not bundle.exists() or force:
            bundle.write_bytes((self.dir / "lab-ca.crt").read_bytes())
            self._say(f"created {bundle} (contains the lab CA)")
        (self.dir / "README.txt").write_text(README)
        return self.dir

    def vtn(self, sans: list, days: int = LEAF_DAYS):
        ca_key = _load_key(self._require("lab-ca.key"))
        ca_cert = _load_cert(self._require("lab-ca.crt"))
        sans = list(dict.fromkeys(sans or ["localhost", "127.0.0.1"]))
        results = {}
        for stem, key in (("vtn-rsa", _rsa(2048)), ("vtn-ecc", _ecc())):
            cert = _leaf(key, sans[0], ca_key, ca_cert, server=True, sans=sans, days=days)
            _write_key(self.dir / f"{stem}.key", key)
            _write_cert(self.dir / f"{stem}.crt", cert)
            info = inspect_pem_file(self.dir / f"{stem}.crt")
            results[stem] = info
            self._say(f"created {self.dir / (stem + '.crt')}: {info.key_type} {info.key_bits} bits, "
                      f"SAN {', '.join(info.sans)}, fingerprint {info.fingerprint}")
        return results

    def ven(self, name: str, ecc: bool = False, p12_password: Optional[str] = None, days: int = LEAF_DAYS):
        ca_key = _load_key(self._require("lab-ca.key"))
        ca_cert = _load_cert(self._require("lab-ca.crt"))
        key = _ecc() if ecc else _rsa(2048)
        cert = _leaf(key, name, ca_key, ca_cert, server=False, days=days)
        stem = f"ven-{name}"
        _write_key(self.dir / f"{stem}.key", key)
        _write_cert(self.dir / f"{stem}.crt", cert)
        info = inspect_pem_file(self.dir / f"{stem}.crt")
        if p12_password is not None:
            _p12(self.dir / f"{stem}.p12", name, key, cert, ca_cert, p12_password)
        self._say(f"created {self.dir / (stem + '.crt')}: {info.key_type} {info.key_bits} bits; "
                  f"OpenADR fingerprint {info.fingerprint}")
        return info

    def operator(self, name: str, p12_password: Optional[str] = None, days: int = LEAF_DAYS):
        ca_key = _load_key(self._require("operator-ca.key"))
        ca_cert = _load_cert(self._require("operator-ca.crt"))
        key = _rsa(2048)
        cert = _leaf(key, name, ca_key, ca_cert, server=False, days=days)
        stem = f"operator-{name}"
        _write_key(self.dir / f"{stem}.key", key)
        _write_cert(self.dir / f"{stem}.crt", cert)
        password = p12_password if p12_password is not None else secrets.token_urlsafe(9)
        _p12(self.dir / f"{stem}.p12", name, key, cert, ca_cert, password)
        info = inspect_pem_file(self.dir / f"{stem}.crt")
        self._say(f"created {self.dir / (stem + '.crt')} and {stem}.p12 (import password: {password})\n"
                  f"fingerprint for [ui] operator_fingerprints: {info.fingerprint}")
        return info, password

    def bundle_add(self, pem_path: str):
        bundle = self.dir / "ven-trust.pem"
        if not bundle.exists():
            raise CertsError(f"{bundle} does not exist; run `ahri-vtn certs init-ca` first")
        certs = pem_certificates(pem_path)
        if not certs:
            raise CertsError(f"{pem_path} contains no PEM certificate")
        existing = {c.sha256 for c in pem_certificates(bundle)}
        added = 0
        with bundle.open("ab") as fh:
            for cert in x509.load_pem_x509_certificates(Path(pem_path).read_bytes()):
                info = inspect_pem_bytes(cert)
                if info.sha256 in existing:
                    self._say(f"already in the bundle: {info.subject}")
                    continue
                fh.write(b"\n" + cert.public_bytes(serialization.Encoding.PEM))
                added += 1
                self._say(f"added {info.subject} (OpenADR fingerprint {info.fingerprint}, expires {info.not_after:%Y-%m-%d})")
        return added

    def fingerprint(self, pem_path: str):
        for info in pem_certificates(pem_path):
            self._say(f"{info.subject}\n  OpenADR fingerprint: {info.fingerprint}\n  SHA-256:             {info.sha256}")
        return [c.fingerprint for c in pem_certificates(pem_path)]

    def show(self, config=None):
        rows = []
        paths = sorted(self.dir.glob("*.crt")) + sorted(self.dir.glob("*.pem"))
        if config is not None:
            extra = [config.tls.cert, config.tls.ecc_cert, config.tls.client_ca_file, config.ui.cert,
                     config.ui.operator_ca_file, config.signatures.cert]
            paths += [Path(p) for p in extra if p and Path(p).exists() and Path(p) not in paths]
        for path in paths:
            for info in pem_certificates(path):
                flag = "EXPIRED" if info.expired else ("expiring soon" if info.expires_within(30) else "")
                rows.append((str(path), info))
                self._say(f"{path}: {info.subject}  {info.key_type} {info.key_bits}  "
                          f"{info.not_before:%Y-%m-%d}..{info.not_after:%Y-%m-%d} {flag}\n"
                          f"    SAN {', '.join(info.sans) or '-'}  fingerprint {info.fingerprint}")
        return rows

    def quickstart(self, sans: list, operator: str = "lab", ven: str = "sim-hvac", force: bool = False):
        """Everything a development bench needs in one go."""
        self.init_ca(force=force)
        self.vtn(sans)
        self.ven(ven)
        info, password = self.operator(operator)
        self._say("\nConfiguration snippet (config.toml):\n")
        self._say(config_snippet(self.dir, operator_fingerprint=info.fingerprint))
        return password


def inspect_pem_bytes(cert: x509.Certificate):
    from .tls import _inspect, _DER
    return _inspect(cert, cert.public_bytes(_DER))


def config_snippet(directory: Path, operator_fingerprint: Optional[str] = None) -> str:
    d = str(directory).rstrip("/")
    fp = f'operator_fingerprints = ["{operator_fingerprint}"]' if operator_fingerprint else "operator_fingerprints = []"
    return f"""[tls]
mode = "mutual"
cert = "{d}/vtn-rsa.crt"
key  = "{d}/vtn-rsa.key"
ecc_cert = "{d}/vtn-ecc.crt"
ecc_key  = "{d}/vtn-ecc.key"
client_ca_file = "{d}/ven-trust.pem"

[ui]
operator_ca_file = "{d}/operator-ca.crt"
{fp}
basic_auth_user = "lab"
# basic_auth_password_hash = "...   (ahri-vtn hash-password)"
"""


def validate_fingerprint_input(value: str) -> str:
    """Normalise an operator-supplied fingerprint (29-char OpenADR form or SHA-256)."""
    return normalize_fingerprint(value)
