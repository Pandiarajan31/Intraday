"""
Backtest DataProvider shim (backtest Task 2 - the look-ahead guard).
====================================================================

During a backtest, engine.evaluate_stock() and positions.py call
DataProvider.get_ohlcv() / get_realtime_quote(), which normally hit the live
Angel One network. This module swaps those two methods for in-memory readers
that serve CACHED historical candles (backtest/history.py) TRUNCATED to the
active as-of instant (backtest/clock.py).

THIS is where look-ahead bias is actually prevented: no candle whose timestamp
is strictly after the as-of instant is ever returned, and the synthetic "quote"
(last_price) is taken from the most recent candle at/before the as-of instant -
never from a future bar.

Design:
  - HistoryStore holds, per (symbol, interval), the full cached candle list
    (ascending). A read slices candles with timestamp <= as_of.
  - install()/restore() (or the `use_history` context manager) monkeypatch
    DataProvider.get_ohlcv and get_realtime_quote for the duration of a run.
  - Index symbols (^NSEI/^NSEBANK) are served from the store too if cached; if
    an index isn't cached, get_ohlcv returns an empty result so the engine's
    regime detection fails OPEN (its existing, documented behavior) rather than
    leaking live data into a historical run.
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from typing import Dict, List, Optional

import pandas as pd

from backtest import clock as _clock
from backtest import history as _history

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# Map the engine's named periods to a candle count is unnecessary here; the
# engine passes `limit`, and we already hold full history, so we just honor
# `limit` (tail N) after truncating to as-of.


def _parse_ts(ts) -> dt.datetime:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize(IST)
    return t.to_pydatetime()


class HistoryStore:
    """In-memory per-(symbol, interval) candle cache for a backtest run.

    Candles are stored ascending by timestamp. `slice_as_of` returns only
    candles at/before the given instant (look-ahead guard).
    """

    def __init__(self):
        # key: (SYMBOL, interval) -> list[candle dict] ascending
        self._store: Dict[tuple, List[dict]] = {}

    def load_symbol(self, symbol: str, intervals=None) -> bool:
        """Load a symbol's candles from the LOCAL cache only (never fetches /
        touches the network). Returns True if any 5m candles were found, False
        if the symbol has no cache (so callers can warn instead of crashing).

        Reading the cache directly - rather than via history.load_history, which
        falls back to a live fetch when the cache is absent - keeps the backtest
        strictly offline and deterministic.
        """
        intervals = intervals or _history.SUPPORTED_INTERVALS
        found_any = False
        for interval in intervals:
            df = _history._read_cache(symbol, interval)  # cache-only, no fetch
            candles = _history.candles_as_dicts(df) if df is not None else []
            self._store[(symbol.upper(), interval)] = candles
            if interval == "5m" and candles:
                found_any = True
        return found_any

    def add(self, symbol: str, interval: str, candles: List[dict]):
        """Directly seed candles (used by tests to avoid any disk/network)."""
        self._store[(symbol.upper(), interval)] = sorted(
            candles, key=lambda c: _parse_ts(c["timestamp"]))

    def has(self, symbol: str, interval: str) -> bool:
        return (symbol.upper(), interval) in self._store

    def slice_as_of(self, symbol: str, interval: str, as_of: dt.datetime,
                    limit: Optional[int] = None) -> List[dict]:
        candles = self._store.get((symbol.upper(), interval), [])
        if not candles:
            return []
        cut = [c for c in candles if _parse_ts(c["timestamp"]) <= as_of]
        if limit and len(cut) > limit:
            cut = cut[-limit:]
        return cut


# Period->interval map for normalizing the engine's get_ohlcv(interval) arg
# to our stored interval keys. The engine uses "1d","15m","5m".
_INTERVAL_PASSTHROUGH = {"1d", "15m", "5m", "1m", "30m", "1h"}


def _make_get_ohlcv(store: HistoryStore):
    def _get_ohlcv(symbol, period="5d", interval="15m", exchange="nse", limit=100):
        norm = symbol.upper().strip().replace(".NS", "").replace(".BO", "")
        as_of = _clock.now_ist()
        key_interval = interval if interval in _INTERVAL_PASSTHROUGH else interval
        if not store.has(norm, key_interval):
            # Not cached (e.g. an index we didn't fetch): return empty, no error,
            # so the engine's regime/aux paths degrade gracefully (fail-open),
            # exactly as they do live when a benchmark fetch returns little data.
            return {"symbol": norm, "symbol_input": symbol.upper(), "exchange": exchange.upper(),
                    "market": "INDIA", "interval": interval, "candles": [], "count": 0,
                    "source": "backtest"}
        candles = store.slice_as_of(norm, key_interval, as_of, limit=limit)
        return {"symbol": norm, "symbol_input": symbol.upper(), "exchange": exchange.upper(),
                "market": "INDIA", "interval": interval, "count": len(candles),
                "total_candles": len(candles), "candles": candles, "source": "backtest"}
    return _get_ohlcv


def _make_get_quote(store: HistoryStore):
    def _get_quote(symbol, exchange="nse"):
        norm = symbol.upper().strip().replace(".NS", "").replace(".BO", "")
        as_of = _clock.now_ist()
        # Derive LTP from the most recent 5m candle at/before as-of (fall back
        # to 15m, then 1d). The timestamp is the as-of instant itself so the
        # engine's data-freshness gate sees "fresh" simulated data.
        for interval in ("5m", "15m", "1d"):
            candles = store.slice_as_of(norm, interval, as_of, limit=1)
            if candles:
                last = candles[-1]
                return {"symbol": norm, "symbol_input": symbol.upper(),
                        "exchange": exchange.upper(), "market": "INDIA",
                        "last_price": float(last["close"]),
                        "timestamp": as_of.astimezone(IST).isoformat(),
                        "source": "backtest"}
        return {"symbol": norm, "symbol_input": symbol.upper(), "exchange": exchange.upper(),
                "market": "INDIA", "error": "no backtest data at as-of", "source": "backtest"}
    return _get_quote


# ---------------------------------------------------------------------------
# Install / restore
# ---------------------------------------------------------------------------

_saved = {}


def install(store: HistoryStore):
    """Monkeypatch DataProvider.get_ohlcv / get_realtime_quote to read from the
    store (truncated to as-of). Idempotent-safe: saves originals once."""
    from shared import DataProvider
    if not _saved:
        _saved["get_ohlcv"] = DataProvider.get_ohlcv
        _saved["get_realtime_quote"] = DataProvider.get_realtime_quote
    DataProvider.get_ohlcv = staticmethod(_make_get_ohlcv(store))
    DataProvider.get_realtime_quote = staticmethod(_make_get_quote(store))


def restore():
    """Restore the original live DataProvider methods."""
    from shared import DataProvider
    if _saved:
        DataProvider.get_ohlcv = _saved["get_ohlcv"]
        DataProvider.get_realtime_quote = _saved["get_realtime_quote"]
        _saved.clear()


@contextmanager
def use_history(store: HistoryStore):
    """Context manager: serve engine data from `store` for the duration."""
    install(store)
    try:
        yield store
    finally:
        restore()
