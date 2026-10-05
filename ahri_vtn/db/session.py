"""
SQLAlchemy engine / session management. Synchronous sessions are used: the VTN handles a
handful of messages per minute and SQLite writes are sub-millisecond, so the event loop is
not measurably blocked. Swap the URL for PostgreSQL if concurrent access is needed.
"""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


class Database:
    def __init__(self, url: str, echo: bool = False):
        self.url = url
        connect_args = {}
        if url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        self.engine = create_engine(url, echo=echo, connect_args=connect_args, future=True)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _set_sqlite_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
        self._sessionmaker = sessionmaker(self.engine, expire_on_commit=False, future=True)

    def create_all(self):
        Base.metadata.create_all(self.engine)

    def drop_all(self):
        Base.metadata.drop_all(self.engine)

    @contextmanager
    def session(self) -> Session:
        """Transactional scope: commits on success, rolls back on exception."""
        session = self._sessionmaker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self):
        self.engine.dispose()
