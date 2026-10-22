"""
Historical candle fetcher + local cache (backtest Task 1).
===========================================================

Pulls N months of daily / 15m / 5m candles per symbol from Angel One by
iterating explicit date-range windows (AngelOneBroker.get_candles_range), then
stitches, de-duplicates, sorts, and caches them to disk as CSV so repeat
backtest runs are fast and fully offline.

Why CSV (not parquet): the project's requirements.txt has pandas but no
pyarrow/fastparquet, so parquet would add a hard dependency. CSV is universal
and plenty for this data volume (a few hundred KB per symbol-interval).

Angel One getCandleData has a documented MAX span per request that varies by
interval (longer bars -> larger allowed span). We chunk each fetch below those
limits with headroom. See MAX_DAYS_PER_REQUEST.

Usage (CLI):
    python -m backtest.history --symbol RELIANCE --months 6
    python -m backtest.history --config intraday_config.json --months 6
    python -m backtest.history --symbol TCS --months 3 --intervals 5m,15m,1d

Usage (programmatic):
    from backtest.history import load_history, fetch_symbol
    df = load_history("RELIANCE", "5m")          # cached read (fetches if absent)
    data = fetch_symbol("RELIANCE", months=6)    # force (re)fetch all intervals
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
from typing import Dict, List, Optional

import pandas as pd

# IST matches the broker's timezone handling.
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# Where cached CSVs live: backtest/data/<SYMBOL>/<interval>.csv
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_THIS_DIR, "data")

# Intervals this fetcher supports (maps to the broker's INTERVALS keys).
SUPPORTED_INTERVALS = ["1d", "15m", "5m"]

# Conservative per-request window sizes (calendar days) chosen BELOW Angel
# One's documented getCandleData maximums, leaving headroom:
#   1m   -> max ~30d   | 5m/3m -> max ~90d  | 15m -> max ~180d
#   30m/1h -> max ~360d | 1d -> max ~2000d
# We use smaller windows so a single throttle/retry never loses much work and
# so the stitched series has frequent, overlapping-safe boundaries.
MAX_DAYS_PER_REQUEST = {
    "1m": 25,
    "3m": 60,
    "5m": 60,
    "15m": 120,
    "30m": 300,
    "1h": 300,
    "1d": 1500,
}

CANDLE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------------
# Cache paths + IO
# ---------------------------------------------------------------------------

def _symbol_dir(symbol: str) -> str:
    return os.path.join(DATA_DIR, symbol.upper().strip())


def cache_path(symbol: str, interval: str) -> str:
    return os.path.join(_symbol_dir(symbol), f"{interval}.csv")


def _read_cache(symbol: str, interval: str) -> Optional[pd.DataFrame]:
    path = cache_path(symbol, interval)
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path)
        if df.empty or "timestamp" not in df.columns:
            return None
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=False)
        return df
    except Exception:
        return None


def _write_cache(symbol: str, interval: str, df: pd.DataFrame) -> str:
    path = cache_path(symbol, interval)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    out = df.copy()
    # Store timestamps as ISO strings for stable round-tripping.
    out["timestamp"] = out["timestamp"].apply(
        lambda t: t.isoformat() if isinstance(t, (pd.Timestamp, dt.datetime)) else str(t)
    )
    tmp = path + ".tmp"
    out.to_csv(tmp, index=False)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Chunking + stitching
# ---------------------------------------------------------------------------

def _date_windows(start: dt.datetime, end: dt.datetime, max_days: int) -> List[tuple]:
    """Split [start, end] into consecutive <= max_days windows (inclusive)."""
    windows = []
    cur = start
    step = dt.timedelta(days=max_days)
    while cur < end:
        w_end = min(cur + step, end)
        windows.append((cur, w_end))
        cur = w_end + dt.timedelta(minutes=1)
    return windows


def _candles_to_df(candles: List[dict]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(columns=CANDLE_COLUMNS)
    df = pd.DataFrame(candles)
    for col in CANDLE_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[CANDLE_COLUMNS]
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=False)
    return df


def _stitch(frames: List[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate window frames, drop duplicate timestamps (overlap-safe), sort."""
    non_empty = [f for f in frames if f is not None and not f.empty]
    if not non_empty:
        return pd.DataFrame(columns=CANDLE_COLUMNS)
    df = pd.concat(non_empty, ignore_index=True)
    df = df.drop_duplicates(subset=["timestamp"], keep="last")
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _default_broker():
    """Lazily build the shared, authenticated read-only broker client.

    Imported lazily so unit tests can run fully offline by passing their own
    fetch callable / broker double.
    """
    from angel_one_api import create_client
    return create_client()


