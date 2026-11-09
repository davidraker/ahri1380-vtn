"""
Mutual TLS on the OpenADR listener: handshake rejection, fingerprint policies (pinned, first-use,
ca-only), identity takeover attempts, operator pinning and re-keying, transport evidence and the
OpenADR 2.0b default cipher suites.
"""
import asyncio
import ssl

import aiohttp
import pytest
from openleadr.messaging import create_message

from .conftest import current_pki, make_config, start_sim, start_vtn, stop_vtn, ui_session, ui_url, wait_until

TLS_FAILURE = (aiohttp.ClientConnectorError, aiohttp.ServerDisconnectedError, aiohttp.ClientOSError,
               aiohttp.ClientConnectionError)


async def post_openadr(vtn, ctx: ssl.SSLContext, service: str, message: str):
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx),
                                     headers={"content-type": "application/xml"}) as session:
        async with session.post(f"{vtn.openadr_url}/{service}", data=message) as resp:
            return resp.status, await resp.read()


def ven_named(vtn, name):
    return next((v for v in vtn.queries.list_vens() if v["ven_name"] == name), None)


def codes(vtn, ven_pk=None):
    return {f["code"] for f in vtn.queries.findings(ven_pk=ven_pk)}


async def registered(vtn, name, timeout=10):
    return await wait_until(lambda: (ven_named(vtn, name) or {}).get("registered"), timeout)


# ---------------------------------------------------------------------------- handshake

@pytest.mark.asyncio
async def test_handshake_rejected_without_or_with_untrusted_certificate(vtn):
    pki = current_pki()
    msg = create_message("oadrPoll", ven_id="x")
    with pytest.raises(TLS_FAILURE):
        await post_openadr(vtn, pki.client_context(), "OadrPoll", msg)
    rogue = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    rogue.load_verify_locations(pki.path("lab-ca.crt"))
    rogue.check_hostname = False
    rogue.load_cert_chain(pki.rogue.dir / "ven-rogue-ven.crt", pki.rogue.dir / "ven-rogue-ven.key")
    with pytest.raises(TLS_FAILURE):
        await post_openadr(vtn, rogue, "OadrPoll", msg)
    assert vtn.queries.list_vens() == []       # nothing reached the application layer


@pytest.mark.asyncio
async def test_default_configuration_is_mutual_tls_on_443():
    from ahri_vtn.config import Config, ConfigError
    with pytest.raises(ConfigError):
        Config()                                      # no certificates: refuse to start
    pki = current_pki()
    cfg = Config(tls={"cert": pki.path("vtn-rsa.crt"), "key": pki.path("vtn-rsa.key"),
                      "client_ca_file": pki.path("ven-trust.pem")},
                 ui={"operator_ca_file": pki.path("operator-ca.crt"), "basic_auth_user": "lab",
                     "basic_auth_password_hash": "pbkdf2_sha256$1$YQ==$YQ=="})
    assert cfg.tls.mode == "mutual" and cfg.port == 443 and cfg.ui.port == 9443
    assert cfg.openadr_url == "https://localhost/OpenADR2/Simple/2.0b"
    assert cfg.ui_url == "https://127.0.0.1:9443/ui/"


# ---------------------------------------------------------------------------- first-use policy

@pytest.mark.asyncio
async def test_first_use_pins_then_refuses_a_different_certificate(vtn, sim):
    pki = current_pki()
    ven = ven_named(vtn, "sim-hvac")
    assert ven["fingerprint"] == pki.fingerprint("sim-hvac")
    assert ven["fingerprint_source"] == "first-use"
    assert "CN=sim-hvac" in ven["cert_subject"] and ven["cert_key_type"] == "RSA" and ven["cert_key_bits"] == 2048
    assert "cert-fingerprint-pinned-on-first-use" in codes(vtn, ven["id"])

    # Another device with a trusted certificate claims the same venName: refused with 463, nothing changes
    imposter = await start_sim(vtn, ven_name="sim-hvac", cert_name="sim-hvac-2")
    try:
        await asyncio.sleep(2)
        assert imposter.client.registration_id is None
        assert ven_named(vtn, "sim-hvac")["fingerprint"] == pki.fingerprint("sim-hvac")
        rejected = vtn.registry.list_rejected()
        assert rejected and rejected[0]["reason"] == "fingerprint-mismatch"
        assert rejected[0]["fingerprint"] == pki.fingerprint("sim-hvac-2")
        assert "cert-fingerprint-mismatch" in codes(vtn, ven["id"])
        # The refusal is an application-layer 463 (OpenLEADR answers a ProtocolError with oadrResponse)
        answers = [vtn.queries.get_message(m["id"]) for m in vtn.queries.list_messages(direction="out")["items"]]
        refusals = [m for m in answers if ((m["payload"] or {}).get("response") or {}).get("response_code") == 463]
        assert refusals and all(m["http_status"] == 200 for m in refusals)      # a valid OpenADR 463, not an HTTP error
    finally:
        await imposter.stop()


