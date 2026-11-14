"""intercom profiles — reach another Hermes profile on this machine, explicitly.

A ``profile`` on ``send`` or ``list`` names the target profile. ``default`` is the Hermes root,
any other name ``<root>/profiles/<name>``, checked with the published ``PROFILE_ID_RE`` and
``named_profile_is_live``. An unknown or deleted profile has no home.

``in_profile(name)`` runs its block with the published context-local home override
(``set_hermes_home_override`` / ``reset_hermes_home_override``, always reset), so discovery,
the ticket, ``state.db``, the config and the record all resolve to that profile. The override
is a context variable: it does not touch ``os.environ`` and other threads never see it.

Inside it, ``enabled()`` says whether intercom is on there (in ``plugins.enabled`` and not in
``plugins.disabled``) and ``Settings`` reads that profile's intercom settings, in place of the
sender's plugin ``ctx``.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

PLUGIN = "intercom"
DEFAULT = "default"


def current() -> str | None:
    """The profile name of the active Hermes home (``default`` for the root), or None."""
    from hermes_constants import get_hermes_home, profile_name_for_home

    return profile_name_for_home(get_hermes_home())


def home_for(name) -> Path | None:
    """The home of profile *name*, or None when it is not a live profile on this machine."""
    from hermes_constants import PROFILE_ID_RE, get_default_hermes_root, named_profile_is_live

    if not isinstance(name, str):
        return None
    root = get_default_hermes_root()
    if name == DEFAULT:
        return root
    if not PROFILE_ID_RE.match(name):
        return None
    home = root / "profiles" / name
    return home if named_profile_is_live(home) else None


def other(name) -> str | None:
    """*name* stripped, or None when it is absent, empty or the active profile itself."""
    name = name.strip() if isinstance(name, str) else ""
    if not name or name == current():
        return None
    return name


@contextmanager
def in_profile(name):
    """Run the block in profile *name*'s home. ``LookupError`` when it has none."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = home_for(name)
    if home is None:
        raise LookupError(name)
    token = set_hermes_home_override(home)
    try:
        yield home
    finally:
        reset_hermes_home_override(token)


def enabled() -> bool:
    """Intercom is in the active profile's ``plugins.enabled`` and not in ``plugins.disabled``."""
    plugins = _config().get("plugins")
    if not isinstance(plugins, dict):
        return False
    return PLUGIN in _names(plugins.get("enabled")) and PLUGIN not in _names(plugins.get("disabled"))


class Settings:
    """``ctx.get_config`` for the active profile: ``plugins.entries.intercom.settings.<key>``
    (else the legacy ``config`` subtree), read from its config on every call."""

    def get_config(self, key, default=None):
        plugins = _config().get("plugins")
        entries = plugins.get("entries") if isinstance(plugins, dict) else None
        entry = entries.get(PLUGIN) if isinstance(entries, dict) else None
        if not isinstance(entry, dict):
            return default
        for subtree in ("settings", "config"):
            values = entry.get(subtree)
            if isinstance(values, dict) and key in values:
                return values[key]
        return default


# ── internals ────────────────────────────────────────────────────────────────


def _config() -> dict:
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
    except Exception:  # health: allow BLE001 -- an unreadable config reads as empty (intercom off)
        return {}
    return config if isinstance(config, dict) else {}


def _names(value) -> set:
    return {v for v in value if isinstance(v, str)} if isinstance(value, list) else set()
