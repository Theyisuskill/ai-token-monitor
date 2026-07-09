"""Entry point: `ai-token-monitor` runs the daemon; flags allow one-shot use."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import __version__, config as config_mod


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
    parser.add_argument("--summary", metavar="PERIOD",
                        choices=("today", "week", "month", "all"),
                        help="print a JSON summary from the database and exit")
    parser.add_argument("--daily", metavar="PERIOD", dest="daily",
                        choices=("week", "month", "all"),
                        help="print a per-day JSON series and exit")
    args = parser.parse_args(argv)

    cfg = config_mod.load(args.config)
    level = "debug" if args.verbose else str(cfg.get("log_level", "info"))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.summary or args.daily:
        from .store import Store, period_start

        store = Store(cfg.database)
        try:
            if args.summary:
                result = store.summary(period_start(args.summary))
                result["period"] = args.summary
            else:
                result = {"period": args.daily,
                          "days": store.daily_series(period_start(args.daily))}
        finally:
            store.close()
        json.dump(result, sys.stdout, indent=2)
        print()
        return 0

    from .daemon import Daemon

    daemon = Daemon(cfg)
    if args.backfill:
        inserted = daemon.backfill()
        print(f"backfill: {inserted} new records", file=sys.stderr)
        daemon.store.close()
        return 0

    daemon.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