@pytest.mark.asyncio
async def test_operator_repins_after_rekeying(vtn, sim):
    """The device got a new certificate: the operator pins the new fingerprint, then it registers."""
    pki = current_pki()
    ven = ven_named(vtn, "sim-hvac")
    await sim.stop()
    await vtn.aio.registry.pin_fingerprint(ven["id"], pki.fingerprint("sim-hvac-2"))
    assert ven_named(vtn, "sim-hvac")["fingerprint_source"] == "operator"
    rekeyed = await start_sim(vtn, ven_name="sim-hvac", cert_name="sim-hvac-2")
    try:
        assert await wait_until(lambda: rekeyed.client.registration_id is not None, 10)
        assert ven_named(vtn, "sim-hvac")["fingerprint"] == pki.fingerprint("sim-hvac-2")
        assert len(vtn.queries.list_vens()) == 1            # same record, no duplicate
    finally:
        await rekeyed.stop()


@pytest.mark.asyncio
async def test_clear_certificate_then_first_use_again(vtn, sim):
    ven = ven_named(vtn, "sim-hvac")
    await sim.stop()
    await vtn.aio.registry.clear_fingerprint(ven["id"])
    assert ven_named(vtn, "sim-hvac")["fingerprint"] is None
    again = await start_sim(vtn, ven_name="sim-hvac", cert_name="sim-hvac-2")
    try:
        assert await wait_until(lambda: again.client.registration_id is not None, 10)
        assert ven_named(vtn, "sim-hvac")["fingerprint"] == current_pki().fingerprint("sim-hvac-2")
    finally:
        await again.stop()


@pytest.mark.asyncio
async def test_known_certificate_with_new_name_is_the_same_device(vtn, sim):
    """A pinned certificate identifies the VEN whatever venName it sends (10.5.1: one-to-one)."""
    await sim.stop()
    renamed = await start_sim(vtn, ven_name="renamed-unit", cert_name="sim-hvac")
    try:
        assert await wait_until(lambda: renamed.client.registration_id is not None, 10)
        names = [v["ven_name"] for v in vtn.queries.list_vens()]
        assert names == ["sim-hvac"]
        assert "reg-ven-name-changed" in codes(vtn)
    finally:
        await renamed.stop()


# ---------------------------------------------------------------------------- pinned policy

@pytest.mark.asyncio
async def test_pinned_policy_requires_a_pinned_fingerprint(pki, tmp_path):
    vtn = await start_vtn(make_config(pki, tmp_path, fingerprint_policy="pinned"))
    try:
        unit = await start_sim(vtn, ven_name="unit-a")
        await asyncio.sleep(2)
        assert unit.client.registration_id is None
        await unit.stop()
        assert vtn.queries.list_vens() == []
        rejected = vtn.registry.list_rejected()
        assert rejected[0]["reason"] == "unknown-fingerprint" and rejected[0]["ven_name"] == "unit-a"
        fp = rejected[0]["fingerprint"]
        assert fp == pki.fingerprint("unit-a")

        # Operator pins before first contact (the Add VEN form path) and the unit registers
        ven = await vtn.aio.registry.add_ven("unit-a", fingerprint=fp)
        assert ven["fingerprint_source"] == "operator"
        unit = await start_sim(vtn, ven_name="unit-a")
        try:
            assert await wait_until(lambda: unit.client.registration_id is not None, 10)
            assert "reg-table4" in codes(vtn, ven["id"])
        finally:
            await unit.stop()

        # A pre-registered VEN without a fingerprint is still refused
        await vtn.aio.registry.add_ven("unit-b")
        other = await start_sim(vtn, ven_name="unit-b")
        await asyncio.sleep(2)
        assert other.client.registration_id is None
        await other.stop()
        assert any(r["reason"] == "unknown-fingerprint" and r["ven_name"] == "unit-b" for r in vtn.registry.list_rejected())
    finally:
        await stop_vtn(vtn)


