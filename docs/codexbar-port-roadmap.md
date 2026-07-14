# CodexBar → ai-token-monitor: Linux port roadmap

Notes from dissecting [steipete/CodexBar](https://github.com/steipete/CodexBar)
(macOS/Swift, 58 providers) and mapping its techniques onto this app's
adapter/daemon architecture for Fedora/GNOME.

## The one architectural difference

- **This app** infers usage by tailing local CLI logs, sums tokens into
  `UsageRecord`s, prices them, and **scales the 5h/weekly bars by an estimated
  dollar budget** per plan tier. It never sees the provider's true numbers.
- **CodexBar** reads the **provider's own authoritative rate-limit percentage
  and reset time** from a network API, authenticating with a credential that
  already lives on disk (an OAuth token file, an auth cookie, or a loopback
  session). Its number *is* the provider's truth — no scaling.

So the highest-value CodexBar features do **not** fit the
`roots()/matches()/parse() → UsageRecord` model. They are a new daemon
**"track"**: a GLib-timer poller that reads a local credential, makes an
outbound (or loopback) call, and writes a **real-limit field** onto the per-tool
window snapshot. That is a deliberate, opt-in relaxation of the local-only
philosophy and needs a one-time schema change end-to-end (window model → SQLite
snapshot → D-Bus → extension/Waybar rendering):

> Add to each window entry: `real_used_percent`, `real_reset_at`,
> `window_seconds`, `source: 'provider'|'estimate'`, plus optional
> `remaining_fraction`, `plan_tier`, and a `credits {used, limit, currency}`
> block. The snapshot builder **prefers `real_*` when present** (provider
> reset, no dollar scaling) and falls back to today's estimate otherwise. The
> extension renders a "real" bar distinctly from an "estimate" bar.

Portable to Linux (credential is a plain file, no Keychain): Claude OAuth
(`~/.claude/.credentials.json`), Codex OAuth (`~/.codex/auth.json`), Gemini CLI
OAuth (`~/.gemini/oauth_creds.json`), Antigravity local loopback, OpenCode local
`.db`. Portable with friction: anything cookie-based (Firefox `cookies.sqlite`
is plaintext; Chrome needs the gnome-keyring "Safe Storage" key; manual paste is
the fallback). **Not portable — do not chase:** every `#if os(macOS)` branch
(Keychain reads, SweetCookieKit auto-import, Safari cookies); Zed is
Keychain-only and infeasible here.

## Tracks

| # | Track | Type | Effort | Value | Status |
|---|-------|------|:---:|:---:|---|
| A | OpenCode local `.db` adapter | LOCAL | S | High | **Done** |
| B | Claude real 5h/weekly % + reset (OAuth) | NET+creds | M | ★ Highest | **Done** |
| C | Antigravity real quota via local loopback | loopback | M | High | **On (awaits agy run)** |
| G | Provider status / "real vs estimate" badges | LOCAL/NET | S | Med-High | **Done** |
| D | Gemini/Antigravity quota via Google OAuth | NET+creds | M/L | Med | Fallback |
| F | OpenCode real 5h/weekly % (web cookie) | NET+cookie | M | Med | Optional |
| E | Codex/ChatGPT real limits (OAuth) | NET+creds | M | Low* | **Built (off)** |
| H | Qwen | NET+creds | M/L | Low | Deferred |
| I | Anthropic/OpenAI Admin-API ledgers | NET+creds | M | Low | Deferred |

\* Low only because there's no `~/.codex` on this machine; the technique is 100% Linux-portable.

### Track A — OpenCode local `.db` adapter — **DONE**
`daemon/src/ai_token_monitor/adapters/opencode.py`. Reads
`~/.local/share/opencode/opencode.db` (`message` table, `role='assistant'`),
emits per-turn `UsageRecord`s with OpenCode's own computed cost. Validated
against real data: 595 turns, $3.34, matches the `session` table's `sum(cost)`.
Required one daemon change — an adapter-supplied non-zero `cost_usd` is now kept
instead of being re-priced to $0 for models absent from the pricing table.

### Track B — Claude real 5h/weekly % + reset  ★ highest value — **DONE**
Shipped as a read-only live poller: `daemon/src/ai_token_monitor/live/claude.py`
+ the `live/` package + a threaded poll loop in `daemon.py` that attaches a
`real {used_percent, resets_at, source}` block to the 5h/weekly snapshot
entries (and a top-level `live` status map). The extension prefers `real` over
the estimate, shows a teal "live" dot + the exact reset, and drives the top-bar
% and alerts from the real numbers. Enabled via `live_limits.claude_code`.
Verified live on the Max-20x account (token has `user:profile`): 5h ≈2%,
weekly 26%, scoped "Fable" 21%. Details below for reference.

- Credential (file, cross-platform): `~/.claude/.credentials.json` →
  `claudeAiOauth {accessToken, refreshToken, expiresAt(ms), scopes[], subscriptionType, rateLimitTier}`.
- Usage: `GET https://api.anthropic.com/api/oauth/usage`, headers
  `Authorization: Bearer <accessToken>`, **`anthropic-beta: oauth-2025-04-20` (required)**,
  `Accept: application/json`. Token must carry the **`user:profile`** scope
  (else `403`).
- Refresh when `now >= expiresAt/1000`:
  `POST https://platform.claude.com/v1/oauth/token`
  (`grant_type=refresh_token&refresh_token=<rt>&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e`),
  rewrite the file.
- Fields → `five_hour.{utilization, resets_at}`, `seven_day.{...}`,
  `seven_day_sonnet/opus.{...}`, `extra_usage.{used_credits, monthly_limit, ...}`
  (cents → ÷100). `utilization` maps 1:1 to `real_used_percent`.
- **First step: verify the org/Max-20x token on this box actually has
  `user:profile` scope.** If not, use the claude.ai `sessionKey` cookie path
  (Track B2: `GET https://claude.ai/api/organizations/{uuid}/usage`) or manual paste.

### Track C — Antigravity real quota via local loopback  (best philosophical fit)
No cloud, no credential — just talk to the running `agy` language server.
- Find its PID (`/proc/*/comm`, `/proc/*/cmdline` for `antigravity`/`language_server`),
  resolve its LISTEN port via `/proc/<pid>/fd/*` `socket:[inode]` matched against
  `/proc/<pid>/net/tcp{,6}` (`st=0A`) — CodexBar ships and Linux-tests this.
- `POST https://127.0.0.1:<port>/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary`,
  body `{"forceRefresh":true}`, headers `Content-Type: application/json`,
  `Connect-Protocol-Version: 1`, self-signed TLS (`verify=False`, loopback only).
- Parse `groups[].buckets[] {displayName, remainingFraction, resetTime}` → fills
  `remaining_fraction` + `real_reset_at` per pool — the exact Gemini vs Claude&GPT
  split this app already models. Identity/tier via `GetUserStatus`.
- Layer on top of the existing `.db` protobuf adapter (keep it for token/cost history).

### Track G — status / "real vs estimate" badges — **DONE**
Shipped in stages: the snapshot's top-level `live` map carries
`{status, fetched_at, plan_tier, stale?, data_fetched_at?}` per tool; poller
status *transitions* are logged to the journal (WARNING on failure, INFO on
recovery); a transient poll failure keeps serving the last OK data for up to
30 min (`live/base.py: effective_live`, windows whose reset passed are
dropped) so bars don't flap back to the dollar estimate. The extension marks a
real bar with a teal dot, a **stale** real bar with a grey dot + "data from Xm
ago", and shows an amber "live limits unavailable: <reason>" note under the
provider header while the poller is failing. Bonus beyond CodexBar: a
burn-rate projection (`live/projection.py`) over the sampled real % adds
`depletes_at` when the window would hit 100% before the provider resets it —
rendered as "runs out in X" plus a one-shot desktop notification.

### Tracks D / F / E / H / I
See the table. D (Gemini OAuth via `~/.gemini/oauth_creds.json` →
`cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota`) is the fallback for
when `agy` isn't running. F adds OpenCode's *real* web rate-limit % on top of
Track A's history via the `auth`/`__Host-auth` cookies for `opencode.ai`. E
(Codex, `~/.codex/auth.json` → `chatgpt.com/backend-api/wham/usage`) is fully
portable but unused here. H (Qwen) is unverified — the Qwen Code CLI likely
stores `~/.qwen/oauth_creds.json` (same shape as D), but confirm before building.
I (Admin-API cost/usage ledgers) fits the existing `UsageRecord` pipeline but
needs a Console org + Admin key, which a Max subscriber doesn't have.

## Recommended build order

1. **Track A** — done.
2. **Shared "real limit" daemon schema** (window model → snapshot → D-Bus →
   extension/Waybar), with **Track G** status plumbing folded in.
3. **Track B** — Claude OAuth real limits (verify `user:profile` scope first).
4. **Track C** — Antigravity local loopback (least invasive network track).
5. **Track G** — finalize the badges now that bars can be real/estimate/stale.
6. **Track D** — Gemini OAuth fallback.
7. **Track F** — OpenCode real web limits (only if wanted; cookie plumbing).
8. Defer E, H, I.

### Tracks C & E — built, shipped OFF

Both pollers exist (`live/antigravity.py`, `live/codex.py`) and register, but
default `enabled: false` in `live_limits`:

- **Antigravity** (`name=antigravity`, `tool=gemini_cli`): loopback primary
  (scan `/proc/net/tcp{,6}` for LISTEN, match inodes to code/antigravity/
  language_server `/proc/<pid>/fd`, POST `RetrieveUserQuotaSummary` with a
  `X-Codeium-Csrf-Token` gathered from process cmdlines; self-signed TLS on
  loopback) → Google-OAuth fallback (`~/.gemini/oauth_creds.json` →
  `cloudcode-pa…:retrieveUserQuota`). Read-only. On this box discovery finds
  the right ports but the running `code` process exposes no `--csrf_token`
  right now (→ 401) and the Gemini token is expired (→ `token_expired`), so it
  can't be live-verified yet; the pure normalizers are unit-tested. Off by
  default because it probes local server ports; **enabled in this machine's
  config** — pending a live run of Antigravity to verify end-to-end. Per-pool
  real rendering is done: scoped entries carry a `pool` key matching
  `QUOTA_GROUPS`, `_attach_live` copies them onto the snapshot's `groups[]`
  sub-entries, and the extension's grouped branch prefers `g.real` on the two
  pool bars.
- **Codex** (`name=codex`, `tool=codex`): `~/.codex/auth.json` →
  `chatgpt.com/backend-api/wham/usage`. Portable, unit-tested, off by default
  (no `~/.codex` here).

## Visual

The GNOME popup was restyled to read like CodexBar: near-black card, thin
fully-rounded **teal** pill meters (`.ai-fill-ok`), muted zinc labels
(label-left / %-right), teal history bars, and an "updated Xm ago" footer.
Kept deliberately: the per-tool brand dot (identity), amber/red severity at
70/90%, the agy two-pool split, and real-$ (not fake-%) bars for pay-as-you-go
tools like OpenCode.

**Provider switcher** (`ui.layout`, default `switcher`): a tab row at the top
of the popup — Overview + one tab per tool — so the widget stays compact and
you drill into a provider for its full card (CodexBar's Merge-Icons/Overview
idea). `_addTabBar` / `_addOverviewRow` / `_addProviderDetail` in
`extension.js`; togglable to the old `stacked` layout in Preferences.

**Brand icons** (`extension/icons/*-symbolic.svg`, see `icons/NOTICE`):
recolorable simple-icons SVGs (CC0) for Claude / Gemini / OpenCode plus an
original terminal glyph for Codex (OpenAI's marks left simple-icons at the
owner's request — don't bundle them). Tinted with the tool's brand color in
tabs, card headers and link rows; the top-bar indicator swaps its generic
gauge for the most-pressured provider's glyph (monochrome). Summary shows a
per-provider "Limits" mini-bar list; bars fill in with a 350ms scale sweep
(honors `enable-animations`); over-pace weekly rows tint amber.
