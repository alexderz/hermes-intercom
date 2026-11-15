#!/usr/bin/env python3
"""Render the agent-peer proxy's Caddy JSON from the active pin folder.

Stdlib only. One route per top-level ``<name>.pem`` whose stem matches
``^[a-z0-9-]{1,63}$``. Other top-level ``*.pem`` names are skipped with a
warning: the leaf folder loader still trusts them at TLS, and they have no
route, so those clients get 403.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Peer name, certificate CN, pin file stem and route name are the same string.
PEER_NAME = re.compile(r"^[a-z0-9-]{1,63}$")
SUBJECT = "{http.request.tls.client.subject}"


def _die(message: str) -> None:
    raise SystemExit(f"render: {message}")


def _require_text(value: str | None, flag: str) -> str:
    if value is None or value == "":
        _die(f"{flag} is required")
    return value


def peer_names(active: str) -> list[str]:
    """Return sorted peer names that get a route. Warn for other top-level pems."""
    folder = Path(active)
    if not folder.is_dir():
        _die(f"--active is not a directory: {active}")
    names: list[str] = []
    for entry in sorted(folder.iterdir(), key=lambda item: item.name):
        if entry.suffix != ".pem":
            continue
        if not entry.is_file():
            print(
                f"warning: skipping {entry.name}: not a file",
                file=sys.stderr,
            )
            continue
        stem = entry.stem
        if PEER_NAME.fullmatch(stem):
            names.append(stem)
            continue
        print(
            f"warning: skipping {entry.name}: "
            f"stem is not a peer name matching {PEER_NAME.pattern}",
            file=sys.stderr,
        )
    return names


def _peer_route(name: str, upstream: str) -> dict:
    path = f"/webhooks/peer-{name}"
    # Matchers in one object are AND. The expression uses the full
    # placeholder: the Caddyfile shorthand {tls_client_subject} is not
    # expanded in JSON, so it never matches.
    return {
        "handle": [
            {"handler": "rewrite", "uri": path},
            {
                "handler": "reverse_proxy",
                "upstreams": [{"dial": upstream}],
            },
        ],
        "match": [
            {
                "expression": f'{SUBJECT} == "CN={name}"',
                "path": [path],
            }
        ],
    }


def _certificate_source(
    server_name: str,
    cert: str | None,
    key: str | None,
    acme_dns: str | None,
) -> dict:
    have_cert = bool(cert) or bool(key)
    have_acme = bool(acme_dns)
    if have_cert and have_acme:
        _die("pass either --cert/--key or --acme-dns, not both")
    if have_cert:
        if not cert or not key:
            _die("--cert and --key are both required")
        return {
            "certificates": {
                "load_files": [
                    {"certificate": cert, "key": key},
                ]
            }
        }
    if not have_acme:
        _die("pass --cert/--key or --acme-dns")
    try:
        provider = json.loads(Path(acme_dns).read_text(encoding="utf-8"))
    except OSError as exc:
        _die(f"reading --acme-dns: {exc}")
    except json.JSONDecodeError as exc:
        _die(f"--acme-dns is not JSON: {exc}")
    if not isinstance(provider, dict):
        _die("--acme-dns must contain a JSON object")
    # The object is the dns provider, unchanged. sort_keys on the way out
    # only orders keys; it does not rename or wrap fields.
    return {
        "automation": {
            "policies": [
                {
                    "issuers": [
                        {
                            "challenges": {
                                "dns": {"provider": provider},
                                "http": {"disabled": True},
                                "tls-alpn": {"disabled": True},
                            },
                            "module": "acme",
                        }
                    ],
                    "subjects": [server_name],
                }
            ]
        },
        "certificates": {"automate": [server_name]},
    }


def render_config(
    *,
    listen: str,
    server_name: str,
    ca: str,
    active: str,
    upstream: str,
    admin_socket: str,
    cert: str | None = None,
    key: str | None = None,
    acme_dns: str | None = None,
) -> dict:
    """Build the proxy config. Paths are stored as given."""
    listen = _require_text(listen, "--listen")
    server_name = _require_text(server_name, "--server-name")
    ca = _require_text(ca, "--ca")
    active = _require_text(active, "--active")
    upstream = _require_text(upstream, "--upstream")
    admin_socket = _require_text(admin_socket, "--admin-socket")

    routes = [_peer_route(name, upstream) for name in peer_names(active)]
    routes.append(
        {"handle": [{"handler": "static_response", "status_code": 403}]}
    )

    # One policy, no match, so a different SNI cannot select a policy that
    # skips client authentication. automatic_https is off so Caddy does not
    # bind port 80 or add a catch-all automation policy. HTTP/3 stays off:
    # Caddy 2.11 client auth and HTTP/3 do not agree.
    server = {
        "automatic_https": {"disable": True},
        "listen": [listen],
        "protocols": ["h1", "h2"],
        "routes": routes,
        "tls_connection_policies": [
            {
                "client_authentication": {
                    "ca": {
                        "pem_files": [ca],
                        "provider": "file",
                    },
                    "mode": "require_and_verify",
                    "verifiers": [
                        {
                            "leaf_certs_loaders": [
                                {
                                    "folders": [active],
                                    "loader": "folder",
                                }
                            ],
                            "verifier": "leaf",
                        }
                    ],
                }
            }
        ],
    }
    return {
        "admin": {
            "config": {"persist": False},
            "listen": "unix/" + admin_socket,
        },
        "apps": {
            "http": {"servers": {"agent-peers": server}},
            "tls": _certificate_source(server_name, cert, key, acme_dns),
        },
    }


def dump_config(config: dict) -> str:
    return json.dumps(config, indent=2, sort_keys=True) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="render.py",
        description=(
            "Render the agent-peer proxy Caddy JSON from the active pin folder."
        ),
    )
    parser.add_argument("--listen", required=True, help="listen address, host:port")
    parser.add_argument("--server-name", required=True, help="proxy hostname")
    parser.add_argument("--ca", required=True, help="PEM file of the client CA")
    parser.add_argument("--active", required=True, help="directory of <name>.pem pins")
    parser.add_argument("--upstream", required=True, help="upstream dial address")
    parser.add_argument("--admin-socket", required=True, help="unix socket for the admin API")
    parser.add_argument("--cert", help="server certificate PEM")
    parser.add_argument("--key", help="server private key PEM")
    parser.add_argument(
        "--acme-dns",
        help="JSON file whose object is the ACME DNS provider",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = render_config(
        listen=args.listen,
        server_name=args.server_name,
        ca=args.ca,
        active=args.active,
        upstream=args.upstream,
        admin_socket=args.admin_socket,
        cert=args.cert,
        key=args.key,
        acme_dns=args.acme_dns,
    )
    sys.stdout.write(dump_config(config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
