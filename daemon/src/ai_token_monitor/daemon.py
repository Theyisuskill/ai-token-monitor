"""Daemon wiring: adapters + watchers + store + D-Bus, one GLib main loop."""

from __future__ import annotations

import dataclasses
import logging
import signal
import time
from functools import partial
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import GLib  # noqa: E402

from . import adapters
from .config import Config
from .pricing import CostEngine
from .service import MonitorInterface, publish
from .store import Store, period_start
from .watcher import LogWatcher

log = logging.getLogger(__name__)


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
        self.interface = MonitorInterface(self)
        self._bus = None
        self._loop = None
        self._emit_id = 0

    # -- lifecycle -------------------------------------------------------------

    def run(self) -> None:
        started = time.monotonic()
        self.backfill()
        log.info("Backfill finished in %.1fs", time.monotonic() - started)

        for _adapter, watcher in self.watchers:
            watcher.start()
        self._bus = publish(self.interface)

        interval = int(self.config.get("rescan_interval_s", 300))
        if interval > 0:
            GLib.timeout_add_seconds(interval, self._periodic_rescan)

        self._loop = GLib.MainLoop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._quit)
        log.info("Entering main loop (%d adapters: %s)",
                 len(self.adapters), ", ".join(a.name for a in self.adapters))
        self._loop.run()

        # Teardown
        for _adapter, watcher in self.watchers:
            watcher.stop()
        if self._bus is not None:
            self._bus.disconnect()
        self.store.close()

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

    def _periodic_rescan(self) -> bool:
        # A GLib timeout callback that raises is treated as returning None,
        # which GLib interprets as SOURCE_REMOVE — the rescan would silently
        # stop firing forever. Never let an exception escape this callback.
        try:
            self.rescan()
        except Exception:
            log.exception("Periodic rescan failed; will retry on next tick")
        return GLib.SOURCE_CONTINUE

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

        records = [
            dataclasses.replace(record, cost_usd=self.engine.cost(record))
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

    def snapshot(self) -> dict:
        return {
            "five_hours": self.summary("5h"),
            "today": self.summary("today"),
            "week": self.summary("week"),
            "month": self.summary("month"),
            "budgets": self._resolved_budgets(),
            "updated": time.time(),
        }

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
        )

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
