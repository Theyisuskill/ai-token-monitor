"""D-Bus surface: io.github.theyisuskill.AITokenMonitor on the session bus.

The interface is deliberately string-typed (JSON payloads): it keeps the
introspection XML trivial for GJS consumers and lets the schema evolve
without breaking the bus contract.

NOTE: no ``from __future__ import annotations`` here — dasbus reads raw
``__annotations__`` to build the introspection XML, so the type hints on the
interface methods must be real objects, not strings.
"""

import json
import logging
from typing import TYPE_CHECKING

from dasbus.connection import SessionMessageBus
from dasbus.server.interface import dbus_interface, dbus_signal
from dasbus.typing import Str

if TYPE_CHECKING:
    from .daemon import Daemon

log = logging.getLogger(__name__)

BUS_NAME = "io.github.theyisuskill.AITokenMonitor"
OBJECT_PATH = "/io/github/theyisuskill/AITokenMonitor"
IFACE_NAME = "io.github.theyisuskill.AITokenMonitor1"

VALID_PERIODS = ("1h", "5h", "24h", "today", "week", "month", "all")


@dbus_interface(IFACE_NAME)
class MonitorInterface:
    """Methods return JSON strings; UsageUpdated pushes fresh snapshots."""

    def __init__(self, daemon: "Daemon"):
        self._daemon = daemon

    def GetSummary(self, period: Str) -> Str:
        if period not in VALID_PERIODS:
            return json.dumps({"error": f"unknown period {period!r}",
                               "valid": list(VALID_PERIODS)})
        return json.dumps(self._daemon.summary(period))

    def GetSnapshot(self) -> Str:
        return json.dumps(self._daemon.snapshot())

    def Refresh(self) -> Str:
        """Force a rescan of all logs and return the resulting snapshot."""
        self._daemon.rescan()
        return json.dumps(self._daemon.snapshot())

    def GetSettings(self) -> Str:
        """Current plans/budget_mode plus the available presets, for the
        Preferences window."""
        from . import config as config_mod

        cfg = self._daemon.config
        effective = {
            tool: cfg.plans.get(tool) or spec["default_plan"]
            for tool, spec in config_mod.PLAN_PRESETS.items()
        }
        presets = {
            tool: {"plans": list(spec["plans"]), "default": spec["default_plan"]}
            for tool, spec in config_mod.PLAN_PRESETS.items()
        }
        return json.dumps({"plans": effective,
                           "budget_mode": cfg.budget_mode,
                           "presets": presets})

    def SetSettings(self, settings: Str) -> Str:
        """Apply Preferences-window changes: validate, persist to ui.yaml,
        reload, push a fresh snapshot to every listener."""
        from . import config as config_mod

        try:
            changes = json.loads(settings)
            if not isinstance(changes, dict):
                raise ValueError("settings must be a JSON object")
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

        plans = changes.get("plans") or {}
        for tool, plan in plans.items():
            spec = config_mod.PLAN_PRESETS.get(tool)
            if spec is None:
                return json.dumps({"error": f"unknown tool {tool!r}"})
            if plan not in spec["plans"]:
                return json.dumps({"error": f"unknown plan {plan!r} for {tool}",
                                   "valid": list(spec["plans"])})
        mode = changes.get("budget_mode")
        if mode is not None and mode not in ("preset", "auto"):
            return json.dumps({"error": f"unknown budget_mode {mode!r}",
                               "valid": ["preset", "auto"]})

        config_mod.save_ui_overrides(changes)
        self._daemon.reload_settings()
        self.UsageUpdated.emit(json.dumps(self._daemon.snapshot()))
        return self.GetSettings()

    @dbus_signal
    def UsageUpdated(self, snapshot: Str):
        """Emitted (debounced) whenever new usage records are stored."""


def publish(interface: MonitorInterface) -> SessionMessageBus:
    bus = SessionMessageBus()
    bus.publish_object(OBJECT_PATH, interface)
    bus.register_service(BUS_NAME)
    log.info("Published %s at %s", BUS_NAME, OBJECT_PATH)
    return bus
