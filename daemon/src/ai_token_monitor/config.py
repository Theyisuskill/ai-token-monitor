"""Configuration: ~/.config/ai-token-monitor/config.yaml over built-in defaults."""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

CONFIG_DIR = (
    Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    / "ai-token-monitor"
)
CONFIG_PATH = CONFIG_DIR / "config.yaml"
# Machine-written overrides (the extension's Preferences window). Merged on
# top of config.yaml, so UI choices win over hand-edited values for the few
# keys the UI manages (plans, budget_mode, budgets).
UI_PATH = CONFIG_DIR / "ui.yaml"
UI_KEYS = ("plans", "budget_mode", "budgets", "ui")

# extension display preferences (the "ui" config key)
PANEL_MODES = ("percent", "icon", "today")
# Popup layout: "switcher" = a provider tab bar at the top with one detailed
# card at a time (compact, CodexBar-style); "stacked" = every provider's full
# section listed at once (the original layout).
LAYOUT_MODES = ("switcher", "stacked")
DEFAULT_UI = {"panel": "percent", "alerts": True, "layout": "switcher"}
DATA_DIR = (
    Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    / "ai-token-monitor"
)

# Prices in USD per 1M tokens. First fnmatch() hit wins, so order rules from
# most to least specific. Values current as of 2026-07; adjust in config.yaml.
DEFAULT_PRICING: list[dict[str, Any]] = [
    {"match": "claude-fable-*", "input": 10.0, "output": 50.0,
     "cache_read": 1.0, "cache_write": 12.5},
    {"match": "claude-opus-4-[01]*", "input": 15.0, "output": 75.0,
     "cache_read": 1.5, "cache_write": 18.75},
    {"match": "claude-opus-*", "input": 5.0, "output": 25.0,
     "cache_read": 0.5, "cache_write": 6.25},
    {"match": "claude-sonnet-*", "input": 3.0, "output": 15.0,
     "cache_read": 0.3, "cache_write": 3.75},
    {"match": "claude-haiku-*", "input": 1.0, "output": 5.0,
     "cache_read": 0.1, "cache_write": 1.25},
    {"match": "gemini-*pro*", "input": 1.25, "output": 10.0,
     "cache_read": 0.31, "cache_write": 0.0},
    {"match": "gemini-*", "input": 0.30, "output": 2.50,
     "cache_read": 0.075, "cache_write": 0.0},
    {"match": "gpt-*", "input": 1.25, "output": 10.0,
     "cache_read": 0.125, "cache_write": 0.0},
    {"match": "*", "input": 0.0, "output": 0.0,
     "cache_read": 0.0, "cache_write": 0.0},
]

# Per-tool subscription presets. The providers do NOT publish a dollar figure
# for their rolling 5h / weekly limits (Claude meters "prompts" and "active
# compute hours", Antigravity meters "work done"), so these are API-equivalent
# USD ceilings used purely to scale the UI progress bars. They are deliberately
# NOT hardcoded to one tier: the user names their plan in config and the daemon
# resolves the budgets, so the tool adapts to whatever plan the user actually
# has. Anthropic's Max tiers are literally 5x / 20x the Pro limits, which is the
# ratio encoded here; anchor values are approximate and can be overridden.
PLAN_PRESETS: dict[str, dict[str, Any]] = {
    "claude_code": {
        "prefix": "claude",
        "default_plan": "pro",
        "plans": {
            "pro":     {"5h": 15.0,  "weekly": 75.0},
            "max_5x":  {"5h": 75.0,  "weekly": 375.0},
            "max_20x": {"5h": 300.0, "weekly": 1500.0},
        },
    },
    "gemini_cli": {  # Antigravity ("agy") + legacy Gemini CLI
        "prefix": "gemini",
        "default_plan": "free",
        "plans": {
            "free":  {"5h": 3.0,  "weekly": 8.0},
            "pro":   {"5h": 10.0, "weekly": 30.0},
            "ultra": {"5h": 40.0, "weekly": 150.0},
        },
    },
    "codex": {  # OpenAI Codex CLI (ChatGPT subscription tiers)
        "prefix": "codex",
        "default_plan": "plus",
        "plans": {
            "plus": {"5h": 10.0, "weekly": 40.0},
            "pro":  {"5h": 60.0, "weekly": 300.0},
        },
    },
}

