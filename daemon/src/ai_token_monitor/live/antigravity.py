"""Google / Antigravity ("agy") real 5h + weekly limits.

Antigravity is Google's Antigravity IDE (a VSCode fork that runs on this box as
the ``code`` process). Its language server exposes a local gRPC-web/Connect
endpoint that reports the account's authoritative quota pools. This app keys
Antigravity usage under the ``gemini_cli`` tool.

Two data paths, tried in order:

1. LOCAL LOOPBACK (primary, no credential). Find the running Antigravity
   language server by scanning ``/proc`` for a candidate process
   (``code`` / ``antigravity`` / ``language_server`` / ``agy``) that owns a
   ``127.0.0.1`` LISTEN socket, then POST::

       POST https://127.0.0.1:<port>/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary
       Content-Type: application/json
       Connect-Protocol-Version: 1
       {"forceRefresh": true}

   over a self-signed (unverified, loopback-only) TLS context, falling back to
   plain ``http://``. A valid response carries ``response.groups[]`` where each
   group ``{displayName, buckets[]}`` and each bucket
   ``{bucketId, displayName, remainingFraction, resetTime, disabled}``. The two
   pools are Gemini vs. Claude & GPT; each pool has a 5-hour (session) and a
   weekly bucket. ``used_percent = (1 - remainingFraction) * 100``. Identity /
   plan tier come from a best-effort ``GetUserStatus`` on the same endpoint.
   If no local server answers -> status ``not_running`` (silent, so a box
   without agy running just shows the local estimate).

2. GOOGLE OAUTH FALLBACK. Read ``~/.gemini/oauth_creds.json``
   (``{access_token, refresh_token, id_token, expiry_date(ms), scope}``).
   READ-ONLY: if ``expiry_date`` is past, report ``token_expired`` and do NOT
   refresh/rewrite the file (the CLI owns it). Otherwise POST::

       POST https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota
       Authorization: Bearer <access_token>
       {"project": "<id>"}

   parsing ``buckets[] {modelId, remainingFraction, resetTime}``. Gated on
   ``~/.gemini/settings.json`` ``security.auth.selectedType`` (skipped for
   ``api-key`` / ``vertex-ai``).

This poller is READ-ONLY and never raises: every failure becomes a ``status``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..models import iso_to_epoch
from . import register
from .base import LivePoller

log = logging.getLogger(__name__)

# Loopback (Antigravity language server) endpoints.
QUOTA_SUMMARY_PATH = "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
USER_STATUS_PATH = "/exa.language_server_pb.LanguageServerService/GetUserStatus"
CONNECT_PROTOCOL_VERSION = "1"
# The IDE/app language server authenticates local requests with this header; the
# token is passed on its command line. The `agy` CLI server needs no token.
CSRF_HEADER = "X-Codeium-Csrf-Token"
_CSRF_FLAGS = ("--csrf_token", "--extension_server_csrf_token")

# Google OAuth (Cloud Code Private API) endpoints.
QUOTA_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"
LOAD_CODE_ASSIST_URL = "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
GEMINI_CRED = "~/.gemini/oauth_creds.json"
GEMINI_SETTINGS = "~/.gemini/settings.json"
SKIP_AUTH_TYPES = {"api-key", "gemini-api-key", "vertex-ai"}

# Process command markers that identify an Antigravity language server.
_PROC_COMMS = {"code", "antigravity", "antigravity-cli", "agy", "language_server"}
_PROC_MARKERS = ("antigravity", "language_server", "language-server")


def _empty(status: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "fetched_at": time.time(),
            "plan_tier": None, "windows": {}, "scoped": [],
            "extra_usage": None, **extra}


def _epoch(iso: Any) -> float | None:
    if not isinstance(iso, str):
        return None
    try:
        return iso_to_epoch(iso)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Pure normalizers (unit-tested, no network).
# --------------------------------------------------------------------------- #

def _extract_groups(payload: dict[str, Any]) -> list[Any]:
    """Pull the ``groups`` array out of the several response envelopes the
    language server uses (``response`` / ``summary`` wrapper, or top-level)."""
    if not isinstance(payload, dict):
        return []
    for key in ("response", "summary"):
        sub = payload.get(key)
        if isinstance(sub, dict) and isinstance(sub.get("groups"), list):
            return sub["groups"]
    if isinstance(payload.get("groups"), list):
        return payload["groups"]
    return []


def _remaining_fraction(bucket: dict[str, Any]) -> float | None:
    """Extract the 0..1 remaining fraction, tolerating the nested ``remaining``
    (``{remainingFraction}`` or oneof ``{case, value}``) shape."""
    rf = bucket.get("remainingFraction")
    if isinstance(rf, (int, float)):
        return float(rf)
    rem = bucket.get("remaining")
    if isinstance(rem, dict):
        nested = rem.get("remainingFraction")
        if isinstance(nested, (int, float)):
            return float(nested)
        if rem.get("case") == "remainingFraction" and isinstance(rem.get("value"), (int, float)):
            return float(rem["value"])
    return None


def _pool_label(display_name: Any) -> str:
    text = display_name if isinstance(display_name, str) else ""
    lower = text.lower()
    if "gemini" in lower:
        return "Gemini"
    if "claude" in lower or "gpt" in lower:
        return "Claude & GPT"
    return text.strip() or "Quota"


def _pool_key(pool_label: str) -> str | None:
    """The daemon's QUOTA_GROUPS key for a pool, so scoped entries can drive
    the per-pool sub-bars in the snapshot (gemini / claude_gpt)."""
    return {"Gemini": "gemini", "Claude & GPT": "claude_gpt"}.get(pool_label)


def _bucket_kind(bucket: dict[str, Any]) -> str:
    combined = (
        str(bucket.get("bucketId") or "") + " " + str(bucket.get("displayName") or "")
    ).lower()
    if "5h" in combined or "5-hour" in combined or "five hour" in combined:
        return "session"
    if "weekly" in combined:
        return "weekly"
    return "other"


def has_usable_bucket(groups: list[Any]) -> bool:
    for group in groups:
        if not isinstance(group, dict):
            continue
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict):
                continue
            if not bucket.get("disabled") and _remaining_fraction(bucket) is not None:
                return True
    return False


def normalize_quota_summary(payload: dict[str, Any], plan_tier: str | None) -> dict[str, Any]:
    """Map a ``RetrieveUserQuotaSummary`` response to the daemon's shape.

    Each pool (Gemini, Claude & GPT) contributes its session (5-hour) and
    weekly buckets to ``scoped``; ``windows.five_hour`` / ``windows.weekly`` are
    the most-constrained (highest used%) bucket of each kind across pools.
    """
    scoped: list[dict[str, Any]] = []
    session: list[tuple[float, float | None]] = []
    weekly: list[tuple[float, float | None]] = []

    for group in _extract_groups(payload):
        if not isinstance(group, dict):
            continue
        pool = _pool_label(group.get("displayName"))
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict):
                continue
            if bucket.get("disabled"):
                continue
            rf = _remaining_fraction(bucket)
            if rf is None:
                continue
            used = max(0.0, min(100.0, (1.0 - rf) * 100.0))
            resets = _epoch(bucket.get("resetTime"))
            kind = _bucket_kind(bucket)
            if kind == "session":
                label, group_field = f"{pool} 5h", "session"
                session.append((used, resets))
            elif kind == "weekly":
                label, group_field = f"{pool} weekly", "weekly"
                weekly.append((used, resets))
            else:
                bname = str(bucket.get("displayName") or bucket.get("bucketId") or "").strip()
                label, group_field = (f"{pool} {bname}".strip() or pool), "weekly"
            scoped.append({"label": label, "used_percent": used,
                           "resets_at": resets, "group": group_field,
                           "pool": _pool_key(pool)})

    windows: dict[str, dict[str, Any]] = {}
    if session:
        used, resets = max(session, key=lambda item: item[0])
        windows["five_hour"] = {"used_percent": used, "resets_at": resets}
    if weekly:
        used, resets = max(weekly, key=lambda item: item[0])
        windows["weekly"] = {"used_percent": used, "resets_at": resets}

    return {"status": "ok", "fetched_at": time.time(), "plan_tier": plan_tier,
            "windows": windows, "scoped": scoped, "extra_usage": None}


def normalize_oauth_quota(payload: dict[str, Any], plan_tier: str | None) -> dict[str, Any]:
    """Map a Cloud Code ``retrieveUserQuota`` response to the daemon's shape.

    These are per-model rolling caps; the most-constrained model backs the
    ``weekly`` window (the longer-horizon bar) and every model lands in
    ``scoped``.
    """
    buckets = payload.get("buckets") if isinstance(payload, dict) else None
    per_model: dict[str, tuple[float, Any]] = {}
    if isinstance(buckets, list):
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            model = bucket.get("modelId")
            rf = bucket.get("remainingFraction")
            if not isinstance(model, str) or not isinstance(rf, (int, float)):
                continue
            reset = bucket.get("resetTime")
            if model not in per_model or rf < per_model[model][0]:
                per_model[model] = (float(rf), reset)

    scoped: list[dict[str, Any]] = []
    for model in sorted(per_model):
        rf, reset = per_model[model]
        used = max(0.0, min(100.0, (1.0 - rf) * 100.0))
        scoped.append({"label": model, "used_percent": used,
                       "resets_at": _epoch(reset), "group": "weekly"})

    windows: dict[str, dict[str, Any]] = {}
    if scoped:
        worst = max(scoped, key=lambda item: item["used_percent"])
        windows["weekly"] = {"used_percent": worst["used_percent"],
                             "resets_at": worst["resets_at"]}

    return {"status": "ok", "fetched_at": time.time(), "plan_tier": plan_tier,
            "windows": windows, "scoped": scoped, "extra_usage": None}


def parse_plan_tier(payload: dict[str, Any]) -> str | None:
    """Pull a human plan/tier label out of a ``GetUserStatus`` response."""
    if not isinstance(payload, dict):
        return None
    status = payload.get("userStatus")
    if not isinstance(status, dict):
        return None
    tier = status.get("userTier")
    if isinstance(tier, dict):
        name = tier.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    plan_status = status.get("planStatus")
    plan_info = plan_status.get("planInfo") if isinstance(plan_status, dict) else None
    if isinstance(plan_info, dict):
        for key in ("planDisplayName", "displayName", "productName", "planName", "planShortName"):
            value = plan_info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


# --------------------------------------------------------------------------- #
# /proc port discovery (Linux, own-user processes only; failures are silent).
# --------------------------------------------------------------------------- #

def _is_loopback_or_any(addr_hex: str) -> bool:
    if addr_hex and set(addr_hex) == {"0"}:  # 0.0.0.0 / :: wildcard
        return True
    if len(addr_hex) == 8:                    # IPv4: 127.x.x.x (big byte last)
        return addr_hex.endswith("7F")
    if len(addr_hex) == 32:                   # IPv6: ::1
        return addr_hex.endswith("01000000")
    return False


def _listen_loopback_inodes() -> dict[str, int]:
    """Map socket inode -> port for every 127.0.0.1 (or wildcard) LISTEN row."""
    result: dict[str, int] = {}
    for proto in ("tcp", "tcp6"):
        try:
            with open(f"/proc/net/{proto}", encoding="ascii", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        for line in lines[1:]:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":  # 0A == TCP_LISTEN
                continue
            local = fields[1]
            if ":" not in local:
                continue
            addr_hex, _, port_hex = local.partition(":")
            if not _is_loopback_or_any(addr_hex):
                continue
            try:
                result[fields[9]] = int(port_hex, 16)
            except ValueError:
                continue
    return result


def _extract_flag(cmd: str, flag: str) -> str | None:
    match = re.search(re.escape(flag) + r"[=\s]+(\S+)", cmd)
    return match.group(1) if match else None


def _antigravity_procs() -> list[tuple[str, list[str]]]:
    """Return ``(pid, [csrf tokens])`` for each Antigravity-like process.

    Tokens come from the ``--csrf_token`` / ``--extension_server_csrf_token``
    command-line flags (case-sensitive); an empty list means the CLI server,
    which requires no token.
    """
    procs: list[tuple[str, list[str]]] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return procs
    for entry in entries:
        if not entry.isdigit():
            continue
        comm = ""
        try:
            with open(f"/proc/{entry}/comm", encoding="ascii", errors="replace") as fh:
                comm = fh.read().strip().lower()
        except OSError:
            continue
        cmd = ""
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            pass
        low = cmd.lower()
        if comm not in _PROC_COMMS and not any(marker in low for marker in _PROC_MARKERS):
            continue
        tokens: list[str] = []
        for flag in _CSRF_FLAGS:
            token = _extract_flag(cmd, flag)
            if token and token not in tokens:
                tokens.append(token)
        procs.append((entry, tokens))
    return procs


def _socket_inodes(pid: str) -> set[str]:
    inodes: set[str] = set()
    fd_dir = f"/proc/{pid}/fd"
    try:
        fds = os.listdir(fd_dir)
    except OSError:
        return inodes
    for fd in fds:
        try:
            target = os.readlink(f"{fd_dir}/{fd}")
        except OSError:
            continue
        if target.startswith("socket:["):
            inodes.add(target[len("socket:["):-1])
    return inodes


def discover_candidates() -> list[tuple[int, str]]:
    """``(port, csrf_token)`` pairs to probe, for every 127.0.0.1 LISTEN port
    owned by an Antigravity-like process.

    Each port is tried with every CSRF token seen on an owning process, plus a
    tokenless attempt (the CLI server needs none). ``""`` means no token.
    """
    inode_port = _listen_loopback_inodes()
    if not inode_port:
        return []
    procs = _antigravity_procs()
    # A port-owning process (often the `code` host) may not carry the token; the
    # sibling language_server that holds it shares the same token, so try every
    # token seen across Antigravity processes against each discovered port.
    all_tokens = sorted({tok for _, tokens in procs for tok in tokens})
    ports: set[int] = set()
    for pid, _ in procs:
        for inode in _socket_inodes(pid):
            port = inode_port.get(inode)
            if port is not None:
                ports.add(port)
    candidates: list[tuple[int, str]] = []
    for port in sorted(ports):
        for token in all_tokens:
            candidates.append((port, token))
        candidates.append((port, ""))  # tokenless (CLI server) attempt
    return candidates


# --------------------------------------------------------------------------- #

@register
class AntigravityLimitsPoller(LivePoller):
    name = "antigravity"
    tool = "gemini_cli"

    def __init__(self, settings: dict[str, Any]):
        super().__init__(settings)
        self._ssl_ctx = ssl._create_unverified_context()  # loopback self-signed only

    # -- loopback ----------------------------------------------------------- #

    def _candidate_ports(self) -> list[tuple[int, str]]:
        override = self.settings.get("ports")
        if isinstance(override, list):
            out: list[tuple[int, str]] = []
            for value in override:
                try:
                    out.append((int(value), ""))
                except (TypeError, ValueError):
                    continue
            return out
        return discover_candidates()

    def _post(self, scheme: str, port: int, path: str, body: bytes,
              csrf: str = "") -> bytes | None:
        url = f"{scheme}://127.0.0.1:{port}{path}"
        headers = {
            "Content-Type": "application/json",
            "Connect-Protocol-Version": CONNECT_PROTOCOL_VERSION,
            "Content-Length": str(len(body)),
            "User-Agent": "ai-token-monitor",
        }
        if csrf:
            headers[CSRF_HEADER] = csrf
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        ctx = self._ssl_ctx if scheme == "https" else None
        timeout = float(self.settings.get("loopback_timeout_s", 2) or 2)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read()
        except (urllib.error.URLError, ssl.SSLError, OSError, TimeoutError, ValueError) as exc:
            log.debug("antigravity loopback %s:%s failed: %s", scheme, port, exc)
            return None

    def _probe_identity(self, scheme: str, port: int, csrf: str) -> str | None:
        body = json.dumps({"metadata": {
            "ideName": "antigravity", "extensionName": "antigravity",
            "ideVersion": "unknown", "locale": "en"}}).encode("utf-8")
        data = self._post(scheme, port, USER_STATUS_PATH, body, csrf)
        if data is None:
            return None
        try:
            payload = json.loads(data.decode("utf-8"))
        except ValueError:
            return None
        return parse_plan_tier(payload)

    def _probe_port(self, port: int, csrf: str) -> dict[str, Any] | None:
        body = json.dumps({"forceRefresh": True}).encode("utf-8")
        for scheme in ("https", "http"):
            data = self._post(scheme, port, QUOTA_SUMMARY_PATH, body, csrf)
            if data is None:
                continue
            try:
                payload = json.loads(data.decode("utf-8"))
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            if not has_usable_bucket(_extract_groups(payload)):
                continue
            plan = self._probe_identity(scheme, port, csrf)
            return normalize_quota_summary(payload, plan)
        return None

    def _poll_loopback(self) -> dict[str, Any] | None:
        for port, csrf in self._candidate_ports():
            result = self._probe_port(port, csrf)
            if result is not None:
                return result
        return None

    # -- Google OAuth fallback --------------------------------------------- #

    def _home(self) -> Path:
        return Path(self.settings.get("home") or "~").expanduser()

    def _selected_auth_type(self) -> str | None:
        path = self._home() / ".gemini" / "settings.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        auth = (((data or {}).get("security") or {}).get("auth") or {})
        selected = auth.get("selectedType")
        return selected if isinstance(selected, str) else None

    def _read_creds(self) -> dict[str, Any] | None:
        path = self._home() / ".gemini" / "oauth_creds.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _oauth_post(self, url: str, token: str, body: dict[str, Any],
                    timeout: float) -> tuple[dict[str, Any] | None, str | None]:
        """POST JSON with a Bearer token. Returns (payload, status-on-error)."""
        raw = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=raw, method="POST", headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "ai-token-monitor",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8")), None
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return None, "unauthorized"
            if exc.code == 403:
                return None, "unauthorized"
            if exc.code == 429:
                return None, "rate_limited"
            return None, f"http_{exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError):
            return None, "network"
        except ValueError:
            return None, "bad_response"

    def _load_project_id(self, token: str, timeout: float) -> str | None:
        payload, _ = self._oauth_post(
            LOAD_CODE_ASSIST_URL, token,
            {"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}}, timeout)
        if not isinstance(payload, dict):
            return None
        project = payload.get("cloudaicompanionProject")
        if isinstance(project, str) and project.strip():
            return project.strip()
        if isinstance(project, dict):
            for key in ("id", "projectId"):
                value = project.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    def _poll_oauth(self) -> dict[str, Any] | None:
        auth = self._selected_auth_type()
        if auth in SKIP_AUTH_TYPES:
            return None  # unsupported auth type; degrade to the local estimate

        creds = self._read_creds()
        if creds is None:
            return None  # no credential on disk; stay silent (not_running)

        token = creds.get("access_token")
        if not isinstance(token, str) or not token:
            return None

        expiry = creds.get("expiry_date")
        # Read-only: never refresh/rewrite the credential (the CLI owns it).
        if isinstance(expiry, (int, float)) and time.time() * 1000.0 >= float(expiry):
            return _empty("token_expired")

        timeout = float(self.settings.get("timeout_s", 15) or 15)
        project = self.settings.get("project") or self._load_project_id(token, timeout)
        body = {"project": project} if project else {}
        payload, error = self._oauth_post(QUOTA_URL, token, body, timeout)
        if error is not None:
            return _empty(error)
        if not isinstance(payload, dict):
            return _empty("bad_response")

        plan_tier = self.settings.get("plan_tier")
        return normalize_oauth_quota(payload, plan_tier if isinstance(plan_tier, str) else None)

    # -- entry point -------------------------------------------------------- #

    def poll(self) -> dict[str, Any]:
        try:
            loopback = self._poll_loopback()
            if loopback is not None:
                return loopback
            oauth = self._poll_oauth()
            if oauth is not None:
                return oauth
            return _empty("not_running")
        except Exception as exc:  # never raise out of a poller
            log.debug("antigravity poll failed: %s", exc)
            return _empty("bad_response")