@pytest.mark.asyncio
async def test_fingerprint_cannot_be_pinned_to_two_vens(vtn, sim):
    pki = current_pki()
    other = await vtn.aio.registry.add_ven("other")
    with pytest.raises(ValueError):
        await vtn.aio.registry.pin_fingerprint(other["id"], pki.fingerprint("sim-hvac"))
    with pytest.raises(ValueError):
        await vtn.aio.registry.add_ven("third", fingerprint=pki.fingerprint("sim-hvac"))
    # Full SHA-256 form and lower case are accepted and normalised
    from ahri_vtn.tls import inspect_pem_file
    info = inspect_pem_file(pki.path("ven-sim-hvac-2.crt"))
    ven = await vtn.aio.registry.pin_fingerprint(other["id"], info.sha256.lower().replace(":", ""))
    assert ven["fingerprint"] == info.fingerprint


# ---------------------------------------------------------------------------- ca-only policy

@pytest.mark.asyncio
async def test_ca_only_policy_accepts_any_trusted_certificate(pki, tmp_path):
    vtn = await start_vtn(make_config(pki, tmp_path, fingerprint_policy="ca-only"))
    try:
        first = await start_sim(vtn, ven_name="shared-name", cert_name="sim-hvac")
        assert await registered(vtn, "shared-name")
        await first.stop()
        second = await start_sim(vtn, ven_name="shared-name", cert_name="sim-hvac-2")
        try:
            assert await wait_until(lambda: second.client.registration_id is not None, 10)
            ven = ven_named(vtn, "shared-name")
            assert ven["fingerprint"] == pki.fingerprint("sim-hvac-2") and ven["fingerprint_source"] == "ca-only"
            assert "cert-fingerprint-changed" in codes(vtn, ven["id"])
            assert vtn.registry.list_rejected() == []
        finally:
            await second.stop()
    finally:
        await stop_vtn(vtn)


# ---------------------------------------------------------------------------- evidence and suites

@pytest.mark.asyncio
async def test_transport_evidence_is_recorded(vtn, sim):
    pki = current_pki()
    page = vtn.queries.list_messages(direction="in")
    assert page["total"] > 0
    for item in page["items"]:
        assert item["tls_version"] in ("TLSv1.2", "TLSv1.3")
        assert item["tls_cipher"]
        assert item["peer_fingerprint"] == pki.fingerprint("sim-hvac")
        assert "CN=sim-hvac" in item["peer_subject"]
        assert item["signed"] is False
    ven = ven_named(vtn, "sim-hvac")
    assert ven["last_tls_version"] and ven["last_tls_cipher"] and ven["last_remote"]
    # Python's default client negotiates TLS 1.3, which is not one of the two 2.0b defaults: informational finding
    found = codes(vtn, ven["id"])
    assert ("tls-cipher-not-default" in found) == (ven["last_tls_cipher"] not in ("AES128-SHA256", "ECDHE-ECDSA-AES128-SHA256"))
    async with ui_session(vtn) as http:
        async with http.get(ui_url(vtn, "/api/tls")) as r:
            info = await r.json()
    assert info["mode"] == "mutual" and {c["key_type"] for c in info["certificates"]} == {"RSA", "EC"}
    assert info["missing_openadr_suites"] == []
    assert "AES128-SHA256" in info["cipher_suites"] and "ECDHE-ECDSA-AES128-SHA256" in info["cipher_suites"]


