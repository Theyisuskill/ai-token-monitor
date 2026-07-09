# AI Token Monitor

Unified token-usage monitor for local AI CLI tools (Claude Code, Gemini CLI,
...) on Linux. A lightweight daemon watches each tool's local logs via
inotify, normalizes usage into SQLite, and a native GNOME Shell extension
shows consolidated tokens + estimated cost in the top bar.

Target platform: **Fedora (GNOME Shell 45–50)**. Anything with systemd,
D-Bus and GNOME should work.

```
┌────────────────────────────────────────────────────────────────────┐
│ GNOME Shell top bar         ⬚ 12.4M · $3.12                        │
│                             (extension/ — GJS, ESM)                │
└──────────────▲─────────────────────────────────────────────────────┘
               │ D-Bus session bus: io.github.franycraft.AITokenMonitor
               │ GetSnapshot() / GetSummary(period) / signal UsageUpdated
┌──────────────┴─────────────────────────────────────────────────────┐
│ ai-token-monitor daemon (daemon/ — Python, GLib main loop)         │
│                                                                    │
│  LogWatcher (Gio.FileMonitor ≙ inotify, recursive, debounced)      │
│     │ new complete lines (offset-tracked tailing)                  │
│  Adapters (plugin registry + entry points)                         │
│     ├── claude_code   ~/.claude/projects/**/*.jsonl                │
│     └── gemini_cli    ~/.gemini/tmp/*/chats/session-*.jsonl        │
│     │ UsageRecord (normalized)                                     │
│  CostEngine (pricing rules from config.yaml)                       │
│     │                                                              │
│  Store (SQLite WAL: usage history + dedup + tail offsets)          │
│      ~/.local/share/ai-token-monitor/usage.db                      │
└────────────────────────────────────────────────────────────────────┘
        config: ~/.config/ai-token-monitor/config.yaml
```

## Design notes

- **Python for the daemon.** The workload is I/O-trivial (tailing a few JSONL
  files); the deciding factors are ecosystem fit, not throughput. PyGObject
  gives `Gio.FileMonitor` (inotify) and the GLib main loop, `dasbus` gives a
  declarative D-Bus service on that same loop, and all dependencies are
  already packaged in Fedora — the RPM needs zero pip downloads. Rust would
  cut RSS from ~30 MB to ~3 MB at a significant cost in contributor
  accessibility for new adapters; Go has weaker GLib/D-Bus ergonomics.
- **D-Bus over a UNIX socket or local REST.** The consumer is a GNOME
  extension — GJS speaks D-Bus natively (`Gio.DBusProxy`), the bus provides
  activation (the daemon starts on first use via `SystemdService=`),
  lifecycle, and signals (push updates, no polling).
- **Native GNOME extension over AppIndicator/tray.** GNOME Shell has no
  system tray; AppIndicator support itself comes from a third-party
  extension, so a GTK4 tray app would *depend on an extension anyway* while
  adding a second process and IPC hop. The native extension is strictly more
  stable on stock Fedora. (An AppIndicator frontend for KDE/others can be
  added later — the daemon doesn't care who consumes the bus.)
- **Idempotent ingestion.** Every record carries a `dedup_key` derived from
  the log line (message id / request id / uuid) with a UNIQUE constraint, so
  file rewrites, inode rotation and full rescans are all safe.

## Install (per-user, for development)

Dependencies (Fedora): `sudo dnf install python3-gobject python3-dasbus
python3-pyyaml pipx`

```console
$ make install-user
$ systemctl --user daemon-reload && systemctl --user enable --now ai-token-monitor.service
$ make enable        # then log out/in on Wayland
```

Useful checks:

```console
$ ai-token-monitor --summary today | jq .totals
$ busctl --user call io.github.franycraft.AITokenMonitor \
    /io/github/franycraft/AITokenMonitor \
    io.github.franycraft.AITokenMonitor1 GetSnapshot
$ journalctl --user -u ai-token-monitor -f
```

## Configuration

Copy `data/config.example.yaml` to `~/.config/ai-token-monitor/config.yaml`.
Defaults are built in, so the file is optional. Pricing rules are fnmatch
patterns evaluated top-to-bottom (USD per 1M tokens) — keep them in sync with
your providers' current price lists.

### Usage limits (5-hour & weekly bars)

The panel menu shows a "Five Hour" and a "Weekly" bar per tool, mirroring the
*rolling* rate-limit windows that Claude Code and Antigravity actually enforce
(both reset on a rolling basis anchored to first use — the weekly window is a
trailing 7 days, **not** a calendar week that resets on Monday).

Providers don't publish a dollar figure for those limits, so the bars are
scaled by an API-equivalent budget that **adapts to your plan** rather than
being hardcoded. Declare your tier and the daemon resolves the ceilings:

```yaml
plans:
  claude_code: max_20x   # pro | max_5x | max_20x  (Max = 5x / 20x the Pro cap)
  gemini_cli: pro        # Antigravity: free | pro | ultra
budget_mode: preset      # preset (use the tiers) | auto (self-calibrate)
```

`auto` ignores the tiers and instead calibrates each bar to your own busiest
observed 5h / 7-day window — useful if you don't know your plan's equivalent
value. Any explicit `budgets:` key (`claude_5h`, `claude_weekly`, `gemini_5h`,
`gemini_weekly`) overrides both.

## Writing an adapter

Subclass `Adapter` (three methods: `roots()`, `matches()`, `parse()`), and
either add it in-tree under `daemon/src/ai_token_monitor/adapters/` with the
`@register` decorator, or ship it as a separate pip package exposing the
entry point group `ai_token_monitor.adapters`. Adapters are pure parsers —
the daemon owns file watching, offsets and dedup. See
`adapters/claude_code.py` for the reference implementation.

## Packaging & publishing

- **RPM (recommended for Fedora):** `packaging/ai-token-monitor.spec` builds
  two subpackages — `ai-token-monitor` (daemon, `%pyproject` macros, systemd
  user unit, D-Bus activation) and `gnome-shell-extension-ai-token-monitor`.
  Publish through [Copr](https://copr.fedorainfracloud.org/) first; submit
  for Fedora review once the API stabilizes.
- **extensions.gnome.org:** `make pack` produces the reviewable zip. E.G.O
  requires a GPL-compatible license (this project is GPL-3.0-or-later) and
  no bundled binaries — the extension is a pure D-Bus client, which is
  exactly what reviewers want to see.
- **Why not Flatpak:** the daemon must read arbitrary dot-directories in
  `$HOME` (`~/.claude`, `~/.gemini`, ...) and own a session bus name, which
  defeats the sandbox (`filesystem=home` + `--own-name`), and GNOME
  extensions cannot be distributed as Flatpaks at all. RPM + E.G.O is the
  honest packaging story for this architecture.

## License

GPL-3.0-or-later. Fetch the license text into `LICENSE` before the first
release: `curl -o LICENSE https://www.gnu.org/licenses/gpl-3.0.txt`
