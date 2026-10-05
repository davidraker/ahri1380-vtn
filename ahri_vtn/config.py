"""
Configuration for the AHRI 1380 VTN test tool (TOML file + environment overrides).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

try:                        # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # Python 3.10
    import tomli as _toml   # type: ignore

ENV_PREFIX = "AHRI_VTN_"


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
    http_cert: Optional[str] = None
    http_key: Optional[str] = None
    http_ca_file: Optional[str] = None
    http_key_passphrase: Optional[str] = None
    signing_cert: Optional[str] = None
    signing_key: Optional[str] = None
    signing_passphrase: Optional[str] = None


class UiConfig(BaseModel):
    basic_auth_user: Optional[str] = None
    basic_auth_password: Optional[str] = None


class Config(BaseModel):
    vtn_id: str = "ahri1380-vtn"
    host: str = "0.0.0.0"
    port: int = 8080
    http_path_prefix: str = "/OpenADR2/Simple/2.0b"
    db_url: str = "sqlite:///ahri_vtn.sqlite"
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
    ui: UiConfig = Field(default_factory=UiConfig)

    @property
    def openadr_url(self) -> str:
        scheme = "https" if self.tls.http_cert else "http"
        host = "localhost" if self.host in ("0.0.0.0", "") else self.host
        return f"{scheme}://{host}:{self.port}{self.http_path_prefix}"


def _apply_env(data: dict) -> dict:
    """Override top-level scalar fields from AHRI_VTN_* environment variables."""
    for name, field in Config.model_fields.items():
        env_name = ENV_PREFIX + name.upper()
        if env_name in os.environ and name not in ("thresholds", "tls", "ui"):
            raw = os.environ[env_name]
            annotation = field.annotation
            if annotation is bool:
                data[name] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif annotation is int:
                data[name] = int(raw)
            else:
                data[name] = raw
    return data


def load_config(path: Optional[str | os.PathLike] = None) -> Config:
    """
    Load the configuration from a TOML file (if given / if it exists) and apply
    environment variable overrides.
    """
    data: dict = {}
    if path is not None:
        p = Path(path)
        if p.exists():
            with p.open("rb") as fh:
                data = _toml.load(fh)
        else:
            raise FileNotFoundError(f"Config file {p} does not exist")
    data = _apply_env(data)
    return Config(**data)
