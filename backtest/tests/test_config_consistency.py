"""Config-consistency guard (Task 5).

Asserts the README's documented "authoritative live values" table stays in sync
with intraday_config.json, so the single-source-of-truth claim can't silently
rot. If someone edits the config but not the README (or vice-versa), this fails.

intraday_config.json is .gitignored, so if it's absent the test skips (nothing
to compare against) rather than failing a fresh checkout.
"""
import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(ROOT, "intraday_config.json")
README_PATH = os.path.join(ROOT, "README.md")

# The subset of settings the README documents as the authoritative live values.
# (key, expected_value). Keep in sync with the Configuration table in README.md.
DOCUMENTED_SETTINGS = {
    "min_rvol": 1.3,
    "preferred_rvol": 2.0,
    "min_rr": 1.8,
    "preferred_rr": 2.5,
    "buy_score_threshold": 60,
    "sell_score_threshold": 60,
    "almost_min_score": 55,
    "max_entry_extension_atr": 0.5,
    "min_body_ratio": 0.5,
    "allow_counter_trend": False,
    "use_market_regime_filter": True,
    "entry_start_time": "09:45",
    "entry_end_time": "14:30",
    "watch_start_time": "09:15",
    "square_off_time": "15:15",
    "max_data_age_seconds": 300,
    "max_trades_per_day": 4,
    "max_consecutive_losses": 3,
    "dynamic_min_rvol": 1.5,
}

DOCUMENTED_PAPER_RISK = {
    "account_equity": 100000,
    "risk_per_trade_percent": 0.5,
    "max_daily_loss_percent": 2.0,
    "max_open_positions": 3,
    "max_position_value_percent": 20.0,
}


def _load_config():
    if not os.path.exists(CONFIG_PATH):
        pytest.skip("intraday_config.json not present (gitignored); nothing to reconcile")
    with open(CONFIG_PATH) as f:
        return json.load(f)


def test_readme_settings_match_config():
    cfg = _load_config()
    settings = cfg.get("settings", {})
    mismatches = []
    for key, expected in DOCUMENTED_SETTINGS.items():
        actual = settings.get(key)
        if actual != expected:
            mismatches.append(f"{key}: README says {expected!r}, config has {actual!r}")
    assert not mismatches, (
        "README Configuration table drifted from intraday_config.json:\n  "
        + "\n  ".join(mismatches)
        + "\nUpdate README.md (and DOCUMENTED_SETTINGS) OR the config so they agree."
    )


def test_readme_paper_risk_matches_config():
    cfg = _load_config()
    pr = cfg.get("settings", {}).get("paper_risk", {})
    mismatches = []
    for key, expected in DOCUMENTED_PAPER_RISK.items():
        actual = pr.get(key)
        if actual != expected:
            mismatches.append(f"paper_risk.{key}: README says {expected!r}, config has {actual!r}")
    assert not mismatches, (
        "README paper_risk table drifted from intraday_config.json:\n  "
        + "\n  ".join(mismatches)
    )


def test_risk_py_defaults_align_with_documented_paper_risk():
    """risk.py RiskLimits defaults should not contradict the documented
    paper_risk values (they are the fallback when paper_risk is absent)."""
    import risk
    limits = risk.RiskLimits()
    assert limits.account_equity == DOCUMENTED_PAPER_RISK["account_equity"]
    assert limits.risk_per_trade_percent == DOCUMENTED_PAPER_RISK["risk_per_trade_percent"]
    assert limits.max_daily_loss_percent == DOCUMENTED_PAPER_RISK["max_daily_loss_percent"]
    assert limits.max_open_positions == DOCUMENTED_PAPER_RISK["max_open_positions"]
    assert limits.max_position_value_percent == DOCUMENTED_PAPER_RISK["max_position_value_percent"]


def test_readme_mentions_single_source_of_truth():
    with open(README_PATH, encoding="utf-8") as f:
        text = f.read()
    assert "Single source of truth" in text
    # The stale 14:45 entry-window claim must be gone.
    assert "09:45 – 14:45" not in text
    assert "09:45-14:45" not in text
