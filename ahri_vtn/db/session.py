"""
SQLAlchemy engine / session management for SQLite (default) and PostgreSQL, including over a
network, without blocking the asyncio event loop.

Two engines share one URL:

- ``engine`` is a plain synchronous engine. It serves start-up (schema creation / version check),
  command-line tools, tests and any code that runs outside the event loop. Using it from the loop
  blocks the loop for the duration of the query; that is acceptable for those callers.
- ``async_engine`` uses the asynchronous driver (aiosqlite, psycopg). Request handlers, OpenLEADR
  callbacks and the UI run their synchronous database code through :meth:`Database.run`, which
  executes it in a greenlet on the loop thread (SQLAlchemy's ``greenlet_spawn``): every statement
  awaits the driver, other tasks run meanwhile, and the code itself stays ordinary synchronous
  SQLAlchemy. Inside such a call :meth:`Database.session` transparently hands out sessions bound
  to the asynchronous engine.

Because all of it executes on the loop thread, OpenLEADR, the live bus and the handlers need no
locking. Deployment: ``db_url = "postgresql://user@dbhost/ahri_vtn"`` with the password supplied
through the environment variable named by ``db_password_env``; add ``?sslmode=verify-full&
sslrootcert=/path/ca.pem`` for TLS to the database server.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.util import greenlet_spawn

from .models import SCHEMA_VERSION, Base, Meta

logger = logging.getLogger("ahri_vtn.db")

# True while a synchronous function runs inside Database.run (a SQLAlchemy greenlet on the loop)
_in_run: ContextVar[bool] = ContextVar("ahri_vtn_db_in_run", default=False)

SUPPORTED = ("sqlite", "postgresql")


class SchemaMismatch(RuntimeError):
    pass


class UnsupportedDatabase(ValueError):
    pass


def _driver_urls(url: URL) -> tuple[URL, URL]:
    """(synchronous URL, asynchronous URL) for the configured backend."""
    backend = url.get_backend_name()
    if backend == "sqlite":
        return url.set(drivername="sqlite"), url.set(drivername="sqlite+aiosqlite")
    if backend == "postgresql":
        # psycopg (3) serves both the synchronous and the asynchronous engine
        return url.set(drivername="postgresql+psycopg"), url.set(drivername="postgresql+psycopg")
    raise UnsupportedDatabase(f"Unsupported database '{backend}'; use sqlite:// or postgresql:// "
                              f"(supported: {', '.join(SUPPORTED)})")


class Database:
    def __init__(self, url: str, echo: bool = False, password: Optional[str] = None, pool_size: int = 5):
        parsed = make_url(url)
        if password:
            parsed = parsed.set(password=password)
        self.backend = parsed.get_backend_name()
        sync_url, async_url = _driver_urls(parsed)
        self.url = parsed.render_as_string(hide_password=True)

        if self.backend == "sqlite":
            # A busy timeout lets the two engines share the file; WAL keeps readers off the writer.
            sync_kwargs = dict(connect_args={"check_same_thread": False, "timeout": 30})
            async_kwargs = dict(connect_args={"timeout": 30})
        else:
            common = dict(pool_pre_ping=True, pool_recycle=1800)
            sync_kwargs = dict(pool_size=2, max_overflow=3, **common)
            async_kwargs = dict(pool_size=pool_size, max_overflow=pool_size * 2, **common)

        self.engine = create_engine(sync_url, echo=echo, future=True, **sync_kwargs)
        self.async_engine = create_async_engine(async_url, echo=echo, **async_kwargs)
        if self.backend == "sqlite":
            for eng in (self.engine, self.async_engine.sync_engine):
                event.listens_for(eng, "connect")(self._set_sqlite_pragma)
        self._sync_sessions = sessionmaker(self.engine, expire_on_commit=False, future=True)
        self._loop_sessions = sessionmaker(self.async_engine.sync_engine, expire_on_commit=False, future=True)

    @staticmethod
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    # ------------------------------------------------------------------ schema

    def create_all(self):
        """
        Create the schema in an empty database, or verify the version of an existing one.
        Databases from another schema version are refused: delete the file / drop the schema (the
        tool is not deployed, so there is no migration path) and start again.
        """
        existing = inspect(self.engine).get_table_names()
        if existing and "meta" not in existing:
            raise SchemaMismatch(f"Database {self.url} predates the schema version table; recreate it and restart")
        Base.metadata.create_all(self.engine)
        with self._sync_sessions() as s:
            row = s.get(Meta, "schema_version")
            if row is None:
                if existing:
                    raise SchemaMismatch(f"Database {self.url} has no schema version; recreate it and restart")
                s.add(Meta(key="schema_version", value=str(SCHEMA_VERSION)))
                s.commit()
            elif row.value != str(SCHEMA_VERSION):
                raise SchemaMismatch(f"Database {self.url} has schema version {row.value}, this build needs "
                                     f"{SCHEMA_VERSION}; recreate the database and restart")

    def drop_all(self):
        Base.metadata.drop_all(self.engine)

    # ------------------------------------------------------------------ sessions

    @property
    def in_run(self) -> bool:
        """Whether the caller executes inside Database.run (and therefore gets non-blocking sessions)."""
        return _in_run.get()

    @contextmanager
    def session(self) -> Session:
        """
        Transactional scope: commits on success, rolls back on exception. Inside Database.run the
        session is bound to the asynchronous engine; elsewhere it is a plain blocking session.
        """
        maker = self._loop_sessions if _in_run.get() else self._sync_sessions
        session = maker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    async def run(self, fn, *args, **kwargs):
        """
        Run synchronous database code without blocking the event loop. `fn` and everything it
        calls may use Database.session() freely; each statement awaits the asynchronous driver.
        Exceptions propagate unchanged.
        """
        def runner():
            token = _in_run.set(True)
            try:
                return fn(*args, **kwargs)
            finally:
                _in_run.reset(token)
        return await greenlet_spawn(runner)

    # ------------------------------------------------------------------ teardown

    def dispose(self):
        self.engine.dispose()

    async def aclose(self):
        self.engine.dispose()
        await self.async_engine.dispose()

    def describe(self) -> dict:
        return {"backend": self.backend, "url": self.url, "async_driver": self.async_engine.url.drivername}
