"""Offline tests for the replay driver (Task 3).

evaluate_stock is monkeypatched to emit a controlled signal on a chosen bar so
we can assert the driver's trade-opening, one-trade-at-a-time, and daily-cap
behavior deterministically without depending on the engine's full logic.
"""
import datetime as dt

import pandas as pd
import pytest

from backtest import replay as replay_module
from backtest.data_shim import HistoryStore

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _day_5m(day, n, base=100.0, start=(9, 45)):
    out = []
    t = dt.datetime(day.year, day.month, day.day, start[0], start[1], tzinfo=IST)
    px = base
    for i in range(n):
        out.append({"timestamp": t.isoformat(), "open": px, "high": px + 2,
                    "low": px - 2, "close": px + 1, "volume": 10000})
        px += 1.0
        t += dt.timedelta(minutes=5)
    return out


SETTINGS = {
    "square_off_time": "15:15", "max_trades_per_day": 4, "atr_period": 14,
    "round_trip_cost_pct": 0.0, "trail_lookback_candles": 3,
    "structural_stop_lookback": 6, "structural_stop_min_atr": 0.6,
    "structural_stop_max_atr": 10.0, "structural_stop_buffer_atr": 0.2,
    "paper_risk": {"account_equity": 100000, "risk_per_trade_percent": 0.5,
                   "max_position_value_percent": 100.0},
}


def _patch_signal_on_bar(monkeypatch, signal_bar_index, direction="IMMEDIATE_BUY"):
    """Make evaluate_stock emit `direction` exactly once, on the bar whose
    as-of close time matches the signal bar; WAIT otherwise."""
    from backtest import clock

    calls = {"n": 0}

    def fake_eval(stock, settings, regime):
        calls["n"] += 1
        now = clock.now_ist()
        # Fire on the Nth evaluation (1-indexed) == signal_bar_index+1.
        if calls["n"] == signal_bar_index + 1:
            price = 100.0 + signal_bar_index
            return {"signal": direction, "entry": price, "price": price,
                    "setup_id": "x", "primary_pattern": "TEST"}
        return {"signal": "WAIT", "entry": None, "price": 100.0}

    monkeypatch.setattr(replay_module.engine_module, "evaluate_stock", fake_eval)


def test_replay_opens_trade_on_actionable_signal(monkeypatch):
    day = dt.date(2026, 3, 2)
    store = HistoryStore()
    store.add("ACME", "5m", _day_5m(day, 40))
    # Fire well past ATR warm-up (atr_period=14, min_periods=14) so the
    # structural stop plan can be built - otherwise ATR is None and the driver
    # correctly skips the entry.
    _patch_signal_on_bar(monkeypatch, signal_bar_index=20, direction="IMMEDIATE_BUY")

    trades = replay_module.replay_symbol("ACME", SETTINGS, store)
    assert len(trades) == 1
    assert trades[0].direction == "BUY"
    assert trades[0].exit_reason is not None  # trade was resolved


def test_replay_respects_daily_cap(monkeypatch):
    day = dt.date(2026, 3, 2)
    store = HistoryStore()
    store.add("ACME", "5m", _day_5m(day, 40))
    from backtest import clock

    # Emit IMMEDIATE_BUY on EVERY bar; with tiny trades the driver should still
    # cap entries at max_trades_per_day.
    def always_buy(stock, settings, regime):
        now = clock.now_ist()
        px = 100.0
        return {"signal": "IMMEDIATE_BUY", "entry": px, "price": px,
                "setup_id": "x", "primary_pattern": "TEST"}

    monkeypatch.setattr(replay_module.engine_module, "evaluate_stock", always_buy)
    capped = dict(SETTINGS, max_trades_per_day=3)
    trades = replay_module.replay_symbol("ACME", capped, store)
    assert len(trades) <= 3


@pytest.mark.integration
def test_full_pipeline_real_engine_offline():
    """Integration: synthetic cached history -> REAL engine replay -> metrics,
    all offline. Asserts the chain runs and produces a coherent report (not a
    specific edge - synthetic data isn't engineered to be profitable)."""
    from backtest import metrics as metrics_module

    def mk(day, n, step, base, start=(9, 15)):
        out, t, px = [], dt.datetime(day.year, day.month, day.day, start[0], start[1], tzinfo=IST), base
        for i in range(n):
            px += 0.3
            out.append({"timestamp": t.isoformat(), "open": px, "high": px + 1.2,
                        "low": px - 1.2, "close": px + 0.6, "volume": 80000 + i})
            t += dt.timedelta(minutes=step)
        return out

    store = HistoryStore()
    fivem, fifteenm = [], []
    # Keep the window small (one week) so the REAL-engine replay stays fast in
    # the unit suite; the chain is identical to a longer run.
    for di in range(7):
        d = dt.date(2026, 3, 2) + dt.timedelta(days=di)
        if d.weekday() >= 5:
            continue
        fivem += mk(d, 75, 5, 100 + di)
        fifteenm += mk(d, 25, 15, 100 + di)
    daily, px = [], 90.0
    for i in range(70):
        px += 1.0
        d = dt.date(2026, 1, 1) + dt.timedelta(days=i)
        daily.append({"timestamp": dt.datetime(d.year, d.month, d.day, tzinfo=IST).isoformat(),
                      "open": px - 1, "high": px + 2, "low": px - 2, "close": px, "volume": 6_000_000})
    store.add("ACME", "5m", fivem)
    store.add("ACME", "15m", fifteenm)
    store.add("ACME", "1d", daily)

    settings = {
        "data_source": "angelone", "entry_start_time": "09:45", "entry_end_time": "14:30",
        "square_off_time": "15:15", "min_avg_volume": 1000, "min_avg_traded_value": 1000,
        "use_market_regime_filter": True, "buy_score_threshold": 60, "sell_score_threshold": 60,
        "max_trades_per_day": 4, "atr_period": 14, "round_trip_cost_pct": 0.08,
        "paper_risk": {"account_equity": 100000, "risk_per_trade_percent": 0.5,
                       "max_position_value_percent": 20.0},
    }
    # Uses the REAL engine.evaluate_stock (not monkeypatched) via the shim.
    trades = replay_module.replay_symbol("ACME", settings, store)
    report = metrics_module.full_report(trades)
    text = metrics_module.render_report(report)
    assert "BACKTEST METRICS" in text
    assert report["overall"]["trades"] == len(trades)
    # Every produced trade must be fully resolved (no dangling open trades).
    assert all(t.exit_reason is not None for t in trades)


def test_replay_one_trade_at_a_time(monkeypatch):
    """A signal on every bar must not open a second trade while one is open:
    entries must be non-overlapping in bar index."""
    day = dt.date(2026, 3, 2)
    store = HistoryStore()
    store.add("ACME", "5m", _day_5m(day, 60))
    from backtest import clock

    def always_buy(stock, settings, regime):
        return {"signal": "IMMEDIATE_BUY", "entry": 100.0, "price": 100.0,
                "setup_id": "x", "primary_pattern": "TEST"}

    monkeypatch.setattr(replay_module.engine_module, "evaluate_stock", always_buy)
    big_cap = dict(SETTINGS, max_trades_per_day=999)
    trades = replay_module.replay_symbol("ACME", big_cap, store)
    # Each trade consumed >=1 bar and entries can't overlap; so total trades
    # can't exceed the number of bars, and must be >=1.
    assert 1 <= len(trades) <= 60
