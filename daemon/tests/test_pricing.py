"""Cost engine: fnmatch rule selection and hostile pricing values."""

from ai_token_monitor.models import UsageRecord
from ai_token_monitor.pricing import CostEngine

RULES = [
    {"match": "claude-opus-*", "input": 5.0, "output": 25.0,
     "cache_read": 0.5, "cache_write": 6.25},
    {"match": "*", "input": 0.0, "output": 0.0,
     "cache_read": 0.0, "cache_write": 0.0},
]


def rec(model, **tokens):
    fields = {"input_tokens": 0, "output_tokens": 0,
              "cache_read_tokens": 0, "cache_write_tokens": 0}
    fields.update(tokens)
    return UsageRecord(tool="t", model=model, ts=0.0, dedup_key="k", **fields)


def test_first_matching_rule_wins():
    engine = CostEngine(RULES)
    cost = engine.cost(rec("claude-opus-4-5", input_tokens=1_000_000,
                           output_tokens=1_000_000))
    assert cost == 30.0


def test_unmatched_model_falls_through_to_the_catch_all():
    engine = CostEngine(RULES)
    assert engine.cost(rec("mystery-model", input_tokens=1_000_000)) == 0.0


def test_unusable_prices_count_as_zero_not_nan():
    """A typo'd or `.nan` price would otherwise be stored, summed, and then
    serialized as the JSON literal NaN — which the extension's JSON.parse
    rejects, freezing the whole UI on one bad config line."""
    engine = CostEngine([{"match": "*", "input": "1,25", "output": float("nan"),
                          "cache_read": None, "cache_write": True}])
    cost = engine.cost(rec("m", input_tokens=1_000_000, output_tokens=1_000_000,
                           cache_read_tokens=1_000_000,
                           cache_write_tokens=1_000_000))
    assert cost == 0.0


def test_infinite_price_does_not_reach_the_snapshot():
    engine = CostEngine([{"match": "*", "input": float("inf")}])
    assert engine.cost(rec("m", input_tokens=1_000_000)) == 0.0
