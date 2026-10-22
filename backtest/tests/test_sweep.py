"""Tests for the parameter sweep runner (Task 8). Fast + offline."""
import datetime as dt

import pytest

from backtest import sweep


# ---------------------------------------------------------------------------
# Variant generation
# ---------------------------------------------------------------------------

def test_default_variants_include_baseline_and_tiered():
    baseline = {"gate_mode": "strict", "booster_penalty": 7, "min_rvol": 1.3, "min_rr": 1.8,
                "buy_score_threshold": 60, "sell_score_threshold": 60}
    variants = dict(sweep.default_variants(baseline))
    assert "baseline" in variants
    assert "tiered" in variants
    assert variants["baseline"]["gate_mode"] == "strict"
    assert variants["tiered"]["gate_mode"] == "tiered"
    # One-factor-at-a-time: baseline must be untouched (deep-copied).
    assert baseline["gate_mode"] == "strict"


def test_variant_overrides_are_isolated():
    baseline = {"gate_mode": "strict", "booster_penalty": 7}
    variants = dict(sweep.default_variants(baseline))
    assert variants["tiered_boost15"]["booster_penalty"] == 15
    # Changing one variant's dict must not bleed into another.
    assert variants["tiered_boost3"]["booster_penalty"] == 3


# ---------------------------------------------------------------------------
# Train/validation split
# ---------------------------------------------------------------------------

def test_split_dates_halves_window():
    lo = dt.date(2026, 1, 1)
    hi = dt.date(2026, 3, 1)
    train_end, valid_start = sweep.split_dates(lo, hi)
    assert lo <= train_end < valid_start <= hi
    assert valid_start == train_end + dt.timedelta(days=1)


# ---------------------------------------------------------------------------
# Recommendation logic
# ---------------------------------------------------------------------------

def _m(exp, tpd, pf=2.0, trades=10, wr=0.5):
    return {"expectancy_r": exp, "trades_per_day": tpd, "profit_factor": pf,
            "trades": trades, "win_rate": wr}


def test_recommend_picks_best_robust_positive_variant():
    result = {"_window": {}, "variants": {
        # Negative expectancy -> excluded.
        "A": {"full": _m(-0.1, 2.0), "train": _m(-0.1, 2.0), "valid": _m(-0.2, 2.0)},
        # Positive but too few trades/day -> excluded (< 1.0).
        "B": {"full": _m(0.5, 0.3), "train": _m(0.5, 0.3), "valid": _m(0.4, 0.3)},
        # Positive + enough trades + robust on valid -> candidate.
        "C": {"full": _m(0.3, 1.5), "train": _m(0.3, 1.5), "valid": _m(0.2, 1.5)},
        # Higher expectancy + robust -> should WIN.
        "D": {"full": _m(0.6, 1.2), "train": _m(0.5, 1.2), "valid": _m(0.4, 1.2)},
    }}
    rec = sweep.recommend(result, min_trades_per_day=1.0)
    assert "'D'" in rec


def test_recommend_rejects_overfit_variant():
    # Strong on full+train but NEGATIVE on validation -> not robust -> excluded.
    result = {"_window": {}, "variants": {
        "overfit": {"full": _m(0.9, 2.0), "train": _m(1.1, 2.0), "valid": _m(-0.3, 2.0)},
    }}
    rec = sweep.recommend(result, min_trades_per_day=1.0)
    assert "no variant met" in rec.lower()


def test_recommend_none_when_nothing_qualifies():
    result = {"_window": {}, "variants": {
        "A": {"full": _m(-0.5, 3.0), "train": None, "valid": None},
    }}
    rec = sweep.recommend(result, min_trades_per_day=1.0)
    assert "no variant met" in rec.lower()
    # It must warn against loosening gates to force trades.
    assert "destroys expectancy" in rec.lower() or "do not loosen" in rec.lower()


def test_render_sweep_runs():
    result = {"_window": {"start": "2026-01-01", "end": "2026-03-01",
                          "train_end": "2026-01-30", "valid_start": "2026-01-31"},
              "variants": {
                  "baseline": {"full": _m(0.1, 0.8), "train": _m(0.1, 0.8), "valid": _m(0.05, 0.8)},
                  "tiered": {"full": _m(0.2, 1.5), "train": _m(0.2, 1.5), "valid": _m(0.15, 1.5)},
              }}
    text = sweep.render_sweep(result)
    assert "PARAMETER SWEEP" in text
    assert "VALIDATION HALF" in text
    assert "RECOMMENDATION" in text


# ---------------------------------------------------------------------------
# Reproducibility (offline, seeded store, real replay but tiny)
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_sweep_reproducible_on_seeded_data():
    """Same cached data + same variants -> identical metrics on repeat runs.
    Marked integration because it runs real replays (slow)."""
    import datetime as dt
    from backtest.data_shim import HistoryStore

    IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

    def mk(day, n, step, base):
        out, t, px = [], dt.datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST), base
        for i in range(n):
            px += 0.3
            out.append({"timestamp": t.isoformat(), "open": px, "high": px + 1,
                        "low": px - 1, "close": px + 0.5, "volume": 80000 + i})
            t += dt.timedelta(minutes=step)
        return out

    store = HistoryStore()
    fivem, fifteenm = [], []
    for di in range(6):
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

    baseline = {"data_source": "angelone", "entry_start_time": "09:45", "entry_end_time": "14:30",
                "square_off_time": "15:15", "min_avg_volume": 1000, "min_avg_traded_value": 1000,
                "buy_score_threshold": 60, "sell_score_threshold": 60, "atr_period": 14,
                "round_trip_cost_pct": 0.08,
                "paper_risk": {"account_equity": 100000, "risk_per_trade_percent": 0.5,
                               "max_position_value_percent": 20.0}}
    variants = [("baseline", baseline), ("tiered", dict(baseline, gate_mode="tiered"))]

    r1 = sweep.run_sweep(["ACME"], baseline, variants=variants, split=False, store=store)
    r2 = sweep.run_sweep(["ACME"], baseline, variants=variants, split=False, store=store)
    assert r1["variants"]["baseline"]["full"] == r2["variants"]["baseline"]["full"]
    assert r1["variants"]["tiered"]["full"] == r2["variants"]["tiered"]["full"]
