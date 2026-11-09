"""
Awaitable views of the synchronous service objects (queries, registry, reports, events, groups,
conformance). A call on a proxy runs through Database.run, so request handlers, scenarios and
tests can `await vtn.aio.q.list_vens()` without blocking the event loop while the database
answers; coroutine functions pass straight through and plain attributes are returned as they are.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace


class AsyncProxy:
    def __init__(self, target, run):
        self._target = target
        self._run = run

    def __getattr__(self, name):
        attr = getattr(self._target, name)
        if not callable(attr) or inspect.iscoroutinefunction(attr):
            return attr

        async def call(*args, **kwargs):
            return await self._run(attr, *args, **kwargs)
        return call


def service_proxies(vtn) -> dict:
    run = vtn.db.run
    return {"q": AsyncProxy(vtn.queries, run), "registry": AsyncProxy(vtn.registry, run),
            "reports": AsyncProxy(vtn.reports, run), "events": AsyncProxy(vtn.events, run),
            "groups": AsyncProxy(vtn.groups, run), "conformance": AsyncProxy(vtn.conformance, run)}


def service_namespace(vtn) -> SimpleNamespace:
    return SimpleNamespace(**service_proxies(vtn))
