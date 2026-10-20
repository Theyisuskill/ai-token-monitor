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
2. **Limit readers** (`live/`, local or opt-in network): read the provider's
   **confirmed** 5h/weekly % + reset from local logs or an existing credential.
   Never turn API-equivalent spend into Claude/Codex quota percentages. Subclass `LivePoller` (`name`, `tool`, `poll()->dict`), `@register`,
   configured under `live_limits.<name>`. **READ-ONLY** — never rewrite a
   credential file (don't race the CLI for its login). `poll()` runs off the main
   loop, must never raise (every failure → a `status` string), stdlib only.
   - `claude_code` (**on by default, verified**): `~/.claude/.credentials.json`
     OAuth (needs `user:profile` scope) → `GET api.anthropic.com/api/oauth/usage`
     with `anthropic-beta: oauth-2025-04-20`. Fills `real{used_percent,resets_at}`
     on the 5h/week snapshot entries + `real_scoped` per model + `extra_usage`.
   - `antigravity` (off): loopback to the running agy language server via /proc
     port scan, then Google-OAuth fallback (`~/.gemini/oauth_creds.json`).
   - `codex` (`enabled: auto`, every 30s): reads provider-confirmed `rate_limits`
     from local rollout logs (`$CODEX_HOME/sessions`, or `sessions_root`). No HTTP
     or credential is needed by default; `source: api` is an explicit legacy mode.
     Preserve the log event timestamp when rereading; never refresh its age.
   - Successful results persist in SQLite (`confirmed_limits`). Claude HTTP 429
     responses honor `Retry-After` and back off exponentially.

## Snapshot shape (what the extension reads)

`five_hours`/`week`/`today`/`month`/`hour`/`day` (each `{tools:[{tool,cost_usd,
total_tokens,...}], totals}`), `daily` (7-day series), `budgets` (plan-scaled
denominators), `windows` (per-tool `{"5h":bool,"weekly":bool}` — which limit
windows that plan actually meters), `tools` (tools with usage), `ui`,
`updated`, and `live` (per-tool poller status). On a tool's `five_hours`/`week` entry a poller may add
`real{used_percent,resets_at,source,confirmed_at}` and (week) `real_scoped[]`.
`live[tool].last_confirmed` preserves the recorded values and confirmation time
across restarts. Current values expire after 30 minutes or their reset; older
values are explicitly historical. **Claude and Codex never fall back to
estimated quota percentages**, including the panel, alerts, Summary and Waybar.
Their snapshot budget denominators are zero and `confirmed_only` lists them.
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
those limits. Claude and Codex show only confirmed provider percentages; local
token counts and estimated API cost are separate context. Other providers may
use the existing plan-scaled estimates (`plans:` + `PLAN_PRESETS`, or
`budget_mode: auto` self-calibrates).
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

- Popup: **provider switcher** — accessible native icon buttons for Summary,
  each provider, and History. Buttons wrap when needed; provider views show
  Session, Weekly, per-model breakdown and cost. `ui.layout` =
  `switcher` (default) | `stacked`. `_addTabBar`/`_addProviderDetail` in
  `extension.js`. Prefs has a Popup-layout combo.
- **Confirmed user preference: native Fedora/GNOME appearance.** Inherit Shell
  backgrounds, typography, buttons and accent; use `BarLevel` for read-only
  quota bars. Avoid custom blue-black cards and oversized percentages.
- `popupLayout()` bounds width and height to the current monitor work area,
  accounting for display and text scaling. Monitor/scaling changes rebuild
  the layout. Long content scrolls vertically; labels wrap or ellipsize.
- Provider status distinguishes confirmed, saved and unavailable data and
  displays the last confirmation date/time. Cached quotas do not trigger alerts.
- Panel top-bar (`ui.panel` = percent|icon|today) shows max pressure. Claude
  and Codex percentages and 70/90/100% alerts require confirmed values.

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
every enabled reader once and prints its source/status. Codex is a local read;
Claude/other network readers make real requests, so do not loop them against a
rate-limited provider. Logs:
`journalctl --user -u ai-token-monitor -f`.

## Conventions

- Tests: pure Python — no PyGObject/dasbus needed; adapters/pollers expose pure
  `normalize_*`/`parse` functions that are unit-tested with realistic payloads,
  no network. Keep them green. **pytest is NOT installed system-wide on this
  machine**: one-time `python3 -m venv ~/.venvs/aitm && ~/.venvs/aitm/bin/pip
  install pytest pyyaml`, then `cd daemon && ~/.venvs/aitm/bin/python -m pytest -q`.
- UI policy/layout tests: `node --test extension/tests/*.test.cjs`; syntax
  checks: `node --check extension/extension.js` and `node --check extension/prefs.js`.
- Codex dedup keys use cumulative input/cache/output counters per rollout.
  Timestamp/quota-only repeats share the same key even after restart. A per-log
  transaction rekeys legacy rows and removes proven duplicates automatically;
  preserve history whose logs are unavailable. Missing counters retain the
  original line-hash fallback.
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
