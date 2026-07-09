"""Recursive directory watching built on Gio.FileMonitor (inotify).

Gio.FileMonitor is not recursive, so we attach one monitor per directory and
extend the set when new directories appear. Events are debounced: bursts of
writes to the same file collapse into a single callback.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

log = logging.getLogger(__name__)

_INTERESTING = (
    Gio.FileMonitorEvent.CHANGED,
    Gio.FileMonitorEvent.CHANGES_DONE_HINT,
    Gio.FileMonitorEvent.CREATED,
)


class LogWatcher:
    def __init__(
        self,
        roots: list[Path],
        matches: Callable[[Path], bool],
        on_files: Callable[[set[Path]], None],
        settle_ms: int = 400,
        max_latency_ms: int = 3000,
    ):
        self._roots = [Path(r).expanduser() for r in roots]
        self._matches = matches
        self._on_files = on_files
        self._settle_ms = settle_ms
        # _schedule_flush resets its timer on every event, so a file that's
        # written to more often than settle_ms (e.g. a streaming response)
        # would otherwise starve ingestion indefinitely. This is a hard
        # ceiling on how long anything can sit in _pending.
        self._max_latency_ms = max_latency_ms
        self._monitors: dict[Path, Gio.FileMonitor] = {}
        self._pending: set[Path] = set()
        self._flush_id = 0
        self._deadline_id = 0

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        for root in self._roots:
            if root.is_dir():
                self._watch_tree(root)
            else:
                log.info("Root %s does not exist yet; periodic rescan will pick it up", root)

    def stop(self) -> None:
        for monitor in self._monitors.values():
            monitor.cancel()
        self._monitors.clear()
        if self._flush_id:
            GLib.source_remove(self._flush_id)
            self._flush_id = 0
        if self._deadline_id:
            GLib.source_remove(self._deadline_id)
            self._deadline_id = 0

    def rescan(self) -> None:
        """Attach monitors for directories created since the last scan."""
        for root in self._roots:
            if root.is_dir():
                self._watch_tree(root)

    def scan(self) -> Iterator[Path]:
        """Yield every currently existing file that the adapter matches."""
        for root in self._roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_file() and self._matches(path):
                    yield path

    # -- internals -------------------------------------------------------------

    def _watch_tree(self, directory: Path) -> None:
        self._watch_dir(directory)
        try:
            for child in directory.rglob("*"):
                if child.is_dir():
                    self._watch_dir(child)
        except OSError as exc:
            log.warning("Cannot walk %s: %s", directory, exc)

    def _watch_dir(self, directory: Path) -> None:
        if directory in self._monitors:
            return
        gfile = Gio.File.new_for_path(str(directory))
        try:
            monitor = gfile.monitor_directory(Gio.FileMonitorFlags.NONE, None)
        except GLib.Error as exc:
            log.warning("Cannot monitor %s: %s", directory, exc.message)
            return
        monitor.connect("changed", self._on_event)
        self._monitors[directory] = monitor
        log.debug("Watching %s", directory)

    def _on_event(self, _monitor, gfile, _other, event) -> None:
        if event not in _INTERESTING:
            return
        raw = gfile.get_path()
        if not raw:
            return
        path = Path(raw)

        if path.is_dir():
            # New subdirectory: watch it, and sweep files that landed in it
            # before our monitor attached.
            if event == Gio.FileMonitorEvent.CREATED:
                self._watch_tree(path)
                for child in path.rglob("*"):
                    if child.is_file() and self._matches(child):
                        self._pending.add(child)
                if self._pending:
                    self._schedule_flush()
            return

        if not self._matches(path):
            return
        self._pending.add(path)
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._flush_id:
            GLib.source_remove(self._flush_id)
        self._flush_id = GLib.timeout_add(self._settle_ms, self._flush)
        # Non-resetting deadline: guarantees a flush at most max_latency_ms
        # after _pending first became non-empty, even under sustained writes
        # that keep pushing the settle timer back.
        if not self._deadline_id:
            self._deadline_id = GLib.timeout_add(self._max_latency_ms, self._flush)

    def _flush(self) -> bool:
        if self._flush_id:
            GLib.source_remove(self._flush_id)
            self._flush_id = 0
        if self._deadline_id:
            GLib.source_remove(self._deadline_id)
            self._deadline_id = 0
        batch, self._pending = self._pending, set()
        if batch:
            self._on_files(batch)
        return GLib.SOURCE_REMOVE
