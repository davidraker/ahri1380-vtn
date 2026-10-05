"""
Web UI and JSON API, served from the same aiohttp application as the OpenADR endpoints.
"""
from __future__ import annotations


def setup_web(vtn):
    from .api import setup_api
    from .ui import setup_ui
    setup_api(vtn)
    setup_ui(vtn)