# In 'auto' budget mode, each denominator tracks the user's own observed peak
# rolling usage times this headroom — a fully self-calibrating limit that needs
# no plan knowledge. The plan preset acts as a floor so a fresh install (no
# history) still shows a sensible bar.
AUTO_HEADROOM = 1.15

DEFAULTS: dict[str, Any] = {
    "database": str(DATA_DIR / "usage.db"),
    "log_level": "info",
    # Coalesce bursts of file events before emitting the D-Bus signal.
    "signal_debounce_ms": 750,
    # Periodic full rescan: catches roots created after startup and any
    # inotify event that slipped through. Offsets make it near-free.
    "rescan_interval_s": 300,
    # Days of usage history to keep (0 = forever). Pruned on start and on
    # every periodic rescan.
    "retention_days": 0,
    "adapters": {
        "claude_code": {"enabled": True, "root": "~/.claude/projects"},
        "gemini_cli": {"enabled": True, "root": "~/.gemini/tmp"},
        "codex": {"enabled": True, "root": "~/.codex/sessions"},
        # OpenCode keeps its own SQLite db; leave "root" unset to use the
        # default ~/.local/share/opencode (honours $XDG_DATA_HOME).
        "opencode": {"enabled": True},
    },
    "pricing": DEFAULT_PRICING,
    # Subscription tier per tool (keys are adapter names). Resolved against
    # PLAN_PRESETS; anything unset falls back to that tool's default_plan.
    "plans": {},
    # Live limit pollers: read the provider's OWN 5h/weekly percentage + reset
    # from a credential already on disk, instead of the dollar-scaled estimate.
    # This makes an outbound request (a deliberate opt-in relaxation of the
    # local-only default), so each poller is enabled explicitly. The Claude
    # poller is read-only: it never rewrites ~/.claude/.credentials.json.
    "live_limits": {
        # Verified read-only OAuth poller — on by default.
        "claude_code": {"enabled": True, "interval_s": 90},
        # Antigravity ("agy", tool=gemini_cli): local-loopback quota first,
        # Google-OAuth fallback. Off by default — it probes local server ports
        # and only yields data while Antigravity is running; enable to try.
        "antigravity": {"enabled": False, "interval_s": 120},
        # Codex/ChatGPT (~/.codex/auth.json). Off by default — portable but
        # only useful if you use the Codex CLI.
        "codex": {"enabled": False, "interval_s": 120},
    },
    # "preset": use the plan presets. "auto": calibrate from observed peaks.
    "budget_mode": "preset",
    "budgets": {
        "daily": 0.0,
        "weekly": 0.0,
        "monthly": 0.0,
    },
    "ui": dict(DEFAULT_UI),
}


def validate_ui(ui: Any) -> str | None:
    """Error message for an invalid "ui" settings value, or None if valid.

    Pure so the D-Bus layer (which needs dasbus) isn't required to test it.
    """
    if not isinstance(ui, dict):
        return "ui must be an object"
    panel = ui.get("panel")
    if panel is not None and panel not in PANEL_MODES:
        return f"unknown panel mode {panel!r} (valid: {', '.join(PANEL_MODES)})"
    layout = ui.get("layout")
    if layout is not None and layout not in LAYOUT_MODES:
        return f"unknown layout {layout!r} (valid: {', '.join(LAYOUT_MODES)})"
    alerts = ui.get("alerts")
    if alerts is not None and not isinstance(alerts, bool):
        return "alerts must be true or false"
    unknown = set(ui) - {"panel", "alerts", "layout"}
    if unknown:
        return f"unknown ui keys: {', '.join(sorted(unknown))}"
    return None


