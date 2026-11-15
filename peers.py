"""intercom peers — the remote agents of the active profile (peers.md, P2).

``plugin-data/intercom/peers.json`` is written by house-infra, never by the plugin. It is a JSON
list with one object per remote agent:

- ``name``: ``^[a-z0-9-]{1,63}$``; the same string is the certificate CN, the webhook route
  (``peer-<name>``) and the address (``peer:<name>``);
- ``url``: the agent's webhook;
- ``auth``: ``url`` (the secret is in the url) or ``hmac-v2``;
- ``secret_file``: where the secret is kept.

``load()`` reads it fresh on every call. A missing, unreadable or malformed file, or any bad
or repeated entry, means no peers: nothing is half-trusted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

PLUGIN = "intercom"
FILENAME = "peers.json"
AUTH_MODES = ("url", "hmac-v2")
NAME_RE = re.compile(r"^[a-z0-9-]{1,63}$")


@dataclass(frozen=True)
class Peer:
    name: str
    url: str
    auth: str
    secret_file: str


def load() -> dict[str, Peer]:
    """The peers of the active profile by name; empty when the file is absent or bad."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, list):
        return {}
    found: dict[str, Peer] = {}
    for entry in data:
        peer = _peer(entry)
        if peer is None or peer.name in found:
            return {}
        found[peer.name] = peer
    return found


def path():
    """``<home>/plugin-data/intercom/peers.json`` of the active profile. Reading it creates nothing."""
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "plugin-data" / PLUGIN / FILENAME


def _peer(entry) -> Peer | None:
    if not isinstance(entry, dict):
        return None
    name, url, auth, secret_file = (entry.get(k) for k in ("name", "url", "auth", "secret_file"))
    if not (isinstance(name, str) and NAME_RE.match(name)):
        return None
    if not (isinstance(url, str) and url) or auth not in AUTH_MODES:
        return None
    if not (isinstance(secret_file, str) and secret_file):
        return None
    return Peer(name=name, url=url, auth=auth, secret_file=secret_file)
