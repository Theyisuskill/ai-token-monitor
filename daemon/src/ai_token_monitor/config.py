"""Configuration: ~/.config/ai-token-monitor/config.yaml over built-in defaults."""

from __future__ import annotations

import copy
import logging
import os
from collections.abc import Iterable
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
        # A window set to None means the plan does not meter it at all (see
        # plan_windows): Codex on the Go tier has a WEEKLY allowance only —
        # there is no 5-hour session window to show a bar for.
        "plans": {
            "go":   {"5h": None, "weekly": 8.0},
            "plus": {"5h": 10.0, "weekly": 40.0},
            "pro":  {"5h": 60.0, "weekly": 300.0},
        },
    },
}

#: The rate-limit windows a plan preset can describe.
WINDOWS = ("5h", "weekly")

#: Credential tier strings whose plan key substring-matching can't find, or
#: would find WRONG: ChatGPT Go reports itself as "prolite", which contains
#: "pro" — the priciest tier — so the alias has to win before the generic
#: match runs, or a Go user gets Pro-sized bars and a 5h window they don't have.
TIER_ALIASES: dict[str, str] = {
    "prolite": "go",
    "pro_lite": "go",
    "plus_lite": "go",
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
        # Codex/ChatGPT: "auto" = on only when ~/.codex/auth.json is actually
        # there, so it costs nothing for people without the Codex CLI and
        # needs no config from the people who have it.
        "codex": {"enabled": "auto", "interval_s": 120},
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


def _finite_number(value: Any) -> float | None:
    """``value`` as a float if it is a real, finite number (bools are not)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def validate_budgets(budgets: Any) -> str | None:
    """Error message for an invalid "budgets" value, or None if valid.

    Pure, so the D-Bus layer can reject junk before it is persisted rather
    than after it has broken a snapshot.
    """
    if not isinstance(budgets, dict):
        return "budgets must be an object"
    for key, value in budgets.items():
        if not isinstance(key, str):
            return f"budget names must be strings (got {key!r})"
        number = _finite_number(value)
        if number is None:
            return f"budget {key!r} must be a finite number (got {value!r})"
        if number < 0:
            return f"budget {key!r} cannot be negative"
    return None


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


def plan_from_tier(tier: Any, plan_keys: Iterable[str]) -> str | None:
    """Map a provider-reported tier string to one of the tool's plan keys.

    Claude's credential carries ``rateLimitTier: default_claude_max_5x``,
    Codex a ``plan_type`` like ``plus`` — a substring match against the
    tool's own preset keys covers both without a per-provider table.
    ``TIER_ALIASES`` handles the tiers that substring-matching gets wrong.
    """
    if not tier or not isinstance(tier, str):
        return None
    lowered = tier.lower()
    keys = set(plan_keys)
    for alias, plan in TIER_ALIASES.items():
        if alias in lowered and plan in keys:
            return plan
    for key in sorted(keys, key=len, reverse=True):
        if key in lowered:
            return key
    return None


def plan_windows(tool: str, plan: str | None) -> dict[str, bool]:
    """Which rate-limit windows ``tool``'s ``plan`` actually meters.

    Not every subscription has both. Codex on the Go tier is metered on a
    weekly allowance with no 5-hour session window, so its preset carries
    ``"5h": None`` — "this plan has no such window". The UI drops that bar
    instead of scaling usage against a limit that does not exist.
    """
    spec = PLAN_PRESETS.get(tool)
    if not spec:  # unknown tool (third-party adapter): assume the usual pair
        return dict.fromkeys(WINDOWS, True)
    base = spec["plans"].get(plan) or spec["plans"][spec["default_plan"]]
    return {window: base.get(window) is not None for window in WINDOWS}


def resolve_budgets(
    plans: dict[str, str] | None,
    mode: str,
    overrides: dict[str, float] | None,
    peaks: dict[str, dict[str, float]] | None = None,
    detected: dict[str, str] | None = None,
) -> dict[str, float]:
    """Concrete per-tool budget dict the UI consumes (``claude_5h`` etc.).

    Precedence per key, highest first:
      1. an explicit ``budgets:`` override in config,
      2. the user's observed peak * headroom (only when ``mode == 'auto'``),
      3. the plan preset for the tool — an explicit ``plans:`` entry, else
         the plan a live poller ``detected`` from the credential, else the
         tool's default_plan.
    Any non-tool keys already in ``overrides`` (daily/weekly/monthly) pass
    through untouched.
    """
    plans = plans or {}
    overrides = overrides or {}
    peaks = peaks or {}
    detected = detected or {}
    resolved: dict[str, float] = dict(overrides)
    for tool, spec in PLAN_PRESETS.items():
        prefix = spec["prefix"]
        plan = plans.get(tool) or detected.get(tool) or spec["default_plan"]
        base = spec["plans"].get(plan) or spec["plans"][spec["default_plan"]]
        for window in WINDOWS:
            key = f"{prefix}_{window}"
            if key in overrides:  # explicit value wins outright
                continue
            limit = base.get(window)
            if limit is None:
                # The plan has no such window (Codex Go: weekly only). Emit no
                # denominator — a bar with no limit behind it is a lie, and the
                # UI keys off the missing budget to drop it.
                continue
            value = float(limit)
            if mode == "auto":
                peak = (peaks.get(tool) or {}).get(window, 0.0)
                value = max(value, peak * AUTO_HEADROOM)
            resolved[key] = round(value, 2)
    return resolved


def resolve_windows(
    plans: dict[str, str] | None,
    detected: dict[str, str] | None = None,
    overrides: dict[str, float] | None = None,
) -> dict[str, dict[str, bool]]:
    """Per-tool ``{"5h": bool, "weekly": bool}`` — the windows to render.

    Same plan precedence as :func:`resolve_budgets` (explicit ``plans:``, then
    the plan a live poller detected from the credential, then the default).
    An explicit ``budgets.<prefix>_<window>`` override re-enables a window the
    preset says the plan lacks: the user naming a ceiling asserts it exists.
    """
    plans = plans or {}
    detected = detected or {}
    overrides = overrides or {}
    resolved: dict[str, dict[str, bool]] = {}
    for tool, spec in PLAN_PRESETS.items():
        plan = plans.get(tool) or detected.get(tool) or spec["default_plan"]
        support = plan_windows(tool, plan)
        for window in WINDOWS:
            override = _finite_number(overrides.get(f"{spec['prefix']}_{window}"))
            if override is not None and override > 0:
                support[window] = True
        resolved[tool] = support
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
        """Budget overrides, junk dropped.

        These reach the daemon from two places that can put anything in them —
        a hand-edited config.yaml and any peer on the session bus calling
        SetSettings — and they are consumed as numbers (``value > 0``,
        ``float(value)``). Filtering here keeps one bad entry from raising out
        of a snapshot or a waybar run instead of just being ignored.
        """
        raw = self._data.get("budgets") or {}
        if not isinstance(raw, dict):
            return {}
        clean: dict[str, float] = {}
        for key, value in raw.items():
            number = _finite_number(value)
            if number is None or number < 0:
                log.warning("Ignoring non-numeric budget %r: %r", key, value)
                continue
            clean[str(key)] = number
        return clean

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
