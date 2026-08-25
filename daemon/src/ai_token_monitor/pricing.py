"""Cost engine: maps a UsageRecord to USD using fnmatch pricing rules."""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Any

from .models import UsageRecord

_ZERO: dict[str, Any] = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}


def _rate(rule: dict[str, Any], key: str) -> float:
    """One price from a rule, or 0.0 if it isn't a usable number.

    Pricing rules are hand-written YAML. A typo ("1,25"), a missing value or a
    literal `.nan` must not propagate: a NaN cost is stored, summed, and then
    serialized as the JSON literal `NaN`, which the extension's JSON.parse
    rejects — one bad line in config.yaml would freeze the whole UI.
    """
    value = rule.get(key, 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return 0.0
    return number


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
            record.input_tokens * _rate(rate, "input")
            + record.output_tokens * _rate(rate, "output")
            + record.cache_read_tokens * _rate(rate, "cache_read")
            + record.cache_write_tokens * _rate(rate, "cache_write")
        ) / 1_000_000
