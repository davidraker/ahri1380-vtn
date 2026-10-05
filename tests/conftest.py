import asyncio
import logging
import os
import socket
import tempfile
from datetime import timedelta

import pytest_asyncio

from ahri_vtn.config import Config
from ahri_vtn.server import AhriVtn
from sim_ven.ahri_sim_ven import AhriSimVen

logging.getLogger("openleadr").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_config(**overrides) -> Config:
    dbfile = tempfile.mktemp(suffix=".sqlite", prefix="ahri_vtn_test_")
    defaults = dict(port=free_port(), host="127.0.0.1", db_url=f"sqlite:///{dbfile}", requested_poll_freq_seconds=1,
                    status_report_interval_seconds=1, default_event_duration_minutes=1)
    defaults.update(overrides)
    return Config(**defaults)


@pytest_asyncio.fixture
async def vtn():
    config = make_config()
    vtn = AhriVtn(config)
    await vtn.start()
    try:
        yield vtn
    finally:
        await vtn.stop()
        dbfile = config.db_url.replace("sqlite:///", "")
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(dbfile + suffix)
            except FileNotFoundError:
                pass


async def start_sim(vtn, ven_name: str = "sim-hvac", **kwargs) -> AhriSimVen:
    params = dict(status_interval=timedelta(seconds=1), static_interval=timedelta(seconds=5), ramp_seconds=1.0)
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


def ven_pk_of(vtn) -> int:
    return vtn.queries.list_vens()[0]["id"]
