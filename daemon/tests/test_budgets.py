from ai_token_monitor.config import (
    AUTO_HEADROOM,
    Config,
    PLAN_PRESETS,
    plan_from_tier,
    plan_windows,
    resolve_budgets,
    resolve_windows,
    validate_budgets,
)


def test_defaults_are_lowest_tiers():
    budgets = resolve_budgets(plans={}, mode="preset", overrides={})
    assert budgets["claude_5h"] == 15.0
    assert budgets["claude_weekly"] == 75.0
    assert budgets["gemini_5h"] == 3.0
    assert budgets["codex_5h"] == 10.0


def test_plans_scale_the_budgets():
    budgets = resolve_budgets(
        plans={"claude_code": "max_20x", "gemini_cli": "ultra", "codex": "pro"},
        mode="preset", overrides={})
    assert budgets["claude_5h"] == 300.0
    assert budgets["claude_weekly"] == 1500.0
    assert budgets["gemini_weekly"] == 150.0
    assert budgets["codex_weekly"] == 300.0


def test_unknown_plan_falls_back_to_default():
    budgets = resolve_budgets(plans={"claude_code": "bogus"},
                              mode="preset", overrides={})
    assert budgets["claude_5h"] == 15.0


def test_explicit_override_beats_everything():
    budgets = resolve_budgets(
        plans={"claude_code": "max_20x"}, mode="auto",
        overrides={"claude_5h": 999.0},
        peaks={"claude_code": {"5h": 5000.0, "weekly": 5000.0}})
    assert budgets["claude_5h"] == 999.0


def test_auto_mode_calibrates_from_peaks_with_preset_floor():
    peaks = {"claude_code": {"5h": 70.0, "weekly": 73.0}}
    budgets = resolve_budgets(plans={"claude_code": "pro"},
                              mode="auto", overrides={}, peaks=peaks)
    assert budgets["claude_5h"] == round(70.0 * AUTO_HEADROOM, 2)
    # peaks below the preset floor keep the preset
    small = resolve_budgets(plans={"claude_code": "pro"}, mode="auto",
                            overrides={},
                            peaks={"claude_code": {"5h": 1.0, "weekly": 1.0}})
    assert small["claude_5h"] == 15.0


def test_non_tool_overrides_pass_through():
    budgets = resolve_budgets(plans={}, mode="preset",
                              overrides={"daily": 10.0, "monthly": 200.0})
    assert budgets["daily"] == 10.0
    assert budgets["monthly"] == 200.0


def test_detected_plan_fills_in_when_config_is_silent():
    budgets = resolve_budgets(plans={}, mode="preset", overrides={},
                              detected={"claude_code": "max_5x"})
    assert budgets["claude_5h"] == 75.0
    assert budgets["claude_weekly"] == 375.0


def test_explicit_plan_beats_detected():
    budgets = resolve_budgets(plans={"claude_code": "max_20x"},
                              mode="preset", overrides={},
                              detected={"claude_code": "max_5x"})
    assert budgets["claude_5h"] == 300.0


def test_plan_from_tier_matches_provider_strings():
    claude_plans = ("pro", "max_5x", "max_20x")
    assert plan_from_tier("default_claude_max_5x", claude_plans) == "max_5x"
    assert plan_from_tier("default_claude_max_20x", claude_plans) == "max_20x"
    assert plan_from_tier("default_claude_pro", claude_plans) == "pro"
    assert plan_from_tier("enterprise_raven", claude_plans) is None
    assert plan_from_tier(None, claude_plans) is None
    assert plan_from_tier("plus", ("plus", "pro")) == "plus"


def test_weekly_only_plan_has_no_5h_budget():
    """Codex on ChatGPT Go is metered weekly only — no 5h denominator to
    scale a bar with, so the key must be absent rather than 0 (which reads
    as 'unknown budget' elsewhere)."""
    budgets = resolve_budgets(plans={"codex": "go"}, mode="preset",
                              overrides={})
    assert "codex_5h" not in budgets
    assert budgets["codex_weekly"] == 8.0
    assert budgets["claude_5h"] == 15.0  # other tools unaffected


def test_weekly_only_plan_ignores_auto_peaks_for_the_missing_window():
    budgets = resolve_budgets(
        plans={"codex": "go"}, mode="auto", overrides={},
        peaks={"codex": {"5h": 900.0, "weekly": 1.0}})
    assert "codex_5h" not in budgets


def test_prolite_tier_maps_to_go_not_pro():
    """The Go credential reports "prolite", which *contains* "pro" — plain
    substring matching would hand a Go account the Pro tier's budgets and a
    5h window it does not have."""
    plans = PLAN_PRESETS["codex"]["plans"]
    assert plan_from_tier("prolite", plans) == "go"
    assert plan_from_tier("pro", plans) == "pro"
    assert plan_from_tier("plus", plans) == "plus"


def test_plan_windows_reports_the_missing_window():
    assert plan_windows("codex", "go") == {"5h": False, "weekly": True}
    assert plan_windows("codex", "plus") == {"5h": True, "weekly": True}
    assert plan_windows("claude_code", "max_20x") == {"5h": True, "weekly": True}
    # Unknown tool (third-party adapter): assume the usual pair.
    assert plan_windows("whatever", None) == {"5h": True, "weekly": True}


def test_resolve_windows_follows_plan_precedence():
    # Explicit config wins over the credential-detected plan.
    assert resolve_windows({"codex": "plus"}, {"codex": "go"})["codex"]["5h"]
    assert resolve_windows({}, {"codex": "go"})["codex"]["5h"] is False
    # Naming an explicit ceiling asserts the window exists.
    assert resolve_windows({}, {"codex": "go"},
                           {"codex_5h": 12.0})["codex"]["5h"] is True


def test_junk_budgets_are_dropped_not_crashed_on():
    """budgets reach the daemon from a hand-edited YAML and from any peer on
    the session bus; they are consumed as numbers, so junk must be ignored
    rather than raise out of a snapshot."""
    cfg = Config({"budgets": {"claude_5h": "lots", "codex_weekly": None,
                              "gemini_5h": True, "daily": -3, "weekly": 12.5}})
    assert cfg.budgets == {"weekly": 12.5}
    assert Config({"budgets": "nope"}).budgets == {}


def test_validate_budgets_rejects_what_it_cannot_use():
    assert validate_budgets({"claude_5h": 10}) is None
    assert validate_budgets({}) is None
    assert validate_budgets("nope")
    assert validate_budgets({"claude_5h": "10"})
    assert validate_budgets({"claude_5h": float("inf")})
    assert validate_budgets({"claude_5h": -1})
    assert validate_budgets({"claude_5h": True})
