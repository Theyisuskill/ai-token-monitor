"""Entry point: `ai-token-monitor` runs the daemon; flags allow one-shot use."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from . import __version__, config as config_mod

#: The providers' rate-limit windows, both anchored to first use.
SESSION_SPAN = 5.0 * 3600.0
WEEK_SPAN = 7.0 * 24.0 * 3600.0

TOOL_LABELS = {"claude_code": "Claude Code", "gemini_cli": "agy",
               "codex": "Codex"}


def waybar_status(store, cfg) -> dict:
    """Waybar custom-module payload: the most-pressured limit as text/class,
    every tool's bars in the tooltip. Pure (no GLib) — usable one-shot.

    Matches the GNOME panel indicator: percentage = max pressure across all
    5h and weekly bars (both anchored to first use, like the providers');
    class flips at 70/90%.
    """
    peaks = None
    if cfg.budget_mode == "auto":
        peaks = {
            tool: {"5h": store.peak_window(tool, 5.0 * 3600.0),
                   "weekly": store.peak_window(tool, 7.0 * 24.0 * 3600.0)}
            for tool in config_mod.PLAN_PRESETS
        }
    budgets = config_mod.resolve_budgets(cfg.plans, cfg.budget_mode,
                                         cfg.budgets, peaks)
    prefixes = {tool: spec["prefix"]
                for tool, spec in config_mod.PLAN_PRESETS.items()}

    def span_str(seconds: float) -> str:
        minutes = max(1, int(seconds // 60))
        if minutes < 60:
            return f"{minutes}m"
        hours, minutes = divmod(minutes, 60)
        if hours < 48:
            return f"{hours}h {minutes:02d}m" if minutes else f"{hours}h"
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h" if hours else f"{days}d"

    now = time.time()
    max_pct = 0.0
    lines = []
    for tool in store.tools_seen():
        prefix = prefixes.get(tool, tool)
        parts = []
        for label, span, lookback, budget in (
                ("5h", SESSION_SPAN, 14,
                 budgets.get(f"{prefix}_5h", 0.0)),
                ("wk", WEEK_SPAN, None,
                 budgets.get(f"{prefix}_weekly", 0.0))):
            anchor = store.session_anchor(tool, span, lookback)
            cost = (store.summary(anchor, tool=tool)["totals"]["cost_usd"]
                    if anchor else 0.0)
            if budget > 0:
                pct = cost / budget * 100
                max_pct = max(max_pct, pct)
                part = f"{label} {round(pct)}% (${cost:.2f}/${budget:.0f}"
            else:
                part = f"{label} ${cost:.2f}"
            if anchor:
                part += f", resets in {span_str(anchor + span - now)}"
            if budget > 0:
                part += ")"
            parts.append(part)
        lines.append(f"{TOOL_LABELS.get(tool, tool)} · " + " · ".join(parts))

    level = ("danger" if max_pct >= 90 else
             "warn" if max_pct >= 70 else "ok")
    return {
        "text": f"{round(max_pct)}%",
        "percentage": round(max_pct),
        "class": level,
        "tooltip": "\n".join(lines) or "no usage recorded",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ai-token-monitor",
        description="Unified token-usage monitor for local AI CLI tools.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", metavar="PATH",
                        help=f"config file (default: {config_mod.CONFIG_PATH})")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug logging")
    parser.add_argument("--backfill", action="store_true",
                        help="scan all logs once, update the database, exit")
    parser.add_argument("--reparse", metavar="TOOL",
                        help="drop TOOL's usage history and re-ingest its logs "
                             "from scratch (after parser/pricing fixes), exit")
    parser.add_argument("--summary", metavar="PERIOD",
                        choices=("1h", "5h", "24h", "today", "week", "month", "all"),
                        help="print a JSON summary from the database and exit")
    parser.add_argument("--daily", metavar="PERIOD", dest="daily",
                        choices=("week", "month", "all"),
                        help="print a per-day JSON series and exit")
    parser.add_argument("--waybar", action="store_true",
                        help="print a Waybar custom-module JSON object and exit")
    args = parser.parse_args(argv)

    cfg = config_mod.load(args.config)
    level = "debug" if args.verbose else str(cfg.get("log_level", "info"))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.summary or args.daily or args.waybar:
        from .store import Store, period_start

        store = Store(cfg.database)
        try:
            if args.waybar:
                result = waybar_status(store, cfg)
            elif args.summary:
                result = store.summary(period_start(args.summary))
                result["period"] = args.summary
            else:
                result = {"period": args.daily,
                          "days": store.daily_series(period_start(args.daily))}
        finally:
            store.close()
        if args.waybar:
            json.dump(result, sys.stdout)  # Waybar wants one compact line
        else:
            json.dump(result, sys.stdout, indent=2)
        print()
        return 0

    from .daemon import Daemon

    daemon = Daemon(cfg)
    if args.reparse:
        try:
            inserted = daemon.reparse(args.reparse)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            daemon.store.close()
            return 2
        print(f"reparse {args.reparse}: {inserted} records re-ingested",
              file=sys.stderr)
        daemon.store.close()
        return 0
    if args.backfill:
        inserted = daemon.backfill()
        print(f"backfill: {inserted} new records", file=sys.stderr)
        daemon.store.close()
        return 0

    daemon.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
