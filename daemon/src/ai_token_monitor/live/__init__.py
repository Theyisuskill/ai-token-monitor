"""Live-limit poller registry.

Mirrors the adapter registry: pollers register under ``cls.name`` and are
instantiated for every entry enabled under the ``live_limits`` config key.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import LivePoller

log = logging.getLogger(__name__)

_REGISTRY: dict[str, type[LivePoller]] = {}


def register(cls: type[LivePoller]) -> type[LivePoller]:
    _REGISTRY[cls.name] = cls
    return cls


def create_enabled(settings: dict[str, dict[str, Any]] | None) -> list[LivePoller]:
    """Instantiate every registered poller enabled in ``live_limits``.

    Pollers are OFF unless explicitly enabled: they make network/loopback
    calls, so the local-only default is preserved until the user opts in.
    """
    if not isinstance(settings, dict):
        settings = {}
    enabled: list[LivePoller] = []
    for name, cls in sorted(_REGISTRY.items()):
        conf = settings.get(name, {})
        if not isinstance(conf, dict):
            conf = {"enabled": bool(conf)}
        if conf.get("enabled", False):
            enabled.append(cls(conf))
    return enabled


# Built-in pollers register on import (needs `register` above).
from . import antigravity as _antigravity  # noqa: E402,F401
from . import claude as _claude  # noqa: E402,F401
from . import codex as _codex  # noqa: E402,F401
