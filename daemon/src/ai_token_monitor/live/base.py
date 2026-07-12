"""Live-limit pollers: read the provider's OWN rate-limit numbers.

Unlike adapters (pure local-log parsers), a poller reads a credential that
already lives on disk and asks the provider for the authoritative usage: the
real 5h/weekly percentage and reset time, not a dollar-scaled estimate. This
is a deliberate, opt-in relaxation of the app's local-only default — a poller
makes an outbound (or loopback) request — so pollers are configured under the
``live_limits`` config key and can be disabled per tool.

``poll()`` runs OFF the GLib main loop (in a worker thread), so it must not
touch the store or GLib; it returns a normalized dict the daemon merges into
the snapshot on the main thread. It must never raise — every failure becomes a
``status`` string so the UI can show why a bar is stale.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar


class LivePoller(ABC):
    #: Unique key; also the ``live_limits.<name>`` section in config.
    name: ClassVar[str]
    #: The ``UsageRecord.tool`` whose snapshot windows this augments.
    tool: ClassVar[str]

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings

    @property
    def interval(self) -> int:
        """Seconds between polls (clamped to a sane floor)."""
        return max(30, int(self.settings.get("interval_s", 90) or 90))

    @abstractmethod
    def poll(self) -> dict[str, Any]:
        """Fetch and normalize the provider's real limits.

        Returns a dict with at least ``status`` and ``fetched_at``; on success
        (``status == "ok"``) also ``windows`` (mapping ``five_hour``/``weekly``
        to ``{used_percent, resets_at}``), optional ``scoped`` and
        ``extra_usage``, and ``plan_tier``. Must not raise.
        """
