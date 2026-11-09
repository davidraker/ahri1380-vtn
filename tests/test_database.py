"""
The database layer: SQLite and PostgreSQL through one URL, non-blocking access from the event
loop through Database.run, plain blocking access elsewhere.
"""
import asyncio
import os
import time

import pytest
from sqlalchemy import select, text

from ahri_vtn.db import Database
from ahri_vtn.db.models import Meta, Ven
from ahri_vtn.db.session import UnsupportedDatabase, _driver_urls
from sqlalchemy.engine import make_url

from .conftest import TEST_DB_URL

def test_driver_mapping():
    s, a = _driver_urls(make_url("sqlite:///x.sqlite"))
    assert s.drivername == "sqlite" and a.drivername == "sqlite+aiosqlite"
    s, a = _driver_urls(make_url("postgresql://u@h/db"))
    assert s.drivername == "postgresql+psycopg" and a.drivername == "postgresql+psycopg"
    s, a = _driver_urls(make_url("postgresql+psycopg2://u@h/db"))
    assert s.drivername == "postgresql+psycopg"
    with pytest.raises(UnsupportedDatabase):
        _driver_urls(make_url("mysql://u@h/db"))


def test_password_is_injected_but_not_shown(tmp_path):
    db = Database("postgresql://user@dbhost/ahri", password="s3cret")
    assert db.engine.url.password == "s3cret"
    assert "s3cret" not in db.url and db.url.startswith("postgresql://user:***@dbhost")
    db.dispose()


@pytest.mark.asyncio
async def test_run_uses_the_async_engine_and_results_match(tmp_path):
    url = TEST_DB_URL or f"sqlite:///{tmp_path / 'd.sqlite'}"
    db = Database(url)
    if not url.startswith("sqlite"):
        db.drop_all()
    db.create_all()
    try:
        assert not db.in_run

        def add_and_count(name):
            assert db.in_run                                   # sessions are bound to the async engine here
            with db.session() as s:
                s.add(Ven(ven_name=name))
            with db.session() as s:
                return s.scalar(select(Meta.value).where(Meta.key == "schema_version")), \
                    len(s.scalars(select(Ven)).all())

        version, count = await db.run(add_and_count, "a")
        assert version and count == 1
        with db.session() as s:                                # plain blocking access still works
            assert len(s.scalars(select(Ven)).all()) == 1

        with pytest.raises(ValueError):
            def boom():
                with db.session():
                    raise ValueError("propagates unchanged")
            await db.run(boom)
    finally:
        await db.aclose()


@pytest.mark.asyncio
async def test_run_does_not_block_the_event_loop(tmp_path):
    """While a slow statement waits for the database, other tasks keep running."""
    url = TEST_DB_URL or f"sqlite:///{tmp_path / 'd.sqlite'}"
    db = Database(url)
    if not url.startswith("sqlite"):
        db.drop_all()
    db.create_all()
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    def slow():
        with db.session() as s:
            if db.backend == "postgresql":
                s.execute(text("SELECT pg_sleep(0.5)"))
            else:
                # SQLite has no sleep: a burst of statements, each awaiting the aiosqlite thread
                for _ in range(300):
                    s.execute(text("SELECT 1"))
    try:
        task = asyncio.get_event_loop().create_task(ticker())
        t0 = time.monotonic()
        await db.run(slow)
        elapsed = time.monotonic() - t0
        task.cancel()
        assert ticks >= 5, f"loop starved: {ticks} ticks in {elapsed:.2f}s"
    finally:
        await db.aclose()


@pytest.mark.asyncio
async def test_schema_version_mismatch_is_refused(tmp_path):
    url = TEST_DB_URL or f"sqlite:///{tmp_path / 'v.sqlite'}"
    db = Database(url)
    if not url.startswith("sqlite"):
        db.drop_all()
    db.create_all()
    with db.session() as s:
        s.get(Meta, "schema_version").value = "1"
    from ahri_vtn.db.session import SchemaMismatch
    with pytest.raises(SchemaMismatch):
        Database(url).create_all()
    db.drop_all()
    await db.aclose()
