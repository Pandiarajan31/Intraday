"""Offline tests for the trade simulator + replay driver (Task 3)."""
import datetime as dt

import pandas as pd
import pytest

from backtest.simulator import simulate_trade

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

SETTINGS = {
    "round_trip_cost_pct": 0.0,   # zero costs so R math is exact in tests
    "trail_lookback_candles": 3,
    "time_stop_minutes": 30,
    "time_stop_min_progress_r": 0.5,
    "square_off_time": "15:15",
}


def _c(day, hhmm, o, h, l, cl, v=1000):
    t = dt.datetime(day.year, day.month, day.day, hhmm[0], hhmm[1], tzinfo=IST)
    return {"timestamp": t.isoformat(), "open": o, "high": h, "low": l, "close": cl, "volume": v}


def _plan(entry, stop, risk):
    sign = 1 if stop < entry else -1
    return {"stop_loss": stop, "risk_per_share": risk,
            "t1": entry + sign * risk, "t2": entry + sign * 2 * risk,
            "t3": entry + sign * 3 * risk, "stop_atr": 1.0, "too_wide": False}


DAY = dt.date(2026, 3, 2)


# ---------------------------------------------------------------------------
# Known winner: BUY runs straight to T3.
# ---------------------------------------------------------------------------

def test_buy_hits_t3_winner():
    entry, stop, risk = 100.0, 99.0, 1.0  # T1=101, T2=102, T3=103
    future = [
        _c(DAY, (10, 5), 100, 100.5, 99.8, 100.4),
        _c(DAY, (10, 10), 100.4, 101.2, 100.3, 101.1),  # T1 touched
        _c(DAY, (10, 15), 101.1, 102.1, 101.0, 102.0),  # T2 touched
        _c(DAY, (10, 20), 102.0, 103.5, 101.9, 103.2),  # T3 touched -> exit
    ]
    t = simulate_trade("ACME", "BUY", dt.datetime(2026, 3, 2, 10, 5, tzinfo=IST),
                       entry, _plan(entry, stop, risk), quantity=100,
                       future_candles=future, settings=SETTINGS)
    assert t.exit_reason == "TARGET_3"
    assert t.booked_half is True
    # Half booked at T1 (+1R on 50) + half at T3 (+3R on 50) = net +2R on 100.
    assert t.r_multiple == pytest.approx(2.0, abs=0.001)
    assert t.net_pnl > 0


# ---------------------------------------------------------------------------
# Known loser: BUY stops out immediately, no T1.
# ---------------------------------------------------------------------------

def test_buy_stops_out_loser():
    entry, stop, risk = 100.0, 99.0, 1.0
    future = [
        _c(DAY, (10, 5), 100, 100.2, 98.5, 98.8),  # low 98.5 <= stop 99 -> stop
    ]
    t = simulate_trade("ACME", "BUY", dt.datetime(2026, 3, 2, 10, 5, tzinfo=IST),
                       entry, _plan(entry, stop, risk), quantity=100,
                       future_candles=future, settings=SETTINGS)
    assert t.exit_reason == "STOP_LOSS"
    assert t.booked_half is False
    assert t.r_multiple == pytest.approx(-1.0, abs=0.001)
    assert t.net_pnl < 0


# ---------------------------------------------------------------------------
# T1 then breakeven stop: after booking half at T1, a pullback to entry exits
# the rest at breakeven -> net +0.5R (the booked half), not a loss.
# ---------------------------------------------------------------------------

def test_buy_t1_then_breakeven():
    entry, stop, risk = 100.0, 99.0, 1.0  # T1=101
    future = [
        _c(DAY, (10, 5), 100, 101.2, 99.9, 101.0),   # T1 touched -> book half, stop->100
        _c(DAY, (10, 10), 101.0, 101.1, 99.8, 99.9),  # low 99.8 <= breakeven 100 -> exit rest
    ]
    t = simulate_trade("ACME", "BUY", dt.datetime(2026, 3, 2, 10, 5, tzinfo=IST),
                       entry, _plan(entry, stop, risk), quantity=100,
                       future_candles=future, settings=SETTINGS)
    assert t.booked_half is True
    assert t.exit_reason == "TRAIL_OR_BREAKEVEN_STOP"
    # Half at +1R, half at 0R -> net +0.5R.
    assert t.r_multiple == pytest.approx(0.5, abs=0.001)


# ---------------------------------------------------------------------------
# Square-off forces exit at/after 15:15.
# ---------------------------------------------------------------------------

def test_square_off_forces_exit():
    entry, stop, risk = 100.0, 99.0, 1.0
    future = [
        _c(DAY, (15, 10), 100, 100.3, 99.8, 100.1),
        _c(DAY, (15, 15), 100.1, 100.4, 99.9, 100.2),  # >= 15:15 -> square off
    ]
    t = simulate_trade("ACME", "BUY", dt.datetime(2026, 3, 2, 15, 10, tzinfo=IST),
                       entry, _plan(entry, stop, risk), quantity=100,
                       future_candles=future, settings=SETTINGS)
    assert t.exit_reason == "SQUARE_OFF"


# ---------------------------------------------------------------------------
# SELL winner mirror.
# ---------------------------------------------------------------------------

def test_sell_hits_t3_winner():
    entry, stop, risk = 100.0, 101.0, 1.0  # T1=99, T2=98, T3=97
    future = [
        _c(DAY, (10, 5), 100, 100.2, 98.9, 99.0),    # T1 touched
        _c(DAY, (10, 10), 99.0, 99.1, 96.8, 97.0),   # T3 touched -> exit
    ]
    t = simulate_trade("ACME", "SELL", dt.datetime(2026, 3, 2, 10, 5, tzinfo=IST),
                       entry, _plan(entry, stop, risk), quantity=100,
                       future_candles=future, settings=SETTINGS)
    assert t.exit_reason == "TARGET_3"
    assert t.r_multiple == pytest.approx(2.0, abs=0.001)


# ---------------------------------------------------------------------------
# Time stop: no progress within time_stop_minutes before T1.
# ---------------------------------------------------------------------------

def test_time_stop_triggers():
    entry, stop, risk = 100.0, 99.0, 1.0
    # Flat bars for 35 minutes, never reaching 0.5R (=0.5 above entry).
    future = [_c(DAY, (10, 5 + 5 * k), 100, 100.1, 99.9, 100.0) for k in range(8)]
    t = simulate_trade("ACME", "BUY", dt.datetime(2026, 3, 2, 10, 5, tzinfo=IST),
                       entry, _plan(entry, stop, risk), quantity=100,
                       future_candles=future, settings=SETTINGS)
    assert t.exit_reason == "TIME_STOP"
