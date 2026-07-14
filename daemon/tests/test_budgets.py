from ai_token_monitor.config import AUTO_HEADROOM, plan_from_tier, resolve_budgets


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
