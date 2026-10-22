"""Offline tests for backtest.metrics (Task 4) - known blotter -> known metrics."""
import datetime as dt
import json

import pytest

from backtest import metrics as m


class FakeTrade:
    def __init__(self, r, net=None, pattern="ORB", signal="IMMEDIATE_BUY",
                 exit_reason="TARGET_3", day="2026-03-02", direction="BUY"):
        self.r_multiple = r
        self.net_pnl = net if net is not None else (r * 100 if r is not None else 0)
        self.pattern = pattern
        self.signal = signal
        self.exit_reason = exit_reason
        self.direction = direction
        self.entry_time = dt.datetime.fromisoformat(f"{day}T10:00:00")
        self.symbol = "ACME"
        self.quantity = 100
        self.gross_pnl = self.net_pnl
        self.cost = 0.0
        self.exit_price = 0.0
        self.entry_price = 0.0
        self.bars_held = 1


def test_core_metrics_known_blotter():
    # 3 wins (+2R,+1R,+3R) and 2 losses (-1R,-1R).
    trades = [FakeTrade(2.0), FakeTrade(1.0), FakeTrade(3.0),
              FakeTrade(-1.0, exit_reason="STOP_LOSS"),
              FakeTrade(-1.0, exit_reason="STOP_LOSS")]
    met = m.compute_metrics(trades)
    assert met["trades"] == 5
    assert met["wins"] == 3
    assert met["losses"] == 2
    assert met["win_rate"] == pytest.approx(0.6)
    # gross won = 6R, gross lost = 2R -> PF = 3.0
    assert met["profit_factor"] == pytest.approx(3.0)
    # expectancy = (6 - 2) / 5 = 0.8R
    assert met["expectancy_r"] == pytest.approx(0.8)
    assert met["total_r"] == pytest.approx(4.0)
    assert met["avg_win_r"] == pytest.approx(2.0)
    assert met["avg_loss_r"] == pytest.approx(-1.0)


def test_max_drawdown():
    # Equity-in-R curve: +2 (peak2), -1 (1), -1 (0), +3 (peak3). Max DD = 2.
    trades = [FakeTrade(2.0), FakeTrade(-1.0), FakeTrade(-1.0), FakeTrade(3.0)]
    met = m.compute_metrics(trades)
    assert met["max_drawdown_r"] == pytest.approx(2.0)


def test_profit_factor_all_wins_is_inf():
    trades = [FakeTrade(1.0), FakeTrade(2.0)]
    met = m.compute_metrics(trades)
    assert met["profit_factor"] == float("inf")


def test_empty_blotter():
    met = m.compute_metrics([])
    assert met["trades"] == 0
    assert met["expectancy_r"] is None


def test_trades_per_day():
    trades = [FakeTrade(1.0, day="2026-03-02"), FakeTrade(1.0, day="2026-03-02"),
              FakeTrade(-1.0, day="2026-03-03")]
    met = m.compute_metrics(trades)
    assert met["trading_days"] == 2
    assert met["trades_per_day"] == pytest.approx(1.5)


def test_breakdowns():
    trades = [FakeTrade(2.0, pattern="ORB"), FakeTrade(-1.0, pattern="VWAP_PULLBACK"),
              FakeTrade(1.0, pattern="ORB")]
    by_pat = m.breakdown_by_pattern(trades)
    assert set(by_pat) == {"ORB", "VWAP_PULLBACK"}
    assert by_pat["ORB"]["trades"] == 2
    assert by_pat["VWAP_PULLBACK"]["trades"] == 1


def test_skipped_unrated_trades():
    trades = [FakeTrade(1.0), FakeTrade(None)]  # one trade has no r_multiple
    met = m.compute_metrics(trades)
    assert met["trades"] == 1
    assert met["skipped"] == 1


# ---------------------------------------------------------------------------
# Decision-log blocker frequencies
# ---------------------------------------------------------------------------

def test_blocker_frequencies(tmp_path):
    log = tmp_path / "2026-03-02.jsonl"
    rows = [
        {"primary_blocker": "breakout", "buy_failed_conditions": ["breakout"], "sell_failed_conditions": []},
        {"primary_blocker": "breakout", "buy_failed_conditions": ["breakout", "rr"], "sell_failed_conditions": []},
        {"primary_blocker": "volume", "buy_failed_conditions": ["volume"], "sell_failed_conditions": []},
        {"primary_blocker": None, "buy_failed_conditions": [], "sell_failed_conditions": []},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    res = m.blocker_frequencies(files=[str(log)])
    assert res["records"] == 4
    assert res["primary_blocker"]["breakout"] == 2
    assert res["any_failed"]["breakout"] == 2
    assert res["any_failed"]["rr"] == 1
    # sole blocker: rows 1 ({breakout}) and 3 ({volume}) have exactly one fail.
    assert res["sole_blocker"]["breakout"] == 1
    assert res["sole_blocker"]["volume"] == 1
    assert "rr" not in res["sole_blocker"]  # rr never appeared alone


def test_render_report_runs():
    trades = [FakeTrade(2.0), FakeTrade(-1.0, exit_reason="STOP_LOSS")]
    report = m.full_report(trades)
    text = m.render_report(report)
    assert "BACKTEST METRICS" in text
    assert "BY SIGNAL" in text
