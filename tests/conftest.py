"""
Test fixtures. The default configuration is the secure one: mutual TLS on the OpenADR listener
with a lab CA minted per test session, and the operator UI on its own TLS listener with
certificate-or-password login. Plain HTTP is a deliberate opt-in (`plain=True`).
"""
import asyncio
import logging
import os
import socket
import ssl
from datetime import timedelta
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio

from ahri_vtn.certs import LabPki
from ahri_vtn.config import Config
from ahri_vtn.server import AhriVtn
from ahri_vtn.tls import inspect_pem_file
from ahri_vtn.web.auth import hash_password
from sim_ven.ahri_sim_ven import AhriSimVen

logging.getLogger("openleadr").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

OPERATOR_PASSWORD = "lab-secret"
API_TOKEN = "test-api-token-0123456789"
# Run the whole suite against PostgreSQL by pointing this at a server, e.g.
#   AHRI_VTN_TEST_DB_URL=postgresql://postgres:ahri@127.0.0.1:55432/ahri_vtn pytest tests
TEST_DB_URL = os.environ.get("AHRI_VTN_TEST_DB_URL")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Pki:
    """Certificates for one test session (lab CA, VTN RSA + ECC, VENs, operator, a rogue CA)."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.lab = LabPki(directory, out=open(os.devnull, "w"))
        self.lab.init_ca()
        self.lab.vtn(["localhost", "127.0.0.1"])
        self.lab.ven("sim-hvac")
        self.lab.ven("sim-hvac-2")
        self.lab.ven("ecc-ven", ecc=True)
        self.lab.operator("tester", p12_password="x")
        # A second, untrusted CA with a VEN certificate
        rogue_dir = directory / "rogue"
        self.rogue = LabPki(rogue_dir, out=open(os.devnull, "w"))
        self.rogue.init_ca(name="Rogue CA")
        self.rogue.ven("rogue-ven")

    def path(self, name: str) -> str:
        return str(self.dir / name)

    def ven_files(self, name: str = "sim-hvac") -> dict:
        """Certificate files for a VEN name, minted on first use so every simulated VEN has its own identity."""
        if not (self.dir / f"ven-{name}.crt").exists():
            self.lab.ven(name)
        return {"cert": self.path(f"ven-{name}.crt"), "key": self.path(f"ven-{name}.key"), "ca_file": self.path("lab-ca.crt")}

    def fingerprint(self, name: str = "sim-hvac") -> str:
        return inspect_pem_file(self.path(f"ven-{name}.crt")).fingerprint

    def operator_fingerprint(self) -> str:
        return inspect_pem_file(self.path("operator-tester.crt")).fingerprint

    def client_context(self, operator_cert: bool = False, ven: str = None) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(self.path("lab-ca.crt"))
        ctx.check_hostname = False
        if operator_cert:
            ctx.load_cert_chain(self.path("operator-tester.crt"), self.path("operator-tester.key"))
        if ven:
            ctx.load_cert_chain(self.path(f"ven-{ven}.crt"), self.path(f"ven-{ven}.key"))
        return ctx


_PKI: Pki = None


def current_pki() -> Pki:
    return _PKI


@pytest.fixture(scope="session")
def pki(tmp_path_factory) -> Pki:
    global _PKI
    _PKI = Pki(tmp_path_factory.mktemp("pki"))
    return _PKI


def make_config(pki: Pki, tmp_path: Path, *, plain: bool = False, fingerprint_policy: str = "first-use",
                mode: str = None, ui_client_cert: str = "optional", password_auth: bool = True,
                signatures: str = "none", **overrides) -> Config:
    dbfile = tmp_path / f"ahri_vtn_test_{free_port()}.sqlite"
    db_url = TEST_DB_URL or f"sqlite:///{dbfile}"
    tls = {"mode": mode or ("plain" if plain else "mutual"), "cert": pki.path("vtn-rsa.crt"), "key": pki.path("vtn-rsa.key"),
           "ecc_cert": pki.path("vtn-ecc.crt"), "ecc_key": pki.path("vtn-ecc.key"),
           "client_ca_file": pki.path("ven-trust.pem"), "fingerprint_policy": fingerprint_policy}
    ui = {"host": "127.0.0.1", "port": free_port(), "client_cert": ui_client_cert, "password_auth": password_auth,
          "operator_ca_file": pki.path("operator-ca.crt"), "basic_auth_user": "lab",
          "basic_auth_password_hash": hash_password(OPERATOR_PASSWORD, iterations=1000),
          "api_tokens_env": "AHRI_VTN_TEST_TOKENS"}
    if not password_auth:
        ui.pop("basic_auth_user"), ui.pop("basic_auth_password_hash")
    os.environ["AHRI_VTN_TEST_TOKENS"] = API_TOKEN
    sig = {"mode": signatures}
    if signatures != "none":
        sig.update(cert=pki.path("vtn-rsa.crt"), key=pki.path("vtn-rsa.key"))
    defaults = dict(port=free_port(), host="127.0.0.1", db_url=db_url, requested_poll_freq_seconds=1,
                    status_report_interval_seconds=1, default_event_duration_minutes=1, tls=tls, ui=ui, signatures=sig)
    defaults.update(overrides)
    return Config(**defaults)


def reset_database(config: Config):
    """A shared PostgreSQL test database starts every test empty (SQLite files are new anyway)."""
    if not config.db_url.startswith("sqlite"):
        from ahri_vtn.db import Database
        db = Database(config.db_url, password=config.db_password())
        db.drop_all()
        db.dispose()


async def start_vtn(config: Config) -> AhriVtn:
    reset_database(config)
    vtn = AhriVtn(config)
    await vtn.start()
    return vtn


async def stop_vtn(vtn: AhriVtn):
    await vtn.stop()
    if vtn.config.db_url.startswith("sqlite"):
        dbfile = vtn.config.db_url.replace("sqlite:///", "")
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(dbfile + suffix)
            except FileNotFoundError:
                pass


@pytest_asyncio.fixture
async def vtn(pki, tmp_path):
    """Mutual TLS, fingerprint policy first-use (so the simulator can register without pre-pinning)."""
    vtn = await start_vtn(make_config(pki, tmp_path))
    try:
        yield vtn
    finally:
        await stop_vtn(vtn)


async def start_sim(vtn, ven_name: str = "sim-hvac", cert_name: str = None, **kwargs) -> AhriSimVen:
    """Start a simulated VEN against `vtn` with the matching lab certificate (unless plain HTTP)."""
    params = dict(status_interval=timedelta(seconds=1), static_interval=timedelta(seconds=5), ramp_seconds=1.0)
    if vtn.config.tls.mode == "mutual" and "cert" not in kwargs and "ssl_context" not in kwargs:
        params.update(current_pki().ven_files(cert_name or ven_name))
    elif vtn.config.tls.mode == "server" and "ca_file" not in kwargs and "ssl_context" not in kwargs:
        params["ca_file"] = current_pki().path("lab-ca.crt")
    params.update(kwargs)
    sim = AhriSimVen(ven_name, vtn.openadr_url, **params)
    await sim.start()
    return sim


@pytest_asyncio.fixture
async def sim(vtn):
    sim = await start_sim(vtn)
    # wait until the VEN has registered its reports and the first status sample arrived
    await wait_until(lambda: any(v["status"]["Operational State"] is not None for v in vtn.queries.list_vens()), 15)
    try:
        yield sim
    finally:
        await sim.stop()


async def wait_until(predicate, timeout: float, interval: float = 0.2):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return True
        await asyncio.sleep(interval)
    return False


def ven_pk_of(vtn, ven_name: str = "sim-hvac") -> int:
    return next(v["id"] for v in vtn.queries.list_vens() if v["ven_name"] == ven_name)


# --------------------------------------------------------------------------- UI / API clients

def ui_session(vtn, *, operator_cert: bool = False, password: str = OPERATOR_PASSWORD, token: str = None,
               no_credentials: bool = False) -> aiohttp.ClientSession:
    """An aiohttp session for the UI listener with the chosen credentials."""
    import base64
    connector = aiohttp.TCPConnector(ssl=current_pki().client_context(operator_cert=operator_cert))
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif not no_credentials and not operator_cert and password is not None:
        headers["Authorization"] = "Basic " + base64.b64encode(f"lab:{password}".encode()).decode()
    return aiohttp.ClientSession(connector=connector, headers=headers)


def ui_url(vtn, path: str = "/ui/") -> str:
    return f"https://127.0.0.1:{vtn.config.ui.port}{path}"


async def csrf_token_of(session: aiohttp.ClientSession, page_url: str) -> str:
    """Fetch a UI page and return the CSRF token embedded in its forms."""
    import re
    async with session.get(page_url) as r:
        html = await r.text()
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', html)
    assert match, "no CSRF token in page"
    return match.group(1)

