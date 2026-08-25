# AGENTS.md — ai-token-monitor

Unified token-usage + rate-limit monitor for local AI CLIs on **Linux/Fedora
(GNOME Shell 45–50, Wayland)**. A Python daemon watches each tool's local logs,
normalizes usage into SQLite, and a native GNOME Shell extension shows
consolidated limits/cost in the top bar. Goal: be the Linux/Fedora equivalent of
[CodexBar](https://github.com/steipete/CodexBar) — see
`docs/codexbar-port-roadmap.md`.

## Architecture

```
GNOME Shell top bar  ──D-Bus(session)──►  daemon (GLib main loop)
  extension/ (GJS, ESM)   io.github.theyisuskill.AITokenMonitor
                                          ├─ Adapters (parse LOCAL logs → UsageRecord)
                                          ├─ Live pollers (read a credential → provider's REAL limits)
                                          ├─ CostEngine (fnmatch pricing) + Store (SQLite WAL)
                                          └─ snapshot() → JSON over D-Bus (+ UsageUpdated signal)
```

- **Daemon** (`daemon/src/ai_token_monitor/`): Python + PyGObject (GLib) + dasbus.
  Single-threaded on the GLib loop; network pollers run on worker threads and
  marshal results back with `GLib.idle_add`.
- **Extension** (`extension/`): thin D-Bus client (GJS/ESM). All parsing lives
  in the daemon; the extension only renders `GetSnapshot()`/`UsageUpdated`.
- **D-Bus interface is string-typed JSON** (`service.py`) — the schema can evolve
  without changing the introspection XML, so new snapshot fields need no XML edit.

## Two ways usage is measured (important)

1. **Adapters** (`adapters/`, LOCAL): pure parsers of a tool's log files.
   `roots()/matches(path)/parse(path,lines)->Iterator[UsageRecord]`. The daemon
   owns file-watching (inotify via `Gio.FileMonitor`), byte-offset tailing and
   dedup (`UNIQUE(tool, dedup_key)`). A `.db` adapter ignores `lines` and queries
   the sqlite file directly, relying on a stable `dedup_key`. Cost is computed by
   the fnmatch pricing engine **unless the adapter supplies a non-zero
   `cost_usd`** (OpenCode does — it prices each turn itself; the daemon keeps it).
   Adapters: `claude_code`, `gemini_cli` (=Antigravity/"agy", reads
   `~/.gemini/antigravity-cli/conversations/*.db` protobuf), `codex`, `opencode`
   (`~/.local/share/opencode/opencode.db`).
2. **Live pollers** (`live/`, NETWORK, opt-in): read a credential already on disk
   and fetch the provider's **real** 5h/weekly % + reset — not a dollar-scaled
   estimate. Subclass `LivePoller` (`name`, `tool`, `poll()->dict`), `@register`,
   configured under `live_limits.<name>`. **READ-ONLY** — never rewrite a
   credential file (don't race the CLI for its login). `poll()` runs off the main
   loop, must never raise (every failure → a `status` string), stdlib only.
   - `claude_code` (**on by default, verified**): `~/.claude/.credentials.json`
     OAuth (needs `user:profile` scope) → `GET api.anthropic.com/api/oauth/usage`
     with `anthropic-beta: oauth-2025-04-20`. Fills `real{used_percent,resets_at}`
     on the 5h/week snapshot entries + `real_scoped` per model + `extra_usage`.
   - `antigravity` (off): loopback to the running agy language server via /proc
     port scan, then Google-OAuth fallback (`~/.gemini/oauth_creds.json`).
   - `codex` (off): `~/.codex/auth.json` → `chatgpt.com/backend-api/wham/usage`.

## Snapshot shape (what the extension reads)

`five_hours`/`week`/`today`/`month`/`hour`/`day` (each `{tools:[{tool,cost_usd,
total_tokens,...}], totals}`), `daily` (7-day series), `budgets` (plan-scaled
denominators), `windows` (per-tool `{"5h":bool,"weekly":bool}` — which limit
windows that plan actually meters), `tools` (tools with usage), `ui`,
`updated`, and `live` (per-tool poller status). On a tool's `five_hours`/`week` entry a poller may add
`real{used_percent,resets_at,source}` and (week) `real_scoped[]`. The extension
**prefers `real` over the dollar estimate** and marks it with a teal "live" dot.
A `live` entry may carry `quiet: true` (`live.QUIET_STATUSES`) meaning "nothing
to report, not a failure" — the UI hides its amber warning for those.

**History is NOT in the snapshot.** `GetHistory(period)` (`week|month|quarter|
all`) returns `{daily, monthly, sessions, models, tools, totals}` from
`Store.history()`; the snapshot is re-emitted on every debounced update, so
months of series don't belong in it. The extension fetches it lazily for the
History tab and caches it (`HISTORY_TTL_MS`). Same data from the CLI:
`--history <period>`, `--sessions [N]`.

## Windows / budgets

5h and weekly are **rolling** windows anchored to first use (Claude/Antigravity
reset on a rolling basis, NOT calendar). Providers publish no dollar figure for
those limits, so bars are scaled by an API-equivalent budget that adapts to the
user's plan (`plans:` + `PLAN_PRESETS`, or `budget_mode: auto` self-calibrates).
Antigravity meters two independent pools (Gemini vs Claude&GPT) → `QUOTA_GROUPS`.

**Not every plan has both windows.** A `PLAN_PRESETS` window set to `None`
means the plan doesn't meter it — Codex on ChatGPT Go (credential tier
`prolite`) has a weekly allowance and no 5h session limit. `resolve_windows()`
resolves that per tool, `Daemon._window_support()` lets a live poller override
it with the windows the provider actually reports, and the extension drops the
bar (`_metersWindow`) instead of scaling against a limit that doesn't exist.
Plans are detected from the credential too: `LivePoller.offline_tier()` (Codex
decodes `chatgpt_plan_type` out of its OAuth JWT, no network) → `plans:` still
wins when set.

## Extension UI

- Popup: **provider switcher** — a tab row (Summary, one tab per tool, and an
  icon-only **History** tab), each opening a
  detailed card (Session, Weekly, per-model breakdown, cost). `ui.layout` =
  `switcher` (default) | `stacked`. `_addTabBar`/`_addProviderDetail` in
  `extension.js`. Prefs has a Popup-layout combo.
- Look mirrors CodexBar: near-black card, thin teal pill meters (`.ai-fill-ok`),
  zinc type, per-window reset countdowns, teal sparkline. Kept: per-tool brand
  dot, amber/red severity at 70/90%.
- Panel top-bar (`ui.panel` = percent|icon|today) shows max pressure (real % when
  available). Alerts at 70/90/100% (real % preferred).

## Install / test on this machine

The daemon and extension are installed as **COPIES, not symlinks to the repo**,
so editing the repo changes nothing until you sync:

- **Daemon** runs from `~/.local/share/ai-token-monitor-app/` via
  `~/.local/bin/ai-token-monitor` (a bash wrapper: `PYTHONPATH=<app> python3 -m
  ai_token_monitor`). No pipx/venv. To update: copy `daemon/src/ai_token_monitor/.`
  into `~/.local/share/ai-token-monitor-app/ai_token_monitor/`, clear
  `__pycache__`, then `systemctl --user restart ai-token-monitor`. **No logout.**
- **Extension** at `~/.local/share/gnome-shell/extensions/<uuid>/`. Copy
  `extension/{extension.js,prefs.js,metadata.json,stylesheet.css}` there, plus
  `extension/icons/` (brand SVGs), and `msgfmt` the `po/*.po`. **Wayland: must log out / log back in** to reload
  (`Alt+F2 r` is X11-only). Quick preview without logout:
  `dbus-run-session -- gnome-shell --nested --wayland`.
- `make` is **not installed** here — run the underlying commands directly
  (`install-daemon` = `pipx install ./daemon`, which is absent; use the copy
  method above). Makefile targets are still the source of truth for what to copy.

Verify the daemon live: `ai-token-monitor --summary all | jq`, or call
`GetSnapshot` over D-Bus and check `live` + per-entry `real`. `--live` polls
every enabled poller once and prints the raw result (why a bar is an estimate)
— note each call is a real request, so don't loop it against a rate-limited
provider. Logs:
`journalctl --user -u ai-token-monitor -f`.

## Conventions

- Tests: pure Python — no PyGObject/dasbus needed; adapters/pollers expose pure
  `normalize_*`/`parse` functions that are unit-tested with realistic payloads,
  no network. Keep them green. **pytest is NOT installed system-wide on this
  machine**: one-time `python3 -m venv ~/.venvs/aitm && ~/.venvs/aitm/bin/pip
  install pytest pyyaml`, then `cd daemon && ~/.venvs/aitm/bin/python -m pytest -q`.
- New adapters/pollers register via a decorator; ship third-party ones as pip
  packages exposing the `ai_token_monitor.adapters` entry-point group.
- GPL-3.0-or-later. Packaging: RPM + extensions.gnome.org (no Flatpak — the
  daemon must read `$HOME` dot-dirs and own a bus name). Extension must stay a
  pure D-Bus client (no bundled binaries) for E.G.O review.
- Reparse after a parser/pricing fix: `ai-token-monitor --reparse <tool>`
  (dedup keys are stable, so a plain backfill won't rewrite stored rows).

## Project memory / ongoing

`docs/codexbar-port-roadmap.md` tracks the CodexBar→Linux port (real-limit
tracks per provider). User: Claude **Max 20x** (org), Antigravity **AI Pro**,
uses Claude Code + agy + OpenCode. Git: solo project on `main`, remote
`git@github.com:Theyisuskill/ai-token-monitor.git` (SSH auth works).
