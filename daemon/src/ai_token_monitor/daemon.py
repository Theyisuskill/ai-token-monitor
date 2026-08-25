"""Daemon wiring: adapters + watchers + store + D-Bus, one GLib main loop."""

from __future__ import annotations

import dataclasses
import logging
import signal
import threading
import time
from functools import partial
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import GLib  # noqa: E402

from . import adapters, live
from .config import Config
from .live import projection
from .pricing import CostEngine
from .service import MonitorInterface, publish
from .store import Store, period_start
from .watcher import LogWatcher

log = logging.getLogger(__name__)

#: The providers' rate-limit windows, both anchored to first use.
SESSION_SPAN = 5.0 * 3600.0
WEEK_SPAN = 7.0 * 24.0 * 3600.0

#: Tools that meter separate model families as independent quota pools.
#: Antigravity's own "Models & Quota" screen shows GEMINI MODELS and
#: CLAUDE AND GPT MODELS with their own 5h/weekly limits each; splitting on
#: the normalized model name mirrors that structure.
QUOTA_GROUPS = {
    "gemini_cli": (
        {"key": "gemini", "label": "Gemini",
         "model_like": "gemini%"},
        {"key": "claude_gpt", "label": "Claude & GPT",
         "model_not_like": "gemini%"},
    ),
}

#: Tools whose weekly reset can be re-anchored server-side (Google has reset
#: Antigravity quotas globally several times); their countdown is marked
#: approximate in the UI.
APPROX_WEEKLY = frozenset({"gemini_cli"})


