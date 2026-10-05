"""
Command line entry point: python -m ahri_vtn [--config config.toml] [--port 8080] ...
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import load_config
from .server import AhriVtn


def main(argv=None):
    parser = argparse.ArgumentParser(description="AHRI 1380 OpenADR 2.0b VTN test tool")
    parser.add_argument("--config", "-c", help="TOML configuration file (default: config.toml if it exists)")
    parser.add_argument("--host", help="Bind address (overrides config)")
    parser.add_argument("--port", type=int, help="TCP port (overrides config)")
    parser.add_argument("--db", help="SQLAlchemy database URL (overrides config)")
    parser.add_argument("--vtn-id", help="VTN ID (overrides config)")
    parser.add_argument("--schema-mode", choices=["strict", "lenient"], help="XML schema validation mode")
    parser.add_argument("--log-level", default=None, help="Logging level (default INFO)")
    parser.add_argument("--no-web", action="store_true", help="Disable the web UI / API")
    args = parser.parse_args(argv)

    config_path = args.config
    if config_path is None and os.path.exists("config.toml"):
        config_path = "config.toml"
    config = load_config(config_path)
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port
    if args.db:
        config.db_url = args.db
    if args.vtn_id:
        config.vtn_id = args.vtn_id
    if args.schema_mode:
        config.schema_mode = args.schema_mode
    if args.log_level:
        config.log_level = args.log_level

    logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # OpenLEADR logs every message at INFO; keep it quiet unless debugging
    level = getattr(logging, config.log_level.upper(), logging.INFO)
    logging.getLogger("openleadr").setLevel(logging.INFO if level <= logging.DEBUG else logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    vtn = AhriVtn(config, enable_web=not args.no_web)
    vtn.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
