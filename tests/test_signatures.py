"""
XML signature modes (OpenADR 2.0b "High" security) on top of mutual TLS.
"""
import asyncio

import pytest

from .conftest import make_config, start_sim, start_vtn, stop_vtn, wait_until


def ven_named(vtn, name):
    return next((v for v in vtn.queries.list_vens() if v["ven_name"] == name), None)


def codes(vtn):
    return {f["code"] for f in vtn.queries.findings()}


def responses_with_code(vtn, code):
    out = []
    for item in vtn.queries.list_messages(direction="out")["items"]:
        full = vtn.queries.get_message(item["id"])
        if ((full["payload"] or {}).get("response") or {}).get("response_code") == code:
            out.append(full)
    return out


@pytest.mark.asyncio
async def test_none_mode_records_that_a_ven_signs(pki, tmp_path):
    vtn = await start_vtn(make_config(pki, tmp_path, signatures="none"))
    try:
        sim = await start_sim(vtn, ven_name="signer", sign_messages=True)
        try:
            assert await wait_until(lambda: (ven_named(vtn, "signer") or {}).get("registered"), 10)
            assert await wait_until(lambda: "sig-present" in codes(vtn), 5)
            assert any(m["signed"] for m in vtn.queries.list_messages(direction="in")["items"])
        finally:
            await sim.stop()
    finally:
        await stop_vtn(vtn)


@pytest.mark.asyncio
async def test_optional_mode_accepts_both(pki, tmp_path):
    vtn = await start_vtn(make_config(pki, tmp_path, signatures="optional"))
    try:
        plain = await start_sim(vtn, ven_name="unsigned")
        signer = await start_sim(vtn, ven_name="signer", sign_messages=True)
        try:
            assert await wait_until(lambda: (ven_named(vtn, "unsigned") or {}).get("registered"), 10)
            assert await wait_until(lambda: (ven_named(vtn, "signer") or {}).get("registered"), 10)
            await asyncio.sleep(2)
            assert "sig-absent" in codes(vtn)
            assert responses_with_code(vtn, 463) == []
            # The VTN itself signs its responses when a signing certificate is configured
            assert any(m["signed"] for m in vtn.queries.list_messages(direction="out")["items"])
        finally:
            await plain.stop()
            await signer.stop()
    finally:
        await stop_vtn(vtn)


@pytest.mark.asyncio
async def test_required_mode_answers_unsigned_messages_with_463_not_500(pki, tmp_path):
    vtn = await start_vtn(make_config(pki, tmp_path, signatures="required"))
    try:
        unsigned = await start_sim(vtn, ven_name="unsigned")
        try:
            # Registration (no venID yet) succeeds; everything after it is unsigned and refused with 463
            assert await wait_until(lambda: (ven_named(vtn, "unsigned") or {}).get("registered"), 10)
            assert await wait_until(lambda: responses_with_code(vtn, 463), 10)
            statuses = {m["http_status"] for m in vtn.queries.list_messages(direction="out")["items"]}
            assert 500 not in statuses
        finally:
            await unsigned.stop()
        signer = await start_sim(vtn, ven_name="signer", sign_messages=True)
        try:
            assert await wait_until(lambda: (ven_named(vtn, "signer") or {}).get("status", {}).get("Operational State")
                                    is not None, 15)
        finally:
            await signer.stop()
    finally:
        await stop_vtn(vtn)
