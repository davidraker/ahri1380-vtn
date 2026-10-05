"""Small shared helpers."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger("ahri_vtn")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def jsonable(value: Any) -> Any:
    """Recursively convert OpenLEADR payloads (datetimes, timedeltas, dataclasses) to JSON-safe data."""
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def seconds(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, timedelta):
        return value.total_seconds()
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fire_and_forget(coro, name: str = "task"):
    task = asyncio.get_event_loop().create_task(coro)

    def _done(t):
        try:
            t.result()
        except asyncio.CancelledError:
            pass
        except Exception as err:    # noqa: BLE001
            logger.error(f"Background task {name} failed: {err.__class__.__name__}: {err}", exc_info=True)
    task.add_done_callback(_done)
    return task