class Daemon:
    def __init__(self, config: Config):
        self.config = config
        self.store = Store(config.database)
        self.engine = CostEngine(config.pricing)
        self.adapters = adapters.create_enabled(config.adapters)
        if not self.adapters:
            log.warning("No adapters enabled — the daemon will serve empty data")
        self.watchers = [
            (adapter, LogWatcher(adapter.roots(), adapter.matches,
                                 partial(self._ingest, adapter)))
            for adapter in self.adapters
        ]
        # Live-limit pollers (opt-in, network): latest normalized result per
        # tool, plus a per-poller "busy" guard so polls never overlap.
        self.pollers = live.create_enabled(config.live_limits)
        self._live: dict[str, dict] = {}
        self._live_ok: dict[str, dict] = {}
        #: Window keys ("five_hour"/"weekly") each poller has ever reported.
        self._live_windows: dict[str, set[str]] = {}
        #: Plan tier read off a credential, no poll needed (see _window_support).
        self._offline_tiers: dict[str, str] | None = None
        self._live_busy: dict[str, bool] = {}
        self._live_status: dict[str, str] = {}
        # (tool, window-key) -> [(ts, used%), ...] feeding the burn-rate
        # projection; reset whenever the provider's reset time moves.
        self._live_samples: dict[tuple[str, str], list] = {}
        self._live_resets: dict[tuple[str, str], float] = {}
        self._plan_warned: dict[str, object] = {}
        self.interface = MonitorInterface(self)
        self._bus = None
        self._loop = None
        self._emit_id = 0

    # -- lifecycle -------------------------------------------------------------

    def run(self) -> None:
        started = time.monotonic()
        self._prune_old()
        self.backfill()
        log.info("Backfill finished in %.1fs", time.monotonic() - started)

        for _adapter, watcher in self.watchers:
            watcher.start()
        self._bus = publish(self.interface)

        interval = int(self.config.get("rescan_interval_s", 300))
        if interval > 0:
            GLib.timeout_add_seconds(interval, self._periodic_rescan)

        for poller in self.pollers:
            log.info("Live poller %s (tool=%s) every %ds",
                     poller.name, poller.tool, poller.interval)
            GLib.timeout_add_seconds(2, self._poll_once, poller)
            GLib.timeout_add_seconds(poller.interval, self._poll_tick, poller)

        self._loop = GLib.MainLoop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._quit)
        log.info("Entering main loop (%d adapters: %s)",
                 len(self.adapters), ", ".join(a.name for a in self.adapters))
        self._loop.run()

        # Teardown. A clean stop must exit 0: with Restart=on-failure a non-zero
        # exit here gets bounced (and can trip systemd's start limit). dasbus's
        # disconnect() does a synchronous ReleaseName round-trip that raises
        # "the connection is closed" when the session bus is already going away
        # (SIGTERM during logout, or a fast restart), so each step is guarded.
        for _adapter, watcher in self.watchers:
            try:
                watcher.stop()
            except Exception:
                log.exception("watcher stop failed during shutdown")
        if self._bus is not None:
            try:
                self._bus.disconnect()
            except Exception:
                log.warning("bus disconnect during shutdown failed "
                            "(connection already closing)")
        try:
            self.store.close()
        except Exception:
            log.exception("store close failed during shutdown")

    def _quit(self) -> bool:
        log.info("Shutting down")
        if self._loop is not None:
            self._loop.quit()
        return GLib.SOURCE_REMOVE

    # -- ingestion ---------------------------------------------------------------

    def backfill(self) -> int:
        """Process everything on disk from the stored offsets. Cheap when
        nothing changed; on first run it imports the full history."""
        total = 0
        for adapter, watcher in self.watchers:
            try:
                paths = set(watcher.scan())
            except OSError as exc:
                log.warning("%s: scan failed: %s", adapter.name, exc)
                continue
            total += self._ingest(adapter, paths)
        return total

    def rescan(self) -> None:
        for _adapter, watcher in self.watchers:
            watcher.rescan()
        self.backfill()

    def reparse(self, tool: str) -> int:
        """Drop a tool's usage and re-ingest its logs from scratch.

        One-shot migration for parser/pricing fixes (e.g. the Antigravity
        model extraction): dedup keys don't include model or cost, so a plain
        backfill would INSERT OR IGNORE the old rows forever.
        """
        adapter = next((a for a in self.adapters if a.name == tool), None)
        if adapter is None:
            raise ValueError(
                f"unknown or disabled tool {tool!r} "
                f"(enabled: {', '.join(a.name for a in self.adapters)})")
        dropped = self.store.delete_tool(tool)
        for root in adapter.roots():
            self.store.reset_file_state_under(str(root))
        log.info("reparse %s: dropped %d rows, re-ingesting", tool, dropped)
        return self.backfill()

    def _periodic_rescan(self) -> bool:
        # A GLib timeout callback that raises is treated as returning None,
        # which GLib interprets as SOURCE_REMOVE — the rescan would silently
        # stop firing forever. Never let an exception escape this callback.
        try:
            self._prune_old()
            self.rescan()
        except Exception:
            log.exception("Periodic rescan failed; will retry on next tick")
        return GLib.SOURCE_CONTINUE

    # -- live limit pollers ----------------------------------------------------

    def _poll_tick(self, poller) -> bool:
        self._poll_once(poller)
        return GLib.SOURCE_CONTINUE

    def _poll_once(self, poller) -> bool:
        """Kick a poll on a worker thread unless one is already in flight."""
        if not self._live_busy.get(poller.name):
            self._live_busy[poller.name] = True
            threading.Thread(target=self._poll_worker, args=(poller,),
                             daemon=True).start()
        return GLib.SOURCE_REMOVE

    def _poll_worker(self, poller) -> None:
        # Off the main loop: no store/GLib access here. poll() never raises,
        # but guard anyway so a bug can't wedge the busy flag.
        try:
            result = poller.poll()
        except Exception:
            log.exception("live poller %s crashed", poller.name)
            result = {"status": "error", "fetched_at": time.time()}
        GLib.idle_add(self._apply_live, poller.name, poller.tool, result)

    def _apply_live(self, name: str, tool: str, result: dict) -> bool:
        self._live_busy[name] = False
        self._live[tool] = result
        # Journal status TRANSITIONS only: a poller failing for hours must
        # leave a trace, but a steady state shouldn't log a line per poll.
        status = result.get("status") or "error"
        if status == "ok":
            self._live_ok[tool] = result
            self._note_samples(tool, result)
            self._warn_plan_mismatch(tool, result.get("plan_tier"))
            # Which windows the provider actually meters, accumulated across
            # polls: the union, so one response that happens to omit a window
            # can't make its bar disappear, while a plan that genuinely has no
            # 5h window (Codex Go) never gets one invented for it.
            reported = set(result.get("windows") or {})
            if reported:
                self._live_windows.setdefault(tool, set()).update(reported)
        if status != self._live_status.get(name):
            self._live_status[name] = status
            if status == "ok" or status in live.QUIET_STATUSES:
                # "not running" is a state, not a fault — INFO, so a WARNING in
                # this log always means something actually broke.
                log.info("live poller %s: %s", name, status)
            else:
                log.warning("live poller %s: %s", name, status)
        self._schedule_emit()
        return GLib.SOURCE_REMOVE

    def _note_samples(self, tool: str, result: dict) -> None:
        """Accumulate (ts, used%) per window for the burn-rate projection,
        restarting whenever the provider's reset time moves (new window)."""
        ts = float(result.get("fetched_at") or time.time())
        for key, w in (result.get("windows") or {}).items():
            pct = w.get("used_percent")
            if not isinstance(pct, (int, float)):
                continue
            k = (tool, key)
            resets = w.get("resets_at")
            prev = self._live_resets.get(k)
            if resets is not None:
                if prev is not None and abs(float(resets) - prev) > 60.0:
                    self._live_samples[k] = []
                self._live_resets[k] = float(resets)
            samples = self._live_samples.setdefault(k, [])
            samples.append((ts, float(pct)))
            # The projector only reads the last hour; keep the list bounded.
            del samples[:-64]

    def _warn_plan_mismatch(self, tool: str, tier) -> None:
        """One WARNING per tier value when the credential's reported tier
        disagrees with an explicitly configured plan (the config wins)."""
        from . import config as config_mod

        spec = config_mod.PLAN_PRESETS.get(tool)
        configured = (self.config.plans or {}).get(tool)
        if not spec or not configured or tier == self._plan_warned.get(tool):
            return
        self._plan_warned[tool] = tier
        detected = config_mod.plan_from_tier(tier, spec["plans"])
        if detected and detected != configured:
            log.warning(
                "%s credential reports tier %r (plan %s) but config has "
                "plans.%s=%s; using the config — remove plans.%s to follow "
                "the credential", tool, tier, detected, tool, configured, tool)

    def _attach_live(self, snap: dict) -> None:
        """Merge poller results into the snapshot: a per-tool `real` block on
        the 5h/weekly window entries, plus a top-level `live` status map so the
        UI can show which bars are provider-real vs estimated, and why."""
        now = time.time()
        status = {}
        for tool, res in self._live.items():
            # A transient poll failure keeps serving the last OK data (see
            # effective_live) so the bars don't flap between the provider's
            # real % and the dollar estimate every time a poll hiccups.
            data, stale = live.effective_live(res, self._live_ok.get(tool), now)
            status[tool] = {"status": res.get("status"),
                            "fetched_at": res.get("fetched_at"),
                            "plan_tier": (data or res).get("plan_tier")}
            # `quiet` = "nothing to report", not "something is wrong": the UI
            # skips its amber note so the warning keeps meaning something.
            if res.get("status") in live.QUIET_STATUSES:
                status[tool]["quiet"] = True
            if stale:
                status[tool]["stale"] = True
                status[tool]["data_fetched_at"] = (data or {}).get("fetched_at")
            if data is None:
                continue
            windows = data.get("windows") or {}
            for period, key in (("five_hours", "five_hour"), ("week", "weekly")):
                w = windows.get(key)
                if not w:
                    continue
                entry = next((t for t in snap.get(period, {}).get("tools", [])
                              if t.get("tool") == tool), None)
                if entry is not None:
                    real = {"used_percent": w.get("used_percent"),
                            "resets_at": w.get("resets_at"),
                            "source": "provider"}
                    depletes = projection.project_depletion(
                        self._live_samples.get((tool, key), []), now,
                        w.get("resets_at"))
                    if depletes is not None:
                        real["depletes_at"] = depletes
                    entry["real"] = real
            scoped = data.get("scoped")
            if scoped:
                entry = next((t for t in snap.get("week", {}).get("tools", [])
                              if t.get("tool") == tool), None)
                if entry is not None:
                    entry["real_scoped"] = scoped
                # Grouped tools (agy's Gemini vs Claude & GPT pools): scoped
                # entries carrying a pool key drive the matching per-pool
                # sub-bars, same preference the tool-level `real` gets.
                for period, kind in (("five_hours", "session"),
                                     ("week", "weekly")):
                    entry = next((t for t in snap.get(period, {}).get("tools", [])
                                  if t.get("tool") == tool), None)
                    for g in (entry or {}).get("groups") or []:
                        m = next((s for s in scoped
                                  if s.get("pool") == g.get("key")
                                  and s.get("group") == kind), None)
                        if m:
                            g["real"] = {"used_percent": m.get("used_percent"),
                                         "resets_at": m.get("resets_at"),
                                         "source": "provider"}
            if data.get("extra_usage"):
                status[tool]["extra_usage"] = data["extra_usage"]
        snap["live"] = status

    def _prune_old(self) -> None:
        """Apply the optional retention window (0 = keep everything)."""
        days = float(self.config.get("retention_days", 0) or 0)
        if days <= 0:
            return
        removed = self.store.prune(time.time() - days * 86400.0)
        if removed:
            log.info("Pruned %d usage records older than %g days",
                     removed, days)

    def _ingest(self, adapter, paths: set[Path]) -> int:
        inserted = 0
        for path in sorted(paths):
            try:
                inserted += self._tail(adapter, path)
            except OSError as exc:
                log.warning("%s: cannot read %s: %s", adapter.name, path, exc)
            except Exception:
                # A malformed log line, a bad pricing rule, or a transient
                # sqlite error must not take down the whole daemon (or, worse,
                # crash-loop it: the offending file gets reprocessed on every
                # restart since its offset was never advanced).
                log.exception("%s: failed to ingest %s", adapter.name, path)
        if inserted:
            log.info("%s: +%d usage records", adapter.name, inserted)
            self._schedule_emit()
        return inserted

    def _tail(self, adapter, path: Path) -> int:
        """Read new complete lines since the stored offset and ingest them."""
        try:
            st = path.stat()
        except FileNotFoundError:
            return 0

        key = str(path)
        prev_inode, offset = self.store.get_file_state(key)
        if prev_inode != st.st_ino or st.st_size < offset:
            offset = 0  # rotated, replaced or truncated: reparse (dedup protects us)
        if st.st_size <= offset:
            return 0

        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()

        cut = data.rfind(b"\n")
        if cut < 0:
            return 0  # no complete line yet; keep the offset where it was
        lines = data[:cut].decode("utf-8", errors="replace").splitlines()

        # An adapter that already knows the authoritative cost (OpenCode prices
        # each turn against live provider rates and stores it) keeps it; every
        # other adapter leaves cost_usd at 0.0 and we price it from the model.
        records = [
            dataclasses.replace(
                record, cost_usd=record.cost_usd or self.engine.cost(record))
            for record in adapter.parse(path, lines)
        ]
        inserted = self.store.add(records)
        self.store.set_file_state(key, st.st_ino, offset + cut + 1)
        return inserted

    # -- queries / notifications ---------------------------------------------------

    def summary(self, period: str) -> dict:
        result = self.store.summary(period_start(period))
        result["period"] = period
        return result

    def history(self, period: str = "month") -> dict:
        """Backwards-looking view — see Store.history().

        Deliberately NOT part of snapshot(): the snapshot is re-emitted on
        every debounced usage update, and nobody needs 90 days of series
        recomputed to redraw a progress bar. The UI asks for this when the
        History tab is opened.
        """
        return self.store.history(period)

    _EMPTY_ENTRY = {"input_tokens": 0, "output_tokens": 0,
                    "cache_read_tokens": 0, "cache_write_tokens": 0,
                    "total_tokens": 0, "cost_usd": 0.0, "requests": 0}

    def _window_entry(self, tool: str, span: float,
                      lookback_days: int | None, **family) -> dict:
        """Anchored-window aggregate for one tool (optionally one of its
        model-family quota pools)."""
        anchor = self.store.session_anchor(tool, span, lookback_days, **family)
        if anchor is None:
            return {**self._EMPTY_ENTRY, "session_active": False}
        result = self.store.summary(anchor, tool=tool, **family)
        entry = dict(result["tools"][0]) if result["tools"] \
            else dict(self._EMPTY_ENTRY)
        entry.pop("tool", None)
        entry["session_active"] = True
        entry["session_started"] = anchor
        entry["resets_at"] = anchor + span
        return entry

    def _anchored_window(self, span: float, period: str,
                         lookback_days: int | None) -> dict:
        """One entry per tool, each measured from that tool's real window
        anchor (first use), with the exact reset time. Providers anchor both
        the 5h and the weekly window to first use, so this — unlike the
        trailing GetSummary periods — matches when they actually reset.

        Tools in QUOTA_GROUPS additionally get per-pool sub-entries (each
        pool has its own anchor chain and reset), included once the user has
        touched at least two pools."""
        tools = []
        totals = dict(self._EMPTY_ENTRY)
        for tool in self.store.tools_seen():
            entry = {"tool": tool,
                     **self._window_entry(tool, span, lookback_days)}
            approx = period == "week" and tool in APPROX_WEEKLY
            if approx:
                entry["approx"] = True
            groups = []
            for spec in QUOTA_GROUPS.get(tool, ()):
                family = {k: spec[k] for k in ("model_like", "model_not_like")
                          if k in spec}
                ever = self.store.summary(0.0, tool=tool, **family)
                if ever["totals"]["requests"] == 0:
                    continue  # pool never touched: don't render it
                sub = self._window_entry(tool, span, lookback_days, **family)
                sub["key"] = spec["key"]
                sub["label"] = spec["label"]
                if approx:
                    sub["approx"] = True
                groups.append(sub)
            if len(groups) >= 2:
                entry["groups"] = groups
            tools.append(entry)
            for key in totals:
                totals[key] += entry[key]
        totals["cost_usd"] = round(totals["cost_usd"], 4)
        return {"tools": tools, "totals": totals,
                "period": period, "anchored": True}

    def snapshot(self) -> dict:
        # The weekly anchor chain must replay from the true first use, so no
        # lookback cap; the 5h chain only needs recent history.
        week = self._anchored_window(WEEK_SPAN, "week", None)
        # Per-tool model breakdown rides on the week entries (the window the
        # popup's "By model" submenu shows).
        models = self.store.models_summary(period_start("week"))
        for entry in week["tools"]:
            entry["models"] = models.get(entry["tool"], [])
        snap = {
            "five_hours": self._anchored_window(SESSION_SPAN, "5h", 14),
            # Short rolling windows so the UI can estimate burn rate.
            "hour": self.summary("1h"),
            "day": self.summary("24h"),
            "today": self.summary("today"),
            "week": week,
            "month": self.summary("month"),
            # 14 days: the sparkline renders the last 7, the previous 7 feed
            # the Summary's week-over-week spend delta.
            "daily": self.store.daily_series(time.time() - 14 * 86400.0),
            "budgets": self._resolved_budgets(),
            # Per-tool {"5h": bool, "weekly": bool} — not every plan meters
            # both (Codex Go: weekly allowance, no 5h session window).
            "windows": self._window_support(),
            "plans": self._effective_plans(),
            "tools": self.store.tools_seen(),
            "ui": self.config.ui,
            "updated": time.time(),
        }
        # Overlay the provider's real 5h/weekly % + reset where a live poller
        # has data (prefer real over the dollar-scaled estimate in the UI).
        self._attach_live(snap)
        return snap

    def reload_settings(self) -> None:
        """Re-read config.yaml + ui.yaml (after a SetSettings write). Only the
        UI-managed keys can change this way; database path, adapters and
        pricing keep their process-lifetime values."""
        from . import config as config_mod

        self.config = config_mod.load(self.config.path)
        self._offline_tiers = None  # credential paths may have changed

    def _resolved_budgets(self) -> dict:
        """Plan-aware limits served to the UI. Presets are resolved from the
        user's declared plan; 'auto' mode calibrates from observed peaks."""
        from . import config as config_mod

        mode = self.config.budget_mode
        peaks = None
        if mode == "auto":
            peaks = {
                tool: {
                    "5h": self.store.peak_window(tool, 5.0 * 3600.0),
                    "weekly": self.store.peak_window(tool, 7.0 * 24.0 * 3600.0),
                }
                for tool in config_mod.PLAN_PRESETS
            }
        return config_mod.resolve_budgets(
            plans=self.config.plans,
            mode=mode,
            overrides=self.config.budgets,
            peaks=peaks,
            detected=self._detected_plans(),
        )

    def _detected_plans(self) -> dict:
        """Plan each tool's live poller inferred from its credential tier."""
        from . import config as config_mod

        offline = self._credential_tiers()
        detected = {}
        for tool in config_mod.PLAN_PRESETS:
            spec = config_mod.PLAN_PRESETS[tool]
            # Last OK result: a transient poll failure carries no plan_tier
            # and must not flip the plan back to the default preset. Falling
            # back to the tier named in the credential keeps the plan right
            # for a poller that is disabled or has never reached the provider.
            src = self._live_ok.get(tool) or self._live.get(tool) or {}
            tier = src.get("plan_tier") or offline.get(tool)
            plan = config_mod.plan_from_tier(tier, spec["plans"])
            if plan:
                detected[tool] = plan
        return detected

    def _credential_tiers(self) -> dict:
        """Plan tier each poller can read straight off its credential file.

        Every registered poller is asked, enabled or not — this is a local
        read, not a poll, and knowing the plan is what tells the UI which
        windows the account even has. Cached for the process: a subscription
        tier changes far less often than a snapshot is built.
        """
        if self._offline_tiers is None:
            self._offline_tiers = live.credential_tiers(self.config.live_limits)
        return self._offline_tiers

    def _window_support(self) -> dict:
        """Which rate-limit windows each tool actually meters.

        Starts from the plan preset — a plan whose preset value is None does
        not have that window (Codex on the Go tier is weekly-only) — and lets
        a live poller correct it: what the provider reports about its own
        windows beats any table shipped here. The extension drops the bars for
        unsupported windows instead of scaling usage against a limit that does
        not exist.
        """
        from . import config as config_mod

        support = config_mod.resolve_windows(
            plans=self.config.plans,
            detected=self._detected_plans(),
            overrides=self.config.budgets,
        )
        for tool, reported in self._live_windows.items():
            support[tool] = {w: (live_key in reported) for w, live_key
                             in (("5h", "five_hour"), ("weekly", "weekly"))}
        return support

    def _effective_plans(self) -> dict:
        """Plan actually driving each tool's budgets, for the UI's plan badge:
        an explicit ``plans.<tool>`` wins, else what the credential reported.
        (Mirrors resolve_budgets' precedence so the badge can't disagree with
        the bars.)"""
        detected = self._detected_plans()
        plans = dict(detected)
        for tool, plan in (self.config.plans or {}).items():
            if plan:
                plans[tool] = plan
        return plans

    def _schedule_emit(self) -> None:
        if self._emit_id:
            GLib.source_remove(self._emit_id)
        debounce = int(self.config.get("signal_debounce_ms", 750))
        self._emit_id = GLib.timeout_add(debounce, self._emit)

    def _emit(self) -> bool:
        self._emit_id = 0
        import json

        self.interface.UsageUpdated.emit(json.dumps(self.snapshot()))
        return GLib.SOURCE_REMOVE
