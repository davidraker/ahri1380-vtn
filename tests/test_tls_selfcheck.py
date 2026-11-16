"""
The OpenADR 2.0b cipher-suite guarantee (section 4.5 of the security plan): context building,
the hard start-up check, the loopback self-check, fingerprint helpers and configuration rules.
"""
import pytest

from ahri_vtn import tls
from ahri_vtn.config import Config, ConfigError, load_config

from .conftest import make_config


def test_self_check_negotiates_both_default_suites(pki, tmp_path, event_loop=None):
    import asyncio
    results = asyncio.run(tls.self_check(make_config(pki, tmp_path)))
    assert {r.suite for r in results} == set(tls.OPENADR_SUITES)
    assert all(r.ok for r in results), [(r.suite, r.error) for r in results]
    assert all(r.version == "TLSv1.2" for r in results)


def test_rsa_only_configuration_is_refused_unless_acknowledged(pki, tmp_path):
    cfg = make_config(pki, tmp_path)
    cfg.tls.ecc_cert = cfg.tls.ecc_key = None
    with pytest.raises(tls.TlsSetupError) as err:
        tls.build_openadr_context(cfg.tls)
    assert "TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA256" in str(err.value) and "require_default_suites" in str(err.value)
    cfg.tls.require_default_suites = False
    report = tls.build_openadr_context(cfg.tls)
    assert report.missing_openadr_suites and report.warnings


def test_tls13_only_cannot_offer_the_default_suites(pki, tmp_path):
    cfg = make_config(pki, tmp_path)
    cfg.tls.min_version = "1.3"
    with pytest.raises(tls.TlsSetupError):
        tls.build_openadr_context(cfg.tls)


def test_context_properties(pki, tmp_path):
    import ssl
    report = tls.build_openadr_context(make_config(pki, tmp_path).tls)
    ctx = report.context
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    names = [c["name"] for c in ctx.get_ciphers()]
    assert names.index("ECDHE-ECDSA-AES128-SHA256") < names.index("AES128-SHA256") < len(names)
    assert not any("RC4" in n or "3DES" in n or "MD5" in n for n in names)
    ui = tls.build_ui_context(make_config(pki, tmp_path).ui, make_config(pki, tmp_path).tls)
    assert ui.context.verify_mode == ssl.CERT_OPTIONAL


def test_fingerprint_helpers(pki):
    info = tls.inspect_pem_file(pki.path("ven-sim-hvac.crt"))
    assert len(info.fingerprint) == 29 and info.sha256.endswith(info.fingerprint)
    assert tls.normalize_fingerprint(info.sha256) == info.fingerprint
    assert tls.normalize_fingerprint(info.fingerprint.lower()) == info.fingerprint
    assert tls.normalize_fingerprint(info.fingerprint.replace(":", " ")) == info.fingerprint
    with pytest.raises(ValueError):
        tls.normalize_fingerprint("AB:CD")
    assert info.version == 3 and info.key_type == "RSA" and info.key_bits == 2048 and not info.problems()
    ca = tls.inspect_pem_file(pki.path("lab-ca.crt"))
    assert ca.key_bits == 3072
    vtn = tls.inspect_pem_file(pki.path("vtn-ecc.crt"))
    assert vtn.key_type == "EC" and vtn.key_bits == 256 and set(vtn.sans) == {"localhost", "127.0.0.1"}


def test_certificate_problem_detection():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    weak = tls.CertInfo("CN=x", "CN=ca", now - timedelta(days=1), now + timedelta(days=10), "RSA", 1024,
                        "AA:" * 9 + "AA", "", [], 1, "1")
    codes = {c for c, _, _ in weak.problems(30)}
    assert codes == {"cert-expiring-soon", "cert-key-too-short", "cert-not-x509v3"}
    expired = tls.CertInfo("CN=x", "CN=ca", now - timedelta(days=400), now - timedelta(days=1), "EC", 256, "", "", [], 3, "1")
    assert {c for c, _, _ in expired.problems()} == {"cert-expired"}


# ---------------------------------------------------------------------------- configuration rules

def base(pki, **tls_over):
    t = {"cert": pki.path("vtn-rsa.crt"), "key": pki.path("vtn-rsa.key"), "client_ca_file": pki.path("ven-trust.pem")}
    t.update(tls_over)
    return {"tls": t, "ui": {"operator_ca_file": pki.path("operator-ca.crt"), "basic_auth_user": "lab",
                             "basic_auth_password_hash": "pbkdf2_sha256$1$YQ==$YQ=="}}


