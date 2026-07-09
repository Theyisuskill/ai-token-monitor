"""Gemini CLI adapter.

Gemini CLI (legacy) writes per-session JSONL under
``~/.gemini/tmp/<project>/chats/session-<date>-<id>.jsonl``.
Gemini CLI (modern: agy) stores conversations as SQLite databases under
``~/.gemini/antigravity-cli/conversations/<id>.db``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

from ..models import UsageRecord, iso_to_epoch
from . import register
from .base import Adapter

log = logging.getLogger(__name__)


# -- Protobuf binary decoding helpers for SQLite payloads ----------------------

def decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while True:
        b = data[pos]
        val |= (b & 0x7F) << shift
        pos += 1
        if not (b & 0x80):
            break
        shift += 7
    return val, pos


def decode_protobuf(data: bytes, depth: int = 0) -> list[tuple[int, str, any]]:
    pos = 0
    limit = len(data)
    fields = []
    while pos < limit:
        try:
            key, pos = decode_varint(data, pos)
        except IndexError:
            break
        wire_type = key & 0x7
        field_num = key >> 3
        
        if wire_type == 0:  # Varint
            val, pos = decode_varint(data, pos)
            fields.append((field_num, "varint", val))
        elif wire_type == 1:  # 64-bit
            val = data[pos:pos+8]
            pos += 8
            fields.append((field_num, "fixed64", val))
        elif wire_type == 2:  # Length-delimited
            length, pos = decode_varint(data, pos)
            val = data[pos:pos+length]
            pos += length
            sub_fields = None
            if len(val) > 0 and depth < 5:
                try:
                    sub_fields = decode_protobuf(val, depth + 1)
                except Exception:
                    pass
            if sub_fields:
                fields.append((field_num, "message", sub_fields))
            else:
                fields.append((field_num, "bytes", val))
        elif wire_type == 5:  # 32-bit
            val = data[pos:pos+4]
            pos += 4
            fields.append((field_num, "fixed32", val))
        else:
            break
    return fields


def find_msg_by_num(fields: list, num: int) -> list | None:
    for f in fields:
        if f[0] == num and f[1] == 'message':
            return f[2]
    return None


def find_val_by_num(fields: list, num: int) -> any:
    for f in fields:
        if f[0] == num:
            return f[2]
    return None


@register
class GeminiCliAdapter(Adapter):
    name = "gemini_cli"

    def roots(self) -> list[Path]:
        # Return both the configured/legacy root and the modern agy conversations path
        roots = []
        configured = self.settings.get("root")
        if configured:
            roots.append(Path(configured).expanduser())
        else:
            roots.append(Path("~/.gemini/tmp").expanduser())
        
        modern_dir = Path("~/.gemini/antigravity-cli/conversations").expanduser()
        if modern_dir not in roots:
            roots.append(modern_dir)
        return roots

    def matches(self, path: Path) -> bool:
        # Legacy: session-*.jsonl under chats/
        # Modern: *.db under conversations/
        if path.suffix == ".jsonl" and path.parent.name == "chats":
            return True
        if path.suffix == ".db" and path.parent.name == "conversations":
            return True
        return False

    def parse(self, path: Path, lines: Iterable[str]) -> Iterator[UsageRecord]:
        if path.suffix == ".db":
            yield from self._parse_db(path)
        else:
            yield from self._parse_jsonl(path, lines)

    def _parse_db(self, path: Path) -> Iterator[UsageRecord]:
        try:
            # Query the database in read-only mode to prevent locking issues
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            cursor = conn.cursor()
            cursor.execute("SELECT idx, step_payload FROM steps WHERE step_type = 15;")
            rows = cursor.fetchall()
            conn.close()
        except Exception as e:
            log.warning("gemini_cli: failed to query SQLite db %s: %s", path, e)
            return

        for idx, payload in rows:
            if not payload:
                continue
            try:
                fields = decode_protobuf(payload)
                f5 = find_msg_by_num(fields, 5)
                if not f5:
                    continue
                
                f1 = find_msg_by_num(f5, 1)
                ts = find_val_by_num(f1, 1) if f1 else None
                if not ts:
                    ts = time.time()
                    
                f9 = find_msg_by_num(f5, 9)
                if not f9:
                    continue
                    
                input_tokens = find_val_by_num(f9, 2) or 0
                cache_read_tokens = find_val_by_num(f9, 5) or 0
                output_tokens = find_val_by_num(f9, 3) or 0
                
                # Deduplication key combines the conversation filename and step index
                dedup = f"{path.stem}:{idx}"

                yield UsageRecord(
                    tool=self.name,
                    model="gemini-3.5-flash",  # Default model for agy CLI usage
                    ts=float(ts),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=0,
                    session_id=path.stem,
                    dedup_key=dedup,
                )
            except Exception as e:
                log.debug("gemini_cli: error decoding step %d in %s: %s", idx, path.name, e)

    def _parse_jsonl(self, path: Path, lines: Iterable[str]) -> Iterator[UsageRecord]:
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("type") != "gemini":
                continue

            tokens = entry.get("tokens")
            if not isinstance(tokens, dict):
                continue
            dedup = entry.get("id")
            if not dedup:
                continue

            try:
                ts = iso_to_epoch(entry["timestamp"])
            except (KeyError, TypeError, ValueError):
                continue

            raw_input = int(tokens.get("input") or 0)
            cached = int(tokens.get("cached") or 0)
            tool = int(tokens.get("tool") or 0)
            output = int(tokens.get("output") or 0)
            thoughts = int(tokens.get("thoughts") or 0)

            yield UsageRecord(
                tool=self.name,
                model=entry.get("model") or "gemini-unknown",
                ts=ts,
                input_tokens=max(raw_input - cached, 0) + tool,
                output_tokens=output + thoughts,
                cache_read_tokens=cached,
                cache_write_tokens=0,
                session_id=path.stem,
                dedup_key=dedup,
            )
