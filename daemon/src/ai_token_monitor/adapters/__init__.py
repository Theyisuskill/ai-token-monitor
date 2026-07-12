"""Adapter registry with entry-point discovery for third-party plugins."""

from __future__ import annotations

import logging
from typing import Any

from .base import Adapter

log = logging.getLogger(__name__)

_REGISTRY: dict[str, type[Adapter]] = {}


def register(cls: type[Adapter]) -> type[Adapter]:
    """Class decorator: make an Adapter available under ``cls.name``."""
    existing = _REGISTRY.get(cls.name)
    if existing is not None and existing is not cls:
        log.warning("Adapter %r re-registered by %s", cls.name, cls.__module__)
    _REGISTRY[cls.name] = cls
    return cls


def _load_external() -> None:
    """Load adapters exposed by other packages via entry points."""
    from importlib.metadata import entry_points

    for ep in entry_points(group="ai_token_monitor.adapters"):
        try:
            register(ep.load())
        except Exception:  # a broken plugin must not kill the daemon
            log.exception("Failed to load adapter entry point %r", ep.name)


def create_enabled(adapter_settings: dict[str, dict[str, Any]] | None) -> list[Adapter]:
    """Instantiate every registered adapter enabled in the config.

    Tolerant of the shapes a hand-edited config.yaml can plausibly produce:
    an empty/null ``adapters:`` key, or a per-adapter value that's a bare
    scalar (``gemini_cli: false``) instead of a mapping — both are common
    ways a user would try to disable an adapter.
    """
    _load_external()
    if not isinstance(adapter_settings, dict):
        adapter_settings = {}
    enabled: list[Adapter] = []
    for name, cls in sorted(_REGISTRY.items()):
        settings = adapter_settings.get(name, {})
        if not isinstance(settings, dict):
            log.warning("adapters.%s in config.yaml must be a mapping "
                       "(got %r) — treating it as enabled=%s",
                       name, settings, bool(settings))
            settings = {"enabled": bool(settings)}
        if settings.get("enabled", False):
            enabled.append(cls(settings))
        else:
            log.debug("Adapter %s disabled", name)
    return enabled


# Built-in adapters register themselves on import. Keep these imports at the
# bottom: they need `register` to exist. Adapters for other tools should ship
# as separate packages via the ai_token_monitor.adapters entry-point group.
from . import claude_code as _claude_code  # noqa: E402,F401
from . import codex as _codex  # noqa: E402,F401
from . import gemini_cli as _gemini_cli  # noqa: E402,F401
from . import opencode as _opencode  # noqa: E402,F401
