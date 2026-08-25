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
from pathlib import Path
from typing import Any, ClassVar

# How long the last OK poll result may keep serving data once the poller
# starts failing. A transient failure (network blip, provider 5xx) must not
# blank the provider-real bars — the UI would flash back to the much lower
# dollar estimate — but data older than this is no better than the estimate.
LAST_OK_TTL_S = 30 * 60.0

#: Statuses that mean "nothing to report", not "something went wrong". A tool
#: you simply aren't running has no real limits to show, and saying so in amber
#: every time you open the popup is noise the user cannot act on — it trains
#: them to ignore the warning that does matter. The UI hides these; the journal
#: logs them at INFO instead of WARNING, and `--live` still prints them.
QUIET_STATUSES = frozenset({"not_running", "not_configured", "disabled"})


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
    #: Files whose presence means "the user actually has this tool set up".
    #: Only consulted for ``enabled: auto`` — see ``is_available``.
    credential_paths: ClassVar[tuple[str, ...]] = ()
    #: Settings key holding a user override for the first credential path.
    credential_setting: ClassVar[str] = "credentials"

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings

    @classmethod
    def is_available(cls, settings: dict[str, Any]) -> bool:
        """Whether this provider looks set up on this machine.

        Backs ``enabled: auto``: a poller that costs nothing when the tool is
        absent should not have to be turned on by hand by everyone who does
        use it. Presence is judged by the credential the poller would read.
        """
        override = settings.get(cls.credential_setting)
        paths = (str(override),) if override else cls.credential_paths
        return any(Path(p).expanduser().exists() for p in paths)

    @classmethod
    def offline_tier(cls, settings: dict[str, Any]) -> str | None:
        """The plan tier readable from the credential itself, or None.

        Some credentials name the subscription (Codex's OAuth tokens carry
        ``chatgpt_plan_type``), which is worth having without a poll: the plan
        decides which windows a tool even has, so a wrong default can show a
        limit bar the account is not metered on. Must not do I/O beyond
        reading that local file, and must not raise.
        """
        return None

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