def resolve_budgets(
    plans: dict[str, str] | None,
    mode: str,
    overrides: dict[str, float] | None,
    peaks: dict[str, dict[str, float]] | None = None,
) -> dict[str, float]:
    """Concrete per-tool budget dict the UI consumes (``claude_5h`` etc.).

    Precedence per key, highest first:
      1. an explicit ``budgets:`` override in config,
      2. the user's observed peak * headroom (only when ``mode == 'auto'``),
      3. the plan preset for the tool (default_plan if the plan is unset),
    Any non-tool keys already in ``overrides`` (daily/weekly/monthly) pass
    through untouched.
    """
    plans = plans or {}
    overrides = overrides or {}
    peaks = peaks or {}
    resolved: dict[str, float] = dict(overrides)
    for tool, spec in PLAN_PRESETS.items():
        prefix = spec["prefix"]
        plan = plans.get(tool) or spec["default_plan"]
        base = spec["plans"].get(plan) or spec["plans"][spec["default_plan"]]
        for window in ("5h", "weekly"):
            key = f"{prefix}_{window}"
            if key in overrides:  # explicit value wins outright
                continue
            value = float(base[window])
            if mode == "auto":
                peak = (peaks.get(tool) or {}).get(window, 0.0)
                value = max(value, peak * AUTO_HEADROOM)
            resolved[key] = round(value, 2)
    return resolved


class Config:
    def __init__(self, data: dict[str, Any], path: Path | None = None):
        self._data = data
        #: Source config.yaml path, so the daemon can reload after SetSettings.
        self.path = path

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    @property
    def database(self) -> Path:
        return Path(self._data["database"]).expanduser()

    @property
    def adapters(self) -> dict[str, dict[str, Any]]:
        # A user config with a bare `adapters:` key yaml-parses to None; keep
        # the return type honest so callers don't need a None check.
        return self._data.get("adapters") or {}

    @property
    def pricing(self) -> list[dict[str, Any]]:
        return self._data["pricing"]

    @property
    def budgets(self) -> dict[str, float]:
        return self._data.get("budgets") or {}

    @property
    def plans(self) -> dict[str, str]:
        return self._data.get("plans") or {}

    @property
    def live_limits(self) -> dict[str, dict[str, Any]]:
        value = self._data.get("live_limits")
        return value if isinstance(value, dict) else {}

    @property
    def budget_mode(self) -> str:
        mode = str(self._data.get("budget_mode", "preset")).lower()
        return mode if mode in ("preset", "auto") else "preset"

    @property
    def ui(self) -> dict[str, Any]:
        merged = dict(DEFAULT_UI)
        value = self._data.get("ui")
        if isinstance(value, dict) and validate_ui(value) is None:
            merged.update(value)
        return merged


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load(path: Path | str | None = None) -> Config:
    path = Path(path).expanduser() if path else CONFIG_PATH
    data = DEFAULTS
    if path.is_file():
        try:
            user = yaml.safe_load(path.read_text()) or {}
            if not isinstance(user, dict):
                raise TypeError(f"top level of {path} must be a mapping")
            data = _deep_merge(DEFAULTS, user)
        except (yaml.YAMLError, TypeError, OSError) as exc:
            log.error("Ignoring invalid config %s: %s", path, exc)
    else:
        log.info("No config at %s, using defaults", path)
    if UI_PATH.is_file():
        try:
            ui = yaml.safe_load(UI_PATH.read_text()) or {}
            if isinstance(ui, dict):
                data = _deep_merge(data, {k: v for k, v in ui.items()
                                          if k in UI_KEYS})
        except (yaml.YAMLError, OSError) as exc:
            log.error("Ignoring invalid UI overrides %s: %s", UI_PATH, exc)
    return Config(data, path=path)


def save_ui_overrides(changes: dict[str, Any]) -> None:
    """Merge Preferences-window changes into ui.yaml (UI-managed keys only)."""
    current: dict[str, Any] = {}
    if UI_PATH.is_file():
        try:
            loaded = yaml.safe_load(UI_PATH.read_text())
            if isinstance(loaded, dict):
                current = loaded
        except (yaml.YAMLError, OSError):
            pass  # unreadable: rewrite from scratch
    merged = _deep_merge(current, {k: v for k, v in changes.items()
                                   if k in UI_KEYS})
    UI_PATH.parent.mkdir(parents=True, exist_ok=True)
    UI_PATH.write_text(
        "# Written by the AI Token Monitor Preferences window.\n"
        "# These keys override config.yaml; edit that file for everything else.\n"
        + yaml.safe_dump(merged, sort_keys=True))
