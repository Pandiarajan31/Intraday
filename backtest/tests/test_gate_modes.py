"""Tests for the tiered gate model (Task 6).

Verifies that:
  - strict mode (default) keeps ALL conditions mandatory (legacy behavior).
  - tiered mode blocks ONLY on must-have failures; a demoted booster failure
    does not flip buy_hard_gate/sell_hard_gate to False, it only penalizes the
    score (so a borderline setup can still qualify, a bad one still can't).

These run the REAL engine.evaluate_stock under the as-of shim, offline.
"""
import datetime as dt

import pytest

from backtest import clock, data_shim
from backtest.data_shim import HistoryStore

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _seed(store, sym="ACME", trend=0.3):
    def mk(day, n, step, base, start=(9, 15)):
        out, t, px = [], dt.datetime(day.year, day.month, day.day, start[0], start[1], tzinfo=IST), base
        for i in range(n):
            px += trend
            out.append({"timestamp": t.isoformat(), "open": px, "high": px + 1.2,
                        "low": px - 1.2, "close": px + 0.6, "volume": 80000 + i})
            t += dt.timedelta(minutes=step)
        return out
    fivem, fifteenm = [], []
    for di in range(10):
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
    store.add(sym, "5m", fivem)
    store.add(sym, "15m", fifteenm)
    store.add(sym, "1d", daily)


BASE_SETTINGS = {
    "data_source": "angelone", "entry_start_time": "09:45", "entry_end_time": "14:30",
    "square_off_time": "15:15", "min_avg_volume": 1000, "min_avg_traded_value": 1000,
    "use_market_regime_filter": True, "buy_score_threshold": 60, "sell_score_threshold": 60,
    "atr_period": 14,
}


def _evaluate(settings, as_of=dt.datetime(2026, 3, 13, 11, 0, tzinfo=IST)):
    import engine
    store = HistoryStore()
    _seed(store)
    regime = {"regime": "RANGE", "reason": "test"}
    with data_shim.use_history(store):
        with clock.as_of(as_of):
            return engine.evaluate_stock({"symbol": "ACME"}, settings, regime)


def test_strict_mode_is_default_and_uses_all_keys():
    rec = _evaluate(dict(BASE_SETTINGS))  # no gate_mode -> strict
    assert rec["gate_mode"] == "strict"
    assert rec["buy_booster_keys"] == []
    assert rec["sell_booster_keys"] == []


def test_tiered_mode_demotes_boosters():
    rec = _evaluate(dict(BASE_SETTINGS, gate_mode="tiered"))
    assert rec["gate_mode"] == "tiered"
    # Boosters must include conditions demoted from the hard gate.
    for demoted in ("market_regime", "candle_quality", "volume", "overextension"):
        assert demoted in rec["buy_booster_keys"], f"{demoted} should be a booster in tiered mode"
    # Must-haves must NOT appear in boosters.
    for must in ("liquidity", "session", "daily_trend", "15m_trend", "vwap"):
        assert must not in rec["buy_booster_keys"]


def test_tiered_hard_gate_not_blocked_by_booster_only_failure():
    """If every MUST-HAVE passes but some BOOSTER fails, the hard gate must be
    OPEN in tiered mode (whereas strict mode would block it)."""
    import engine
    store = HistoryStore()
    _seed(store)
    regime = {"regime": "RANGE", "reason": "test"}
    as_of = dt.datetime(2026, 3, 13, 11, 0, tzinfo=IST)

    with data_shim.use_history(store):
        with clock.as_of(as_of):
            strict = engine.evaluate_stock({"symbol": "ACME"}, dict(BASE_SETTINGS), regime)
        with clock.as_of(as_of):
            tiered = engine.evaluate_stock({"symbol": "ACME"}, dict(BASE_SETTINGS, gate_mode="tiered"), regime)

    # Identify which must-haves fail in tiered mode. If NONE fail, the tiered
    # hard gate should be open even though strict is closed (unless strict is
    # also only failing on must-haves).
    tiered_must_fail = tiered["buy_failed_conditions"]
    if not tiered_must_fail:
        # All must-haves pass -> tiered gate open; strict may be closed due to
        # a booster. This is the core behavioral difference we want.
        assert tiered["buy_hard_gate"] is True
        # strict failing where tiered passes proves boosters were the blocker
        if not strict["buy_hard_gate"]:
            assert set(strict["buy_failed_conditions"]) <= set(tiered["buy_booster_keys"]) | set(tiered_must_fail)
    else:
        # Some must-have genuinely fails -> both modes block (correct; we do
        # NOT force trades). Assert tiered is also closed.
        assert tiered["buy_hard_gate"] is False


def test_tiered_booster_failures_recorded_and_penalize_score():
    """Failed boosters are recorded, and when present the score reflects a
    penalty (score summary mentions boosters)."""
    rec = _evaluate(dict(BASE_SETTINGS, gate_mode="tiered", booster_penalty=10))
    # buy_failed_boosters is a list (possibly empty); if non-empty, the score
    # summary should mention the penalty.
    assert isinstance(rec["buy_failed_boosters"], list)
    if rec["buy_failed_boosters"]:
        assert any("Boosters failed" in s for s in rec["score_summary"])


def test_custom_must_have_set_respected():
    rec = _evaluate(dict(BASE_SETTINGS, gate_mode="tiered",
                         hard_gate_must_haves=["session", "atr"]))
    # With a tiny must-have set, liquidity/daily_trend become boosters.
    assert "liquidity" in rec["buy_booster_keys"]
    assert "daily_trend" in rec["buy_booster_keys"]
    # breakout is always forced into the must-have set.
    assert "breakout" not in rec["buy_booster_keys"]


@pytest.mark.integration
def test_tiered_mode_produces_at_least_as_many_trades_as_strict():
    """The whole point of Task 6 (your 'I never get a call' problem): tiered
    mode must open AT LEAST as many trades as strict over the same history,
    because it only relaxes blocking - it never adds a new blocker. Uses the
    REAL engine replay offline."""
    from backtest import replay as replay_module
    store = HistoryStore()
    _seed(store)
    settings_strict = dict(BASE_SETTINGS,
                           paper_risk={"account_equity": 100000, "risk_per_trade_percent": 0.5,
                                       "max_position_value_percent": 20.0},
                           max_trades_per_day=99, round_trip_cost_pct=0.08)
    settings_tiered = dict(settings_strict, gate_mode="tiered")

    strict_trades = replay_module.replay_symbol("ACME", settings_strict, store)
    tiered_trades = replay_module.replay_symbol("ACME", settings_tiered, store)
    # Tiered relaxes gates, so it can only produce >= strict's count.
    assert len(tiered_trades) >= len(strict_trades)
