"""Cost engine: maps a UsageRecord to USD using fnmatch pricing rules."""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Any

from .models import UsageRecord

_ZERO: dict[str, Any] = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}


class CostEngine:
    def __init__(self, rules: list[dict[str, Any]]):
        self._rules = rules
        self._cache: dict[str, dict[str, Any]] = {}

    def rate_for(self, model: str) -> dict[str, Any]:
        rate = self._cache.get(model)
        if rate is None:
            rate = next(
                (r for r in self._rules if fnmatch(model, str(r.get("match", "*")))),
                _ZERO,
            )
            self._cache[model] = rate
        return rate

    def cost(self, record: UsageRecord) -> float:
        rate = self.rate_for(record.model)
        return (
            record.input_tokens * float(rate.get("input", 0.0))
            + record.output_tokens * float(rate.get("output", 0.0))
            + record.cache_read_tokens * float(rate.get("cache_read", 0.0))
            + record.cache_write_tokens * float(rate.get("cache_write", 0.0))
        ) / 1_000_000
