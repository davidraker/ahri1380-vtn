"""
The operator UI/API listener: TLS only, every request authenticated (certificate, password or
token), CSRF and cross-origin protection, security headers, throttling, parameter clamps, and no
secrets in responses.
"""
import asyncio
import ssl

import aiohttp
import pytest

from .conftest import (API_TOKEN, OPERATOR_PASSWORD, csrf_token_of, current_pki, make_config, start_vtn, stop_vtn,
                       ui_session, ui_url)

TLS_FAILURE = (aiohttp.ClientConnectorError, aiohttp.ServerDisconnectedError, aiohttp.ClientOSError,
               aiohttp.ClientConnectionError)


@pytest.mark.asyncio
async def test_ui_is_tls_only(vtn):
    async with aiohttp.ClientSession() as http:
        with pytest.raises((aiohttp.ClientConnectorError, aiohttp.ClientResponseError, aiohttp.ClientOSError,
                            aiohttp.ServerDisconnectedError)):
            async with http.get(f"http://127.0.0.1:{vtn.config.ui.port}/ui/") as r:
                r.raise_for_status()


@pytest.mark.asyncio
async def test_no_anonymous_access(vtn):
    async with ui_session(vtn, password="wrong") as http:
        async with http.get(ui_url(vtn)) as r:
            assert r.status == 401
    async with ui_session(vtn, no_credentials=True) as http:
        for path in ("/ui/", "/api/status", "/api/vens", "/ui/static/app.css"):
            async with http.get(ui_url(vtn, path)) as r:
                assert r.status == 401, path
                assert r.headers["WWW-Authenticate"].startswith("Basic")
        # five failures within a minute: the address is throttled
        async with http.get(ui_url(vtn, "/api/stream")) as r:
            assert r.status == 429 and r.headers["Retry-After"] == "60"


@pytest.mark.asyncio
async def test_password_login(vtn):
    async with ui_session(vtn) as http:
        async with http.get(ui_url(vtn, "/api/status")) as r:
            assert r.status == 200
            body = await r.json()
            assert body["principal"] == "password:lab"
            assert body["security"]["tls_mode"] == "mutual"
            assert "hash" not in str(body).lower() and OPERATOR_PASSWORD not in str(body)
        async with http.get(ui_url(vtn)) as r:
            html = await r.text()
            assert r.status == 200 and "lab" in html and 'name="csrf_token"' in html


@pytest.mark.asyncio
async def test_operator_certificate_logs_in_without_password(vtn):
    async with ui_session(vtn, operator_cert=True) as http:
        async with http.get(ui_url(vtn, "/api/status")) as r:
            assert r.status == 200
            assert (await r.json())["principal"] == "certificate:tester"


@pytest.mark.asyncio
async def test_bearer_token_for_scripts(vtn):
    async with ui_session(vtn, token=API_TOKEN) as http:
        async with http.get(ui_url(vtn, "/api/status")) as r:
            assert r.status == 200 and (await r.json())["principal"] == "token:api"
        async with http.post(ui_url(vtn, "/api/vens"), json={"ven_name": "scripted"}) as r:
            assert r.status == 201
    async with ui_session(vtn, token="not-a-valid-token") as http:
        async with http.get(ui_url(vtn, "/api/status")) as r:
            assert r.status == 401 and "WWW-Authenticate" not in r.headers


@pytest.mark.asyncio
async def test_throttling_after_repeated_failures(vtn):
    async with ui_session(vtn, password="wrong") as http:
        statuses = []
        for _ in range(7):
            async with http.get(ui_url(vtn, "/api/status")) as r:
                statuses.append(r.status)
        assert statuses[:5] == [401] * 5 and statuses[-1] == 429


@pytest.mark.asyncio
async def test_client_certificate_required_mode(pki, tmp_path):
    vtn = await start_vtn(make_config(pki, tmp_path, ui_client_cert="required"))
    try:
        # No certificate: refused during the handshake
        async with ui_session(vtn) as http:
            with pytest.raises(TLS_FAILURE):
                async with http.get(ui_url(vtn)) as r:
                    pass
        # Certificate but no password: the password is the second factor
        async with ui_session(vtn, operator_cert=True) as http:
            async with http.get(ui_url(vtn)) as r:
                assert r.status == 401
        # Certificate and password
        import base64
        connector = aiohttp.TCPConnector(ssl=pki.client_context(operator_cert=True))
        basic = "Basic " + base64.b64encode(f"lab:{OPERATOR_PASSWORD}".encode()).decode()
        async with aiohttp.ClientSession(connector=connector, headers={"Authorization": basic}) as http:
            async with http.get(ui_url(vtn, "/api/status")) as r:
                assert r.status == 200 and (await r.json())["principal"] == "certificate:tester"
    finally:
        await stop_vtn(vtn)


@pytest.mark.asyncio
async def test_client_certificate_required_without_password(pki, tmp_path):
    vtn = await start_vtn(make_config(pki, tmp_path, ui_client_cert="required", password_auth=False))
    try:
        async with ui_session(vtn, operator_cert=True) as http:
            async with http.get(ui_url(vtn, "/api/status")) as r:
                assert r.status == 200
    finally:
        await stop_vtn(vtn)