def fetch_interval(symbol: str, interval: str, months: int, exchange: str = "nse",
                   broker=None, end: Optional[dt.datetime] = None,
                   pause: float = 0.0) -> pd.DataFrame:
    """Fetch `months` of history for one (symbol, interval) by iterating
    chunked date windows through the broker, then stitch + cache.

    broker: anything exposing get_candles_range(symbol, interval, start, end,
            exchange) -> {"candles": [...]}. Defaults to the live Angel One
            client. Injectable for offline testing.
    """
    if interval not in MAX_DAYS_PER_REQUEST:
        raise ValueError(f"Unsupported interval for history fetch: {interval}")
    if broker is None:
        broker = _default_broker()
    if end is None:
        end = dt.datetime.now(IST)
    start = end - dt.timedelta(days=int(months * 31))

    frames: List[pd.DataFrame] = []
    for w_start, w_end in _date_windows(start, end, MAX_DAYS_PER_REQUEST[interval]):
        try:
            res = broker.get_candles_range(symbol, interval, w_start, w_end, exchange)
            frames.append(_candles_to_df(res.get("candles", [])))
        except Exception as exc:  # keep partial history rather than aborting the whole run
            print(f"  [warn] {symbol} {interval} {w_start.date()}..{w_end.date()}: {exc}")
        if pause:
            time.sleep(pause)

    df = _stitch(frames)
    if not df.empty:
        _write_cache(symbol, interval, df)
    return df


def fetch_symbol(symbol: str, months: int = 6, intervals: Optional[List[str]] = None,
                 exchange: str = "nse", broker=None) -> Dict[str, pd.DataFrame]:
    """Fetch + cache all requested intervals for one symbol."""
    intervals = intervals or SUPPORTED_INTERVALS
    out: Dict[str, pd.DataFrame] = {}
    for interval in intervals:
        print(f"[fetch] {symbol} {interval} ({months} months)...")
        df = fetch_interval(symbol, interval, months, exchange=exchange, broker=broker)
        out[interval] = df
        print(f"  -> {len(df)} candles cached at {cache_path(symbol, interval)}")
    return out


def load_history(symbol: str, interval: str, months: int = 6,
                 refresh: bool = False, broker=None) -> pd.DataFrame:
    """Return cached candles for (symbol, interval), fetching if absent/refresh."""
    if not refresh:
        cached = _read_cache(symbol, interval)
        if cached is not None and not cached.empty:
            return cached
    return fetch_interval(symbol, interval, months, broker=broker)


def candles_as_dicts(df: pd.DataFrame) -> List[dict]:
    """Convert a cached DataFrame back into the engine's candle-dict shape
    (ISO timestamp strings), so the replay harness can feed engine functions."""
    if df is None or df.empty:
        return []
    recs = []
    for _, row in df.iterrows():
        ts = row["timestamp"]
        recs.append({
            "timestamp": ts.isoformat() if isinstance(ts, (pd.Timestamp, dt.datetime)) else str(ts),
            "open": float(row["open"]), "high": float(row["high"]),
            "low": float(row["low"]), "close": float(row["close"]),
            "volume": int(float(row["volume"])) if pd.notna(row["volume"]) else 0,
        })
    return recs


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _symbols_from_config(config_path: str) -> List[str]:
    with open(config_path) as f:
        cfg = json.load(f)
    return [s["symbol"] for s in cfg.get("stocks", []) if s.get("symbol")]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch + cache Angel One historical candles for backtesting.")
    ap.add_argument("--symbol", help="Single symbol to fetch (e.g. RELIANCE)")
    ap.add_argument("--config", help="intraday_config.json to pull the stock universe from")
    ap.add_argument("--months", type=int, default=6, help="Months of history (default 6)")
    ap.add_argument("--intervals", default=",".join(SUPPORTED_INTERVALS),
                    help="Comma-separated intervals (default 5m,15m,1d)")
    ap.add_argument("--exchange", default="nse")
    args = ap.parse_args(argv)

    intervals = [i.strip() for i in args.intervals.split(",") if i.strip()]
    symbols: List[str] = []
    if args.symbol:
        symbols.append(args.symbol)
    if args.config:
        symbols.extend(_symbols_from_config(args.config))
    if not symbols:
        ap.error("provide --symbol and/or --config")

    seen = set()
    for sym in symbols:
        if sym.upper() in seen:
            continue
        seen.add(sym.upper())
        fetch_symbol(sym, months=args.months, intervals=intervals, exchange=args.exchange)

    print(f"\nDone. Cache dir: {DATA_DIR}")


if __name__ == "__main__":
    main()
