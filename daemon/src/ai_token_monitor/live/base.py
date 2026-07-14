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

# How long the last OK poll result may keep serving data once the poller
# starts failing. A transient failure (network blip, provider 5xx) must not
# blank the provider-real bars — the UI would flash back to the much lower
# dollar estimate — but data older than this is no better than the estimate.
LAST_OK_TTL_S = 30 * 60.0


def effective_live(latest: dict[str, Any], last_ok: dict[str, Any] | None,
                   now: float, ttl_s: float = LAST_OK_TTL_S,
                   ) -> tuple[dict[str, Any] | None, bool]:
    """The poll result whose data should drive the UI, and whether it's stale.

    While the latest poll is OK it wins. While it is failing, the previous OK
    result keeps serving until it ages past ``ttl_s``; windows and scoped
    entries whose reset has already passed are dropped (their percentage
    refers to a window that no longer exists). ``(None, False)`` when nothing
    usable is left.
    """
    if latest.get("status") == "ok":
        return latest, False
    if not last_ok:
        return None, False
    if now - float(last_ok.get("fetched_at") or 0.0) > ttl_s:
        return None, False
    windows = {key: w for key, w in (last_ok.get("windows") or {}).items()
               if not w.get("resets_at") or float(w["resets_at"]) > now}
    scoped = [s for s in (last_ok.get("scoped") or [])
              if not s.get("resets_at") or float(s["resets_at"]) > now]
    if not windows and not scoped:
        return None, False
    return {**last_ok, "windows": windows, "scoped": scoped}, True


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
