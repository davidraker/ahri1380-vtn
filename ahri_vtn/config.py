"""
Configuration for the AHRI 1380 VTN test tool (TOML file + environment overrides).

Secure by default: the OpenADR listener runs mutual TLS and the operator UI is a separate TLS
listener that always requires authentication. Plain HTTP exists only as an explicitly enabled
development mode (`[tls] mode = "plain"`).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

try:                        # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # Python 3.10
    import tomli as _toml   # type: ignore

logger = logging.getLogger("ahri_vtn.config")

ENV_PREFIX = "AHRI_VTN_"

# Keys of the pre-deployment configuration schema; named in the error when they are still used.
_OLD_TLS_KEYS = ("http_cert", "http_key", "http_ca_file", "http_key_passphrase",
                 "signing_cert", "signing_key", "signing_passphrase")
_OLD_UI_KEYS = ("basic_auth_password", "listener", "auth", "tls", "allow_basic_auth_over_http")


class ConfigError(Exception):
    """A configuration that must not be run with. (Not a ValueError, so pydantic does not wrap it.)"""


class Thresholds(BaseModel):
    general_curtailment_fraction: float = 0.70
    critical_curtailment_fraction: float = 0.40
    off_mode_max_watts: float = 200.0
    stabilization_seconds: int = 120
    sample_count: int = 20
    sample_interval_seconds: int = 60
    event_response_timeout_seconds: int = 60
    override_timeout_seconds: int = 600
    status_interval_tolerance: float = 0.5
    max_mito_degrees_f: int = 4


class TlsConfig(BaseModel):
    """Transport security of the OpenADR listener."""
    model_config = ConfigDict(extra="forbid")

    # mutual: OpenADR 2.0b "Standard" security (client certificate required, default)
    # server: TLS without a client certificate (prototypes that cannot present one yet)
    # plain:  development only; logged and shown in the UI as insecure
    mode: Literal["mutual", "server", "plain"] = "mutual"
    cert: Optional[str] = None              # server certificate chain (PEM), RSA
    key: Optional[str] = None
    key_passphrase_env: Optional[str] = None
    ecc_cert: Optional[str] = None          # optional second chain (ECDSA) so both 2.0b suites work
    ecc_key: Optional[str] = None
    ecc_key_passphrase_env: Optional[str] = None
    client_ca_file: Optional[str] = None    # trust bundle for VEN certificates
    min_version: Literal["1.2", "1.3"] = "1.2"
    ciphers: str = "openadr-default"
    fingerprint_policy: Literal["pinned", "first-use", "ca-only"] = "pinned"
    expiry_warning_days: int = 30
    require_default_suites: bool = True

    @property
    def enabled(self) -> bool:
        return self.mode != "plain"

    @property
    def client_cert_required(self) -> bool:
        return self.mode == "mutual"

    def key_passphrase(self) -> Optional[str]:
        return os.environ.get(self.key_passphrase_env) if self.key_passphrase_env else None

    def ecc_key_passphrase(self) -> Optional[str]:
        return os.environ.get(self.ecc_key_passphrase_env) if self.ecc_key_passphrase_env else None


class SignaturesConfig(BaseModel):
    """OpenADR 2.0b "High" security (XML signatures). AHRI Table 4 says N, so the default is none."""
    model_config = ConfigDict(extra="forbid")

    mode: Literal["none", "optional", "required"] = "none"
    cert: Optional[str] = None
    key: Optional[str] = None
    key_passphrase_env: Optional[str] = None

    def key_passphrase(self) -> Optional[str]:
        return os.environ.get(self.key_passphrase_env) if self.key_passphrase_env else None


class UiConfig(BaseModel):
    """The operator UI / API listener: always separate, always TLS, always authenticated."""
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = 9443
    cert: Optional[str] = None              # empty: reuse the VTN server certificate(s)
    key: Optional[str] = None
    key_passphrase_env: Optional[str] = None
    client_cert: Literal["optional", "required"] = "optional"
    operator_ca_file: Optional[str] = None
    operator_fingerprints: list[str] = Field(default_factory=list)
    password_auth: bool = True
    basic_auth_user: Optional[str] = None
    basic_auth_password_hash: Optional[SecretStr] = None
    api_tokens_env: Optional[str] = None
    max_sse_clients: int = 20

    def key_passphrase(self) -> Optional[str]:
        return os.environ.get(self.key_passphrase_env) if self.key_passphrase_env else None

    def api_tokens(self) -> list[str]:
        if not self.api_tokens_env:
            return []
        raw = os.environ.get(self.api_tokens_env, "")
        return [t.strip() for t in raw.split(",") if t.strip()]


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_vens_auto_accept: int = 50
    max_message_body_bytes: int = 1024 * 1024
    requests_per_minute_per_ip: int = 600
    auth_failures_per_minute: int = 5


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vtn_id: str = "ahri1380-vtn"
    host: str = "0.0.0.0"
    port: int = 443
    http_path_prefix: str = "/OpenADR2/Simple/2.0b"
    db_url: str = "sqlite:///ahri_vtn.sqlite"      # or postgresql://user@host:5432/ahri_vtn
    db_password_env: Optional[str] = None           # variable holding the database password (never in the URL)
    db_pool_size: int = 5
    requested_poll_freq_seconds: int = 10
    auto_accept_unknown_vens: bool = True
    schema_mode: Literal["strict", "lenient"] = "lenient"
    auto_request_reports: bool = True
    status_report_interval_seconds: int = 60
    offline_after_polls: int = 3
    market_context: str = "oadr://ahri1380"
    default_event_duration_minutes: int = 30
    log_level: str = "INFO"
    thresholds: Thresholds = Field(default_factory=Thresholds)
    tls: TlsConfig = Field(default_factory=TlsConfig)
    signatures: SignaturesConfig = Field(default_factory=SignaturesConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)

    # ------------------------------------------------------------------ validation

    @model_validator(mode="after")
    def _check_security(self):
        problems = []
        tls, ui, sig = self.tls, self.ui, self.signatures
        if tls.mode != "plain":
            if not (tls.cert and tls.key):
                problems.append(f"[tls] mode = \"{tls.mode}\" requires cert and key "
                                "(run `ahri-vtn certs quickstart` to create a lab PKI, or set mode = \"plain\" "
                                "explicitly for development against the simulator)")
            if tls.mode == "mutual" and not tls.client_ca_file:
                problems.append("[tls] mode = \"mutual\" requires client_ca_file (the trust bundle for VEN certificates)")
        if bool(tls.ecc_cert) != bool(tls.ecc_key):
            problems.append("[tls] ecc_cert and ecc_key must be given together")
        if not ((ui.cert and ui.key) or (tls.cert and tls.key)):
            problems.append("the UI listener always uses TLS: set [ui] cert/key or [tls] cert/key")
        if bool(ui.cert) != bool(ui.key):
            problems.append("[ui] cert and key must be given together")
        if not ui.operator_ca_file:
            problems.append("[ui] operator_ca_file is required (operator client certificates are always accepted)")
        if ui.client_cert == "optional" and not ui.password_auth:
            problems.append("[ui] client_cert = \"optional\" requires password_auth = true "
                            "(otherwise a browser without a certificate could never log in)")
        if ui.password_auth and not (ui.basic_auth_user and ui.basic_auth_password_hash):
            problems.append("[ui] password_auth = true requires basic_auth_user and basic_auth_password_hash "
                            "(create the hash with `ahri-vtn hash-password`)")
        if ui.basic_auth_password_hash and not ui.basic_auth_password_hash.get_secret_value().startswith("pbkdf2_sha256$"):
            problems.append("[ui] basic_auth_password_hash must be a `pbkdf2_sha256$...` hash from `ahri-vtn hash-password`, "
                            "not a clear-text password")
        if sig.mode != "none" and tls.mode == "plain":
            problems.append("[signatures] mode requires TLS (signatures are only verified on TLS connections)")
        if sig.mode != "none" and bool(sig.cert) != bool(sig.key):
            problems.append("[signatures] cert and key must be given together")
        if tls.mode == "plain" and tls.fingerprint_policy == "pinned":
            # Nothing to pin without client certificates; keep the setting but it has no effect.
            pass
        if problems:
            raise ConfigError("Refusing to start with an insecure or incomplete configuration:\n  - "
                              + "\n  - ".join(problems))
        return self

    # ------------------------------------------------------------------ derived values

    def db_password(self) -> Optional[str]:
        return os.environ.get(self.db_password_env) if self.db_password_env else None

    @property
    def openadr_scheme(self) -> str:
        return "http" if self.tls.mode == "plain" else "https"

    @property
    def openadr_url(self) -> str:
        host = "localhost" if self.host in ("0.0.0.0", "") else self.host
        default_port = 80 if self.openadr_scheme == "http" else 443
        port = "" if self.port == default_port else f":{self.port}"
        return f"{self.openadr_scheme}://{host}{port}{self.http_path_prefix}"

    @property
    def ui_url(self) -> str:
        host = "localhost" if self.ui.host in ("0.0.0.0", "") else self.ui.host
        port = "" if self.ui.port == 443 else f":{self.ui.port}"
        return f"https://{host}{port}/ui/"

    @property
    def insecure_transport(self) -> bool:
        return self.tls.mode == "plain"

    def summary(self) -> dict:
        """Non-secret configuration facts for the status endpoint and the start-up banner."""
        return {"openadr_url": self.openadr_url, "ui_url": self.ui_url, "tls_mode": self.tls.mode,
                "client_certificates": "required" if self.tls.mode == "mutual" else "not requested",
                "fingerprint_policy": self.tls.fingerprint_policy if self.tls.mode == "mutual" else None,
                "signature_mode": self.signatures.mode, "ui_client_cert": self.ui.client_cert,
                "ui_password_auth": self.ui.password_auth, "schema_mode": self.schema_mode}


# ---------------------------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------------------------

def _apply_env(data: dict) -> dict:
    """Override top-level scalar fields from AHRI_VTN_* environment variables."""
    for name, field in Config.model_fields.items():
        env_name = ENV_PREFIX + name.upper()
        if env_name in os.environ and name not in ("thresholds", "tls", "ui", "signatures", "limits"):
            raw = os.environ[env_name]
            annotation = field.annotation
            if annotation is bool:
                data[name] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif annotation is int:
                data[name] = int(raw)
            else:
                data[name] = raw
    return data


def _reject_old_schema(data: dict):
    old = [f"[tls] {k}" for k in (data.get("tls") or {}) if k in _OLD_TLS_KEYS]
    old += [f"[ui] {k}" for k in (data.get("ui") or {}) if k in _OLD_UI_KEYS]
    if old:
        raise ConfigError("The configuration uses keys from the pre-release schema: " + ", ".join(old)
                          + ". See config.example.toml for the current [tls], [signatures] and [ui] tables.")


def load_config(path: Optional[str | os.PathLike] = None) -> Config:
    """
    Load the configuration from a TOML file (if given / if it exists) and apply environment
    variable overrides. Raises ConfigError for insecure or incomplete configurations.
    """
    data: dict = {}
    if path is not None:
        p = Path(path)
        if p.exists():
            with p.open("rb") as fh:
                data = _toml.load(fh)
        else:
            raise FileNotFoundError(f"Config file {p} does not exist")
    _reject_old_schema(data)
    data = _apply_env(data)
    try:
        return Config(**data)
    except ValidationError as err:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in err.errors())
        raise ConfigError(f"Invalid configuration: {problems}") from None