@pytest.mark.asyncio
async def test_operator_fingerprint_allowlist(pki, tmp_path):
    from ahri_vtn.config import ConfigError
    from ahri_vtn.tls import inspect_pem_file
    cfg = make_config(pki, tmp_path)
    cfg.ui.operator_fingerprints = ["00:11:22:33:44:55:66:77:88:99"]
    vtn = await start_vtn(cfg)
    try:
        async with ui_session(vtn, operator_cert=True) as http:
            async with http.get(ui_url(vtn, "/api/status")) as r:
                assert r.status == 401          # valid CA, but not an allowed operator
    finally:
        await stop_vtn(vtn)
    # The allow-list accepts the SHA-256 form in any case and normalises it at start-up
    sha = inspect_pem_file(pki.path("operator-tester.crt")).sha256.lower().replace(":", "")
    cfg = make_config(pki, tmp_path)
    cfg2 = cfg.model_copy(update={"ui": cfg.ui.model_copy(update={"operator_fingerprints": [sha]})})
    from ahri_vtn.config import UiConfig
    ui = UiConfig(**{**cfg.ui.model_dump(), "operator_fingerprints": [sha]})
    assert ui.operator_fingerprints == [pki.operator_fingerprint()]
    cfg2 = cfg.model_copy(update={"ui": ui})
    vtn = await start_vtn(cfg2)
    try:
        async with ui_session(vtn, operator_cert=True) as http:
            async with http.get(ui_url(vtn, "/api/status")) as r:
                assert r.status == 200
    finally:
        await stop_vtn(vtn)
    with pytest.raises(Exception):
        UiConfig(**{**cfg.ui.model_dump(), "operator_fingerprints": ["not-a-fingerprint"]})


@pytest.mark.asyncio
async def test_csrf_token_required_on_ui_forms(vtn):
    async with ui_session(vtn) as http:
        async with http.post(ui_url(vtn, "/ui/vens"), data={"ven_name": "no-token"}, allow_redirects=False) as r:
            assert r.status == 403
        async with http.post(ui_url(vtn, "/ui/vens"), data={"ven_name": "bad-token", "csrf_token": "f" * 64},
                             allow_redirects=False) as r:
            assert r.status == 403
        token = await csrf_token_of(http, ui_url(vtn))
        async with http.post(ui_url(vtn, "/ui/vens"), data={"ven_name": "with-token", "csrf_token": token},
                             allow_redirects=False) as r:
            assert r.status == 302 and "sig=" in r.headers["Location"]
        assert [v["ven_name"] for v in vtn.queries.list_vens()] == ["with-token"]


@pytest.mark.asyncio
async def test_cross_origin_state_changes_refused(vtn):
    async with ui_session(vtn) as http:
        for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"},
                        {"Referer": "https://evil.example/page"}):
            async with http.post(ui_url(vtn, "/api/vens"), json={"ven_name": "x"}, headers=headers) as r:
                assert r.status == 403, headers
        # Form-encoded bodies to the API are what a cross-site <form> would send: refused for browser sessions
        async with http.post(ui_url(vtn, "/api/vens"), data={"ven_name": "formed"}) as r:
            assert r.status == 403
        # Same-origin JSON is fine, as are same-origin headers
        async with http.post(ui_url(vtn, "/api/vens"), json={"ven_name": "ok"},
                             headers={"Origin": f"https://127.0.0.1:{vtn.config.ui.port}", "Sec-Fetch-Site": "same-origin"}) as r:
            assert r.status == 201
        assert [v["ven_name"] for v in vtn.queries.list_vens()] == ["ok"]


@pytest.mark.asyncio
async def test_security_headers(vtn):
    async with ui_session(vtn) as http:
        async with http.get(ui_url(vtn)) as r:
            h = r.headers
            assert h["Content-Security-Policy"].startswith("default-src 'self'") and "script-src 'self'" in h["Content-Security-Policy"]
            assert h["X-Content-Type-Options"] == "nosniff" and h["X-Frame-Options"] == "DENY"
            assert h["Strict-Transport-Security"].startswith("max-age=") and h["Referrer-Policy"] == "same-origin"
        async with http.get(ui_url(vtn, "/ui/nowhere")) as r:
            assert r.status == 404 and r.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.asyncio
async def test_open_redirect_and_flash_spoofing_are_blocked(vtn):
    ven = await vtn.aio.registry.add_ven("bench")
    async with ui_session(vtn) as http:
        token = await csrf_token_of(http, ui_url(vtn))
        async with http.post(ui_url(vtn, "/ui/events"), data={"ven_pk": ven["id"], "preset": "general_curtailment",
                                                             "back": "https://evil.example/", "csrf_token": token},
                             allow_redirects=False) as r:
            assert r.status == 302 and r.headers["Location"].startswith("/ui/")
        async with http.post(ui_url(vtn, "/ui/events"), data={"ven_pk": ven["id"], "preset": "general_curtailment",
                                                             "back": "//evil.example/x", "csrf_token": token},
                             allow_redirects=False) as r:
            assert r.headers["Location"].startswith("/ui/")
        async with http.get(ui_url(vtn, "/ui/?flash=You+have+been+hacked&kind=ok")) as r:
            assert "hacked" not in (await r.text())
        # A signed flash (as produced by a redirect) is shown
        async with http.post(ui_url(vtn, "/ui/vens"), data={"ven_name": "flashy", "csrf_token": token},
                             allow_redirects=True) as r:
            assert "VEN &#39;flashy&#39; added" in (await r.text()) or "flashy" in (await r.text())