def restricted_context(pki, cert_name: str, cipher: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(pki.path("lab-ca.crt"))
    ctx.load_cert_chain(pki.path(f"ven-{cert_name}.crt"), pki.path(f"ven-{cert_name}.key"))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers(cipher)
    return ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("cert_name,cipher", [("sim-hvac", "AES128-SHA256"), ("ecc-ven", "ECDHE-ECDSA-AES128-SHA256")])
async def test_ven_offering_only_a_2_0b_default_suite_connects(vtn, cert_name, cipher):
    """Rule 67: a VEN that offers only one mandatory suite must be served (RSA and ECDSA alike)."""
    pki = current_pki()
    sim = await start_sim(vtn, ven_name=f"legacy-{cert_name}", ssl_context=restricted_context(pki, cert_name, cipher))
    try:
        assert await wait_until(lambda: sim.client.registration_id is not None, 10)
        ven = ven_named(vtn, f"legacy-{cert_name}")
        assert ven["last_tls_version"] == "TLSv1.2" and ven["last_tls_cipher"] == cipher
        assert "tls-cipher-not-default" not in codes(vtn, ven["id"])
    finally:
        await sim.stop()


# ---------------------------------------------------------------------------- server and plain modes

@pytest.mark.asyncio
async def test_server_mode_accepts_vens_without_certificates_but_records_it(pki, tmp_path):
    vtn = await start_vtn(make_config(pki, tmp_path, mode="server"))
    try:
        sim = await start_sim(vtn, ven_name="proto")       # ca_file only, no client certificate
        try:
            assert await registered(vtn, "proto")
            ven = ven_named(vtn, "proto")
            assert ven["fingerprint"] is None
            assert await wait_until(lambda: "tls-no-client-cert" in codes(vtn, ven["id"]), 5)
            # polls and reports are served (the venID is not bound to a certificate in this mode)
            assert await wait_until(lambda: ven_named(vtn, "proto")["status"]["Operational State"] is not None, 15)
            msg = vtn.queries.list_messages(direction="in")["items"][0]
            assert msg["tls_version"] and msg["peer_fingerprint"] is None
        finally:
            await sim.stop()
    finally:
        await stop_vtn(vtn)


@pytest.mark.asyncio
async def test_plain_mode_is_explicit_and_flagged(pki, tmp_path, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="ahri_vtn"):
        vtn = await start_vtn(make_config(pki, tmp_path, plain=True))
    assert any("INSECURE" in rec.message for rec in caplog.records)
    try:
        assert vtn.openadr_url.startswith("http://")
        sim = await start_sim(vtn, ven_name="dev-unit")
        try:
            assert await registered(vtn, "dev-unit")
            ven = ven_named(vtn, "dev-unit")
            assert await wait_until(lambda: "transport-plain-http" in codes(vtn, ven["id"]), 5)
            # The UI (still TLS, still authenticated) shows the badge
            async with ui_session(vtn) as http:
                async with http.get(ui_url(vtn)) as r:
                    assert r.status == 200
                    assert "INSECURE: plain HTTP" in await r.text()
        finally:
            await sim.stop()
    finally:
        await stop_vtn(vtn)


@pytest.mark.asyncio
async def test_claimed_ven_id_from_another_certificate_leaves_no_trace_on_the_victim(vtn, sim):
    """A 463-refused poll must not mark the claimed VEN online or raise findings on it."""
    pki = current_pki()
    victim = ven_named(vtn, "sim-hvac")
    await sim.stop()
    await asyncio.sleep(1.5)
    before = ven_named(vtn, "sim-hvac")
    polls_before, seen_before = before["poll_count"], before["last_seen_at"]
    findings_before = len(vtn.queries.findings(ven_pk=victim["id"]))
    status, body = await post_openadr(vtn, pki.client_context(ven="sim-hvac-2"), "OadrPoll",
                                      create_message("oadrPoll", ven_id=victim["ven_id"]))
    assert status == 200
    from openleadr.messaging import parse_message
    assert parse_message(body)[1]["response"]["response_code"] == 463
    after = ven_named(vtn, "sim-hvac")
    assert after["poll_count"] == polls_before and after["last_seen_at"] == seen_before
    assert len(vtn.queries.findings(ven_pk=victim["id"])) == findings_before
    # the raw message is still in the log, attributed to the claimed venID, with the real certificate
    latest = vtn.queries.list_messages(direction="in", message_type="oadrPoll")["items"][0]
    assert latest["peer_fingerprint"] == pki.fingerprint("sim-hvac-2")
    rejected = vtn.registry.list_rejected()
    assert all(r["remote"] for r in rejected) or not rejected
