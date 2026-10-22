"""Offline unit tests for backtest.history (Task 1).

No network / credentials: a FakeBroker generates deterministic candles for any
requested date range, so we can assert chunking, stitching (dedup/overlap/gaps),
and cache round-tripping behave correctly.
"""
import datetime as dt
import os

import pandas as pd
import pytest

from backtest import history


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


class FakeBroker:
    """Generates one 5m candle per interval-step across [start, end], and
    records the windows it was asked for so chunking can be asserted."""

    STEP = {"5m": dt.timedelta(minutes=5), "15m": dt.timedelta(minutes=15),
            "1d": dt.timedelta(days=1)}

    def __init__(self):
        self.calls = []

    def get_candles_range(self, symbol, interval, start, end, exchange="nse"):
        self.calls.append((start, end))
        step = self.STEP[interval]
        if start.tzinfo is None:
            start = start.replace(tzinfo=IST)
        if end.tzinfo is None:
            end = end.replace(tzinfo=IST)
        candles = []
        t = start
        price = 100.0
        while t <= end:
            # Skip weekends for intraday realism (not strictly required).
            candles.append({
                "timestamp": t.isoformat(), "open": price, "high": price + 1,
                "low": price - 1, "close": price + 0.5, "volume": 1000,
            })
            price += 0.1
            t += step
        return {"candles": candles}


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def test_date_windows_cover_range_without_gaps_or_overlaps():
    start = dt.datetime(2026, 1, 1, tzinfo=IST)
    end = dt.datetime(2026, 4, 1, tzinfo=IST)
    windows = history._date_windows(start, end, max_days=30)
    # Windows are consecutive: each start is 1 minute after the previous end.
    assert windows[0][0] == start
    assert windows[-1][1] == end
    for (s0, e0), (s1, e1) in zip(windows, windows[1:]):
        assert s1 == e0 + dt.timedelta(minutes=1)
    # No window exceeds the max span.
    for s, e in windows:
        assert (e - s) <= dt.timedelta(days=30)


def test_fetch_interval_chunks_long_span():
    broker = FakeBroker()
    # 5m max window is 60 days; 6 months (~186d) must be split into >= 3 chunks.
    history.fetch_interval("TESTSYM", "5m", months=6, broker=broker,
                           end=dt.datetime(2026, 6, 30, tzinfo=IST))
    assert len(broker.calls) >= 3


# ---------------------------------------------------------------------------
# Stitching
# ---------------------------------------------------------------------------

def test_stitch_dedupes_overlapping_timestamps():
    ts = dt.datetime(2026, 1, 1, 9, 15, tzinfo=IST)
    df1 = history._candles_to_df([
        {"timestamp": ts.isoformat(), "open": 1, "high": 2, "low": 0, "close": 1.5, "volume": 10},
    ])
    df2 = history._candles_to_df([
        {"timestamp": ts.isoformat(), "open": 9, "high": 9, "low": 9, "close": 9, "volume": 99},
    ])
    stitched = history._stitch([df1, df2])
    assert len(stitched) == 1  # duplicate timestamp collapsed
    assert stitched.iloc[0]["close"] == 9  # keep="last" wins


def test_stitch_sorts_ascending():
    t0 = dt.datetime(2026, 1, 1, 9, 15, tzinfo=IST)
    t1 = dt.datetime(2026, 1, 1, 9, 20, tzinfo=IST)
    later = history._candles_to_df([{"timestamp": t1.isoformat(), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}])
    earlier = history._candles_to_df([{"timestamp": t0.isoformat(), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}])
    stitched = history._stitch([later, earlier])
    assert list(stitched["timestamp"]) == sorted(stitched["timestamp"])


# ---------------------------------------------------------------------------
# Cache round-trip
# ---------------------------------------------------------------------------

def test_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DATA_DIR", str(tmp_path))
    broker = FakeBroker()
    df = history.fetch_interval("ACME", "15m", months=1, broker=broker,
                                end=dt.datetime(2026, 2, 1, tzinfo=IST))
    assert not df.empty
    assert os.path.exists(history.cache_path("ACME", "15m"))

    # A second load with refresh=False must read the cache, NOT call the broker.
    broker2 = FakeBroker()
    cached = history.load_history("ACME", "15m", refresh=False, broker=broker2)
    assert len(cached) == len(df)
    assert broker2.calls == []  # cache hit -> no fetch


def test_candles_as_dicts_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DATA_DIR", str(tmp_path))
    broker = FakeBroker()
    df = history.fetch_interval("ACME", "5m", months=1, broker=broker,
                                end=dt.datetime(2026, 2, 1, tzinfo=IST))
    recs = history.candles_as_dicts(df)
    assert recs and set(recs[0]) == {"timestamp", "open", "high", "low", "close", "volume"}
    assert isinstance(recs[0]["volume"], int)