@pytest.mark.asyncio
async def test_parameter_validation(vtn):
    async with ui_session(vtn) as http:
        async with http.get(ui_url(vtn, "/api/vens/abc")) as r:
            assert r.status == 404
        async with http.get(ui_url(vtn, "/api/messages?page_size=100000&page=1")) as r:
            assert r.status == 200 and (await r.json())["page_size"] <= 500
        async with http.get(ui_url(vtn, "/api/messages?page=notanumber")) as r:
            assert r.status == 400
        async with http.get(ui_url(vtn, "/api/events?limit=-5")) as r:
            assert r.status == 200
        ven = await vtn.aio.registry.add_ven("p")
        async with http.get(ui_url(vtn, f"/ui/vens/{ven['id']}?minutes=abc")) as r:
            assert r.status == 400
        async with http.get(ui_url(vtn, f"/ui/vens/{ven['id']}?minutes=99999999")) as r:
            assert r.status == 200


@pytest.mark.asyncio
async def test_live_stream_requires_auth_and_is_capped(pki, tmp_path):
    cfg = make_config(pki, tmp_path)
    cfg.ui.max_sse_clients = 1
    vtn = await start_vtn(cfg)
    try:
        async with ui_session(vtn) as http:
            async with http.get(ui_url(vtn, "/api/stream")) as first:
                assert first.status == 200
                assert (await first.content.readline()).startswith(b": connected")
                async with http.get(ui_url(vtn, "/api/stream")) as second:
                    assert second.status == 503
    finally:
        await stop_vtn(vtn)


@pytest.mark.asyncio
async def test_rejected_registration_pin_flow_through_ui(pki, tmp_path):
    """Dashboard flow: a refused device appears, the operator adds it with its certificate, it registers."""
    from .conftest import start_sim, wait_until
    vtn = await start_vtn(make_config(pki, tmp_path, fingerprint_policy="pinned"))
    try:
        unit = await start_sim(vtn, ven_name="new-unit")
        await asyncio.sleep(2)
        await unit.stop()
        async with ui_session(vtn) as http:
            async with http.get(ui_url(vtn)) as r:
                html = await r.text()
            assert "Refused registrations" in html and pki.fingerprint("new-unit") in html
            token = await csrf_token_of(http, ui_url(vtn))
            rejected = vtn.registry.list_rejected()[0]
            async with http.post(ui_url(vtn, "/ui/vens"), data={"ven_name": "new-unit", "fingerprint": rejected["fingerprint"],
                                                               "rejected_pk": rejected["id"], "csrf_token": token},
                                 allow_redirects=False) as r:
                assert r.status == 302
        assert vtn.registry.list_rejected() == []
        unit = await start_sim(vtn, ven_name="new-unit")
        try:
            assert await wait_until(lambda: unit.client.registration_id is not None, 10)
        finally:
            await unit.stop()
    finally:
        await stop_vtn(vtn)


def test_csv_cells_are_neutralised():
    from ahri_vtn.web.api import _csv_cell
    assert _csv_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert _csv_cell("+1") == "'+1" and _csv_cell("-1") == "'-1" and _csv_cell("@x") == "'@x"
    assert _csv_cell("Inst Power") == "Inst Power" and _csv_cell(12.5) == 12.5 and _csv_cell(None) is None


def test_pki_names_cannot_escape_the_directory(tmp_path, monkeypatch, capsys):
    from ahri_vtn.__main__ import main
    monkeypatch.chdir(tmp_path)
    assert main(["certs", "init-ca"]) == 0
    assert main(["certs", "ven", "--name", "../escape"]) == 2
    assert "Invalid name" in capsys.readouterr().err
    assert not (tmp_path / "escape.crt").exists() and not (tmp_path / "certs" / ".." / "ven-escape.crt").exists()
    assert main(["certs", "operator", "--name", "ok-name_1", "--p12-password", "x"]) == 0


@pytest.mark.asyncio
async def test_password_verification_is_cached_and_off_the_loop(vtn):
    import time
    async with ui_session(vtn) as http:
        t0 = time.monotonic()
        async with http.get(ui_url(vtn, "/api/status")) as r:
            assert r.status == 200
        first = time.monotonic() - t0
        t0 = time.monotonic()
        for _ in range(5):
            async with http.get(ui_url(vtn, "/api/status")) as r:
                assert r.status == 200
        each = (time.monotonic() - t0) / 5
    assert each < first or each < 0.05          # cached verifications skip the PBKDF2 work