def test_configuration_refuses_insecure_or_incomplete_settings(pki):
    Config(**base(pki))
    with pytest.raises(ConfigError, match="requires cert and key"):
        Config(tls={"mode": "mutual"}, ui=base(pki)["ui"])
    with pytest.raises(ConfigError, match="client_ca_file"):
        Config(tls={"cert": pki.path("vtn-rsa.crt"), "key": pki.path("vtn-rsa.key")}, ui=base(pki)["ui"])
    with pytest.raises(ConfigError, match="operator_ca_file"):
        Config(tls=base(pki)["tls"], ui={"basic_auth_user": "lab", "basic_auth_password_hash": "pbkdf2_sha256$1$YQ==$YQ=="})
    with pytest.raises(ConfigError, match="password_auth = true"):
        cfg = base(pki); cfg["ui"]["password_auth"] = False
        Config(**cfg)
    with pytest.raises(ConfigError, match="not a clear-text password"):
        cfg = base(pki); cfg["ui"]["basic_auth_password_hash"] = "secret"
        Config(**cfg)
    with pytest.raises(ConfigError, match="always uses TLS"):
        Config(tls={"mode": "plain"}, ui=base(pki)["ui"])
    with pytest.raises(ConfigError, match="signatures"):
        cfg = base(pki); cfg["tls"]["mode"] = "plain"; cfg["signatures"] = {"mode": "required"}
        Config(**cfg)
    # client_cert required may drop the password entirely
    cfg = base(pki); cfg["ui"].update(client_cert="required", password_auth=False)
    cfg["ui"].pop("basic_auth_user"); cfg["ui"].pop("basic_auth_password_hash")
    assert Config(**cfg).ui.client_cert == "required"
    # plain mode is allowed when stated explicitly (the UI keeps the VTN certificate)
    cfg = base(pki, mode="plain")
    assert Config(**cfg).insecure_transport


def test_old_schema_keys_are_rejected(tmp_path):
    f = tmp_path / "old.toml"
    f.write_text('[tls]\nhttp_cert = "x"\n[ui]\nbasic_auth_password = "secret"\n')
    with pytest.raises(ConfigError, match="pre-release schema"):
        load_config(f)


def test_password_hashing_roundtrip():
    from ahri_vtn.web.auth import hash_password, verify_password
    h = hash_password("correct horse", iterations=2000)
    assert h.startswith("pbkdf2_sha256$2000$") and verify_password("correct horse", h)
    assert not verify_password("wrong", h) and not verify_password("correct horse", "garbage") \
        and not verify_password("x", "pbkdf2_sha256$2000$YQ==$YQ==")


def test_cli_subcommands(pki, tmp_path, capsys, monkeypatch):
    from ahri_vtn.__main__ import main
    monkeypatch.chdir(tmp_path)
    assert main(["certs", "quickstart", "--san", "localhost", "--san", "10.0.0.5"]) == 0
    out = capsys.readouterr().out
    assert "vtn-rsa.crt" in out and "operator_fingerprints" in out
    assert main(["certs", "fingerprint", "certs/ven-sim-hvac.crt"]) == 0
    assert "OpenADR fingerprint" in capsys.readouterr().out
    assert main(["hash-password", "--password", "pw"]) == 0
    hashed = capsys.readouterr().out.strip()
    assert hashed.startswith("pbkdf2_sha256$")
    assert main(["gen-token"]) == 0 and len(capsys.readouterr().out.strip()) > 30
    (tmp_path / "config.toml").write_text(f'''
[tls]
cert = "certs/vtn-rsa.crt"
key = "certs/vtn-rsa.key"
ecc_cert = "certs/vtn-ecc.crt"
ecc_key = "certs/vtn-ecc.key"
client_ca_file = "certs/ven-trust.pem"
[ui]
operator_ca_file = "certs/operator-ca.crt"
basic_auth_user = "lab"
basic_auth_password_hash = "{hashed}"
''')
    assert main(["tls-selfcheck"]) == 0
    out = capsys.readouterr().out
    assert out.count("OK  ") == 2
    assert main(["certs", "show"]) == 0
    assert "lab-ca.crt" in capsys.readouterr().out
    assert main(["certs", "bundle", "add", "certs/ven-sim-hvac.crt"]) == 0
    assert "added" in capsys.readouterr().out
    # An insecure configuration makes `run` exit with status 2 before anything starts
    (tmp_path / "bad.toml").write_text('[tls]\nmode = "mutual"\n')
    assert main(["run", "--config", "bad.toml"]) == 2
    assert "Refusing to start" in capsys.readouterr().err


def test_privileged_port_gives_actionable_message(pki, tmp_path, capsys, monkeypatch):
    """A normal user cannot bind port 1; the CLI must explain the options instead of a traceback."""
    import os
    if os.geteuid() == 0:
        pytest.skip("running as root: privileged ports bind")
    from ahri_vtn.__main__ import main
    monkeypatch.chdir(tmp_path)
    assert main(["certs", "quickstart", "--san", "localhost"]) == 0
    capsys.readouterr()
    from ahri_vtn.web.auth import hash_password
    (tmp_path / "config.toml").write_text(f'''
port = 1
[tls]
cert = "certs/vtn-rsa.crt"
key = "certs/vtn-rsa.key"
ecc_cert = "certs/vtn-ecc.crt"
ecc_key = "certs/vtn-ecc.key"
client_ca_file = "certs/ven-trust.pem"
[ui]
port = 0
operator_ca_file = "certs/operator-ca.crt"
basic_auth_user = "lab"
basic_auth_password_hash = "{hash_password('x', iterations=1000)}"
''')
    assert main(["run", "--config", "config.toml", "--db", f"sqlite:///{tmp_path}/p.sqlite"]) == 2
    err = capsys.readouterr().err
    assert "Permission denied binding port 1" in err and "--port 8443" in err and "CAP_NET_BIND_SERVICE" in err
