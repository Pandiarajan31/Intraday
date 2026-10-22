"""Tests for the best-available daily candidate (Task 7, requirement a)."""
import pytest

from engine import compute_best_available


def _rec(symbol, buy_score=0, sell_score=0, signal="WAIT", rr=1.5,
         buy_failed=None, sell_failed=None, primary_blocker=None):
    return {
        "symbol": symbol, "signal": signal,
        "buy_score": buy_score, "sell_score": sell_score, "risk_reward": rr,
        "buy_failed_conditions": buy_failed or [], "sell_failed_conditions": sell_failed or [],
        "primary_blocker": primary_blocker,
    }


SETTINGS = {}


def test_picks_one_buy_and_one_sell_when_no_immediate():
    results = [
        _rec("AAA", buy_score=58, sell_score=10, primary_blocker="breakout", buy_failed=["breakout"]),
        _rec("BBB", buy_score=40, sell_score=20, primary_blocker="volume", buy_failed=["volume"]),
        _rec("CCC", buy_score=15, sell_score=55, primary_blocker="breakdown", sell_failed=["breakdown"]),
        _rec("DDD", buy_score=5, sell_score=30, primary_blocker="vwap", sell_failed=["vwap"]),
    ]
    ba = compute_best_available(results, SETTINGS)
    assert ba["active"] is True
    assert ba["buy"] is not None and ba["sell"] is not None
    # Best BUY is the highest buy_score among buy-leaning (AAA=58).
    assert ba["buy"]["symbol"] == "AAA"
    # Best SELL is the highest sell_score among sell-leaning (CCC=55).
    assert ba["sell"]["symbol"] == "CCC"


def test_best_available_is_clearly_labeled_context_only():
    results = [_rec("AAA", buy_score=58, primary_blocker="breakout", buy_failed=["breakout"])]
    ba = compute_best_available(results, SETTINGS)
    buy = ba["buy"]
    assert buy["best_available_direction"] == "BUY"
    assert "NOT a trade signal" in buy["best_available_label"]
    assert buy["best_available_blocker"] == "breakout"
    assert buy["best_available_score"] == 58
    # It must NOT masquerade as an IMMEDIATE signal.
    assert buy["signal"] not in ("IMMEDIATE_BUY", "IMMEDIATE_SELL")


def test_blocker_text_counts_extra_failures():
    results = [_rec("AAA", buy_score=50, primary_blocker="daily_trend",
                    buy_failed=["daily_trend", "vwap", "volume"])]
    ba = compute_best_available(results, SETTINGS)
    assert ba["buy"]["best_available_blocker"] == "daily_trend (+2 more)"


def test_suppressed_when_immediate_exists():
    results = [
        _rec("AAA", buy_score=72, signal="IMMEDIATE_BUY"),
        _rec("BBB", buy_score=58, primary_blocker="breakout", buy_failed=["breakout"]),
    ]
    ba = compute_best_available(results, SETTINGS)
    # A real signal exists, so best-available is NOT the thing to act on.
    assert ba["active"] is False
    # It still computes the candidates (for consistency) but active=False tells
    # the UI to prefer the real signals.
    assert ba["buy"] is not None


def test_errors_excluded():
    results = [
        {"symbol": "ERR", "signal": "ERROR", "buy_score": 0, "sell_score": 0},
        _rec("AAA", buy_score=45, primary_blocker="volume", buy_failed=["volume"]),
    ]
    ba = compute_best_available(results, SETTINGS)
    assert ba["buy"]["symbol"] == "AAA"
    assert ba["sell"] is None or ba["sell"]["symbol"] != "ERR"


def test_empty_results():
    ba = compute_best_available([], SETTINGS)
    assert ba["active"] is True   # nothing immediate -> would show, but...
    assert ba["buy"] is None and ba["sell"] is None  # ...nothing to show


def test_no_sell_leaning_candidate():
    # All records lean BUY -> sell is None, buy present.
    results = [_rec("AAA", buy_score=50, sell_score=10, buy_failed=["breakout"], primary_blocker="breakout"),
               _rec("BBB", buy_score=40, sell_score=5, buy_failed=["volume"], primary_blocker="volume")]
    ba = compute_best_available(results, SETTINGS)
    assert ba["buy"]["symbol"] == "AAA"
    assert ba["sell"] is None
