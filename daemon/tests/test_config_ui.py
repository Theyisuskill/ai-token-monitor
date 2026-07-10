"""config.yaml + ui.yaml overlay and the Preferences write path."""

import pytest

from ai_token_monitor import config as cfg


@pytest.fixture
def paths(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(cfg, "UI_PATH", tmp_path / "ui.yaml")
    return config_path


def test_ui_overrides_win_over_config_yaml(paths):
    paths.write_text("plans:\n  claude_code: max_20x\n")
    assert cfg.load(paths).plans["claude_code"] == "max_20x"

    cfg.save_ui_overrides({"plans": {"claude_code": "pro"}})
    loaded = cfg.load(paths)
    assert loaded.plans["claude_code"] == "pro"
    assert loaded.path == paths


def test_save_merges_and_ignores_foreign_keys(paths):
    cfg.save_ui_overrides({"plans": {"claude_code": "pro"}})
    cfg.save_ui_overrides({"budget_mode": "auto",
                           "database": "/tmp/evil.db"})  # not a UI key
    loaded = cfg.load(paths)
    assert loaded.plans["claude_code"] == "pro"      # earlier write preserved
    assert loaded.budget_mode == "auto"
    assert "evil" not in str(loaded.database)


def test_invalid_ui_yaml_is_ignored(paths):
    paths.write_text("plans:\n  claude_code: max_5x\n")
    cfg.UI_PATH.write_text("{not yaml::")
    loaded = cfg.load(paths)
    assert loaded.plans["claude_code"] == "max_5x"


def test_budget_mode_falls_back_to_preset(paths):
    paths.write_text("budget_mode: nonsense\n")
    assert cfg.load(paths).budget_mode == "preset"
