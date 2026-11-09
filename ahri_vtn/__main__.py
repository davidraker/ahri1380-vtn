"""
Command line entry point.

    ahri-vtn [run] [--config config.toml] [--port 443] ...     start the VTN (default command)
    ahri-vtn certs quickstart|init-ca|vtn|ven|operator|bundle|fingerprint|show ...
    ahri-vtn hash-password                                      PBKDF2 hash for [ui] basic_auth_password_hash
    ahri-vtn gen-token                                          random API token for the AHRI_VTN_API_TOKENS variable
    ahri-vtn tls-selfcheck [--config config.toml]               handshake with each OpenADR 2.0b default suite
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys

from .config import ConfigError, load_config

SUBCOMMANDS = ("run", "certs", "hash-password", "gen-token", "tls-selfcheck")


def _add_run_args(parser):
    parser.add_argument("--config", "-c", help="TOML configuration file (default: config.toml if it exists)")
    parser.add_argument("--host", help="Bind address of the OpenADR listener (overrides config)")
    parser.add_argument("--port", type=int, help="TCP port of the OpenADR listener (overrides config)")
    parser.add_argument("--db", help="SQLAlchemy database URL (overrides config)")
    parser.add_argument("--vtn-id", help="VTN ID (overrides config)")
    parser.add_argument("--schema-mode", choices=["strict", "lenient"], help="XML schema validation mode")
    parser.add_argument("--log-level", default=None, help="Logging level (default INFO)")
    parser.add_argument("--no-web", action="store_true", help="Disable the web UI / API listener")


def _config_from_args(args):
    config_path = args.config
    if config_path is None and os.path.exists("config.toml"):
        config_path = "config.toml"
    config = load_config(config_path)
    for attr, name in (("host", "host"), ("port", "port"), ("db", "db_url"), ("vtn_id", "vtn_id"),
                       ("schema_mode", "schema_mode"), ("log_level", "log_level")):
        value = getattr(args, attr, None)
        if value:
            setattr(config, name, value)
    return config


def cmd_run(args) -> int:
    from .server import AhriVtn
    from .tls import TlsSetupError
    try:
        config = _config_from_args(args)
    except ConfigError as err:
        print(err, file=sys.stderr)
        return 2
    logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    level = getattr(logging, config.log_level.upper(), logging.INFO)
    logging.getLogger("openleadr").setLevel(logging.INFO if level <= logging.DEBUG else logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    try:
        vtn = AhriVtn(config, enable_web=not args.no_web)
    except TlsSetupError as err:
        print(f"TLS setup failed:\n{err}", file=sys.stderr)
        return 2
    except Exception as err:   # noqa: BLE001   (schema mismatch, unreadable database, ...)
        print(f"Could not start: {err.__class__.__name__}: {err}", file=sys.stderr)
        return 2
    vtn.run()
    return 0


def cmd_certs(args) -> int:
    from .certs import CertsError, LabPki
    pki = LabPki(args.dir)
    try:
        if args.certs_cmd == "init-ca":
            pki.init_ca(name=args.name, ecc=args.ecc, force=args.force)
        elif args.certs_cmd == "vtn":
            pki.vtn(args.san, days=args.days)
        elif args.certs_cmd == "ven":
            pki.ven(args.name, ecc=args.ecc, p12_password=args.p12_password, days=args.days)
        elif args.certs_cmd == "operator":
            pki.operator(args.name, p12_password=args.p12_password, days=args.days)
        elif args.certs_cmd == "bundle":
            pki.bundle_add(args.pem)
        elif args.certs_cmd == "fingerprint":
            pki.fingerprint(args.pem)
        elif args.certs_cmd == "show":
            config = None
            if args.config or os.path.exists("config.toml"):
                try:
                    config = load_config(args.config or "config.toml")
                except (ConfigError, FileNotFoundError):
                    config = None
            pki.show(config)
        elif args.certs_cmd == "quickstart":
            pki.quickstart(args.san, operator=args.operator, ven=args.ven, force=args.force)
        else:
            print("certs: missing subcommand (init-ca, vtn, ven, operator, bundle, fingerprint, show, quickstart)",
                  file=sys.stderr)
            return 2
    except CertsError as err:
        print(err, file=sys.stderr)
        return 2
    return 0


def cmd_hash_password(args) -> int:
    from .web.auth import hash_password
    password = args.password if args.password is not None else getpass.getpass("Password: ")
    if args.password is None and getpass.getpass("Repeat:   ") != password:
        print("Passwords do not match", file=sys.stderr)
        return 2
    print(hash_password(password))
    return 0


def cmd_gen_token(args) -> int:
    from .web.auth import generate_token
    print(generate_token())
    return 0


def cmd_tls_selfcheck(args) -> int:
    from .tls import TlsSetupError, describe_runtime, self_check
    try:
        config = _config_from_args(args)
        results = asyncio.run(self_check(config))
    except (ConfigError, TlsSetupError) as err:
        print(err, file=sys.stderr)
        return 2
    runtime = describe_runtime()
    print(f"OpenSSL: {runtime['openssl']}")
    ok = True
    for r in results:
        status = "OK  " if r.ok else "FAIL"
        extra = f"negotiated {r.negotiated} ({r.version})" if r.ok else (r.error or "")
        print(f"{status} {r.suite:45s} {extra}")
        ok = ok and r.ok
    if not ok:
        print("\nAt least one OpenADR 2.0b default cipher suite cannot be negotiated on this host. See the user "
              "guide section 'Guaranteeing the mandated cipher suites'.", file=sys.stderr)
    return 0 if ok else 1


def build_parser():
    parser = argparse.ArgumentParser(prog="ahri-vtn", description="AHRI 1380 OpenADR 2.0b VTN test tool")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="start the VTN (default)")
    _add_run_args(run)

    certs = sub.add_parser("certs", help="lab PKI: CA, VTN, VEN and operator certificates")
    certs.add_argument("--dir", default="certs", help="certificate directory (default: certs)")
    csub = certs.add_subparsers(dest="certs_cmd")
    p = csub.add_parser("init-ca", help="create the lab CA and the operator CA")
    p.add_argument("--name", default="AHRI 1380 Lab CA")
    p.add_argument("--ecc", action="store_true", help="ECC P-256 CA keys instead of RSA 3072")
    p.add_argument("--force", action="store_true", help="replace existing CA files")
    p = csub.add_parser("vtn", help="VTN server certificates (RSA and ECC) with the given names/addresses")
    p.add_argument("--san", action="append", default=[], help="DNS name or IP address the VENs use (repeatable)")
    p.add_argument("--days", type=int, default=825)
    p = csub.add_parser("ven", help="a VEN client certificate")
    p.add_argument("--name", required=True)
    p.add_argument("--ecc", action="store_true")
    p.add_argument("--p12-password", help="also write a PKCS#12 file with this password")
    p.add_argument("--days", type=int, default=825)
    p = csub.add_parser("operator", help="an operator certificate (+ PKCS#12 for the browser)")
    p.add_argument("--name", required=True)
    p.add_argument("--p12-password", help="PKCS#12 import password (default: generated)")
    p.add_argument("--days", type=int, default=825)
    p = csub.add_parser("bundle", help="add a vendor CA or self-signed VEN certificate to ven-trust.pem")
    p.add_argument("add_literal", metavar="add", choices=["add"])
    p.add_argument("pem")
    p = csub.add_parser("fingerprint", help="OpenADR and SHA-256 fingerprints of a PEM certificate")
    p.add_argument("pem")
    p = csub.add_parser("show", help="list the certificates in the directory and the configuration")
    p.add_argument("--config", "-c")
    p = csub.add_parser("quickstart", help="init-ca + vtn + ven + operator in one go")
    p.add_argument("--san", action="append", default=[], help="DNS name or IP address of the VTN (repeatable)")
    p.add_argument("--operator", default="lab")
    p.add_argument("--ven", default="sim-hvac")
    p.add_argument("--force", action="store_true")

    hp = sub.add_parser("hash-password", help="hash an operator password for the configuration")
    hp.add_argument("--password", help="password (prompted when omitted; the flag ends up in shell history and `ps`, "
                                       "so prefer the prompt outside scripts)")

    sub.add_parser("gen-token", help="generate an API token")

    sc = sub.add_parser("tls-selfcheck", help="verify the OpenADR 2.0b default cipher suites on this host")
    sc.add_argument("--config", "-c")
    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in SUBCOMMANDS and not argv[0] in ("-h", "--help"):
        argv = ["run"] + argv          # flat invocation = run
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "certs":
        return cmd_certs(args)
    if args.command == "hash-password":
        return cmd_hash_password(args)
    if args.command == "gen-token":
        return cmd_gen_token(args)
    if args.command == "tls-selfcheck":
        return cmd_tls_selfcheck(args)
    build_parser().print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
