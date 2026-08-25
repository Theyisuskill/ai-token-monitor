"""Live-limit poller registry.

Mirrors the adapter registry: pollers register under ``cls.name`` and are
instantiated for every entry enabled under the ``live_limits`` config key.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import (  # noqa: F401  (re-exported)
    QUIET_STATUSES,
    LivePoller,
    effective_live,
)

log = logging.getLogger(__name__)

_REGISTRY: dict[str, type[LivePoller]] = {}


def register(cls: type[LivePoller]) -> type[LivePoller]:
    _REGISTRY[cls.name] = cls
    return cls


def registered() -> dict[str, type[LivePoller]]:
    """Every poller class known to the process, by name (diagnostics)."""
    return dict(_REGISTRY)


def is_enabled(cls: type[LivePoller], conf: dict[str, Any]) -> bool:
    """Resolve ``live_limits.<name>.enabled``: true / false / ``"auto"``.

    ``auto`` turns the poller on only when the provider's credential is
    actually on this machine, so a poller stays free for people who don't use
    that tool without being dead weight for the people who do.
    """
    value = conf.get("enabled", False)
    if isinstance(value, str) and value.strip().lower() == "auto":
        return cls.is_available(conf)
    return bool(value)


def credential_tiers(settings: dict[str, dict[str, Any]] | None
                     ) -> dict[str, str]:
    """Plan tier each poller can read off its credential file, by tool.

    A local read, not a poll, so every registered poller is asked whether or
    not it is enabled: the plan decides which rate-limit windows an account
    even has, and getting that wrong shows a bar for a limit that doesn't
    exist. A poller that can't tell (most of them) simply returns None.
    """
    if not isinstance(settings, dict):
        settings = {}
    tiers: dict[str, str] = {}
    for name, cls in sorted(_REGISTRY.items()):
        conf = settings.get(name)
        if not isinstance(conf, dict):
            conf = {}
        try:
            tier = cls.offline_tier(conf)
        except Exception:  # a mangled credential must not break the caller
            log.exception("live poller %s: offline_tier failed", name)
            tier = None
        if tier:
            tiers[cls.tool] = tier
    return tiers


def create_enabled(settings: dict[str, dict[str, Any]] | None) -> list[LivePoller]:
    """Instantiate every registered poller enabled in ``live_limits``.

    Pollers are OFF unless enabled (explicitly, or by ``auto`` finding the
    tool's credential): they make network/loopback calls, so the local-only
    default is preserved until the user opts in.
    """
    if not isinstance(settings, dict):
        settings = {}
    enabled: list[LivePoller] = []
    for name, cls in sorted(_REGISTRY.items()):
        conf = settings.get(name, {})
        if not isinstance(conf, dict):
            conf = {"enabled": bool(conf)}
        if is_enabled(cls, conf):
            enabled.append(cls(conf))
    return enabled


# Built-in pollers register on import (needs `register` above).
from . import antigravity as _antigravity  # noqa: E402,F401
from . import claude as _claude  # noqa: E402,F401
from . import codex as _codex  # noqa: E402,F401
