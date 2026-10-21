"""
Shared module for Intraday Trading System
=========================================
Provides DataProvider and Indicators classes for fetching Indian stock market data
and computing technical indicators. Used by both the MCP server and the dashboard.

Optimized for intraday trading with multi-timeframe analysis (5m primary, 15m secondary).
"""
import json
import os
import time
import zoneinfo
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


def _ensure_env_loaded() -> None:
    """
    Load credentials/secrets from the project .env file into os.environ.

    The project relies on Angel One credentials (ANGELONE_API_KEY,
    ANGELONE_CLIENT_ID, ANGELONE_PIN, ANGELONE_TOTP_SECRET) and
    INTRADAY_DATA_SOURCE. These live in .env, which is never auto-loaded by
    Python. If this step is skipped, every data fetch fails with
    "Missing environment variables", which surfaces downstream as
    daily_data_missing / 15m_data_missing / 5m_data_missing even when the
    market and broker are perfectly fine. Calling load_dotenv() is idempotent
    and only sets variables that are not already present.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        # python-dotenv not installed / .env missing: rely on real env vars.
        pass


# Load .env as soon as this shared module is imported (covers MCP server,
# dashboard, and engine which all import from here).
_ensure_env_loaded()


def _session_today(tz: "zoneinfo.ZoneInfo"):
    """Today's date in `tz`, honoring an active backtest as-of clock so the
    session-only VWAP filter picks the SIMULATED day, not the wall-clock day.
    Guarded/lazy import keeps live behavior unchanged when backtest isn't present."""
    import datetime as _dt
    try:
        from backtest import clock as _bt_clock
        if _bt_clock.is_active():
            return _bt_clock.now_ist().astimezone(tz).date()
    except Exception:
        pass
    return _dt.datetime.now(tz).date()


def _configured_market_data_source() -> str:
    """Return the configured market-data provider (Angel One only)."""
    _ensure_env_loaded()
    source = os.getenv("INTRADAY_DATA_SOURCE", "angelone").strip().lower()
    if source != "angelone":
        raise ValueError(
            "Only Angel One market data is supported. "
            "Set INTRADAY_DATA_SOURCE=angelone."
        )
    return source


class DataProvider:
    """Provides normalized Indian market data from Angel One SmartAPI."""

    # Cache to avoid excessive API calls.
    # A single flat TTL for all intervals was wasteful: daily candles don't
    # change intraday, but were being refetched every ~30s along with 5m/15m
    # bars, burning rate-limit budget that the genuinely time-sensitive
    # intraday intervals need. TTLs are now interval-aware:
    #  - 1d candles: cached for hours (effectively "once per session")
    #  - 15m candles: cached ~90s (close enough for setup detection)
    #  - 5m/1m and everything else: kept short (~20-30s) for freshness
    _cache: Dict[str, Dict[str, Any]] = {}
    _CACHE_TTL = 30  # seconds - fallback/default TTL for keys with no interval hint
    _CACHE_TTL_BY_INTERVAL = {
        "1d": 4 * 60 * 60,   # 4 hours - daily bar is fixed once the session's candle closes
        "15m": 90,
        "5m": 25,
        "1m": 15,
    }
    _QUOTE_CACHE_TTL = 8   # seconds - LTP quotes are cheap, tiny payloads; no
                           # reason to share the heavier 30s default TTL

    @staticmethod
    def normalize_symbol(symbol: str, exchange: str = "nse") -> str:
        """Normalize an equity/index symbol for Angel One resolution."""
        del exchange
        return symbol.upper().strip().replace(".NS", "").replace(".BO", "")

    @staticmethod
    def _ttl_for_key(key: str) -> float:
        """Cache keys are built as 'ohlcv:{exchange}:{symbol}:{period}:{interval}'
        (see get_ohlcv) or 'quote:{exchange}:{symbol}' for LTP lookups."""
        parts = key.split(":")
        if not parts:
            return DataProvider._CACHE_TTL
        if parts[0] == "ohlcv" and len(parts) >= 2:
            interval = parts[-1]
            return DataProvider._CACHE_TTL_BY_INTERVAL.get(interval, DataProvider._CACHE_TTL)
        if parts[0] == "quote":
            return DataProvider._QUOTE_CACHE_TTL
        return DataProvider._CACHE_TTL

    @staticmethod
    def _cache_get(key: str) -> Optional[Any]:
        entry = DataProvider._cache.get(key)
        ttl = DataProvider._ttl_for_key(key)
        if entry and (time.time() - entry["ts"]) < ttl:
            return entry["data"]
        return None

    @staticmethod
    def _cache_set(key: str, data: Any) -> None:
        DataProvider._cache[key] = {"ts": time.time(), "data": data}

    @staticmethod
    def _to_serializable(obj: Any) -> Any:
        if isinstance(obj, pd.Timestamp):
            return obj.isoformat()
        if isinstance(obj, pd.Series):
            return obj.to_dict()
        if hasattr(obj, "item") and hasattr(obj, "shape") and obj.shape == ():
            return obj.item()
        if isinstance(obj, dict):
            return {k: DataProvider._to_serializable(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [DataProvider._to_serializable(v) for v in obj]
        return obj

    @staticmethod
    def resample_5m_to_15m(m5: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build a 15m series by aggregating 5m candles in groups of 3.

        Used as a fallback when a broker cannot serve native 15m bars (e.g.
        Angel One may return fewer 15m rows than required for indicators).
        Returns a dict with the same shape as get_ohlcv().
        """
        if not m5 or "error" in m5 or not m5.get("candles"):
            return {"error": "no_5m_data", "candles": []}

        candles = m5["candles"]
        try:
            # Sort by timestamp ascending to keep chunking deterministic.
            candles = sorted(candles, key=lambda c: c.get("timestamp", ""))
        except TypeError:
            pass

        aggregated = []
        for i in range(0, len(candles) - 2, 3):
            chunk = candles[i:i + 3]
            if len(chunk) < 3:
                continue
            aggregated.append({
                "timestamp": chunk[0]["timestamp"],
                "open": float(chunk[0]["open"]),
                "high": max(float(c["high"]) for c in chunk),
                "low": min(float(c["low"]) for c in chunk),
                "close": float(chunk[-1]["close"]),
                "volume": sum(int(c.get("volume", 0)) for c in chunk),
            })

        if not aggregated:
            return {"error": "insufficient_5m_candles", "candles": []}

        return {
            "symbol": m5.get("symbol"),
            "symbol_input": m5.get("symbol_input"),
            "exchange": m5.get("exchange"),
            "market": m5.get("market", "INDIA"),
            "interval": "15m",
            "resampled": True,
            "count": len(aggregated),
            "total_candles": len(aggregated),
            "candles": aggregated,
            "source": m5.get("source", "angelone"),
        }

    @staticmethod
    def get_realtime_quote(symbol: str, exchange: str = "nse") -> Dict[str, Any]:
        """Get a normalized Angel One LTP quote without exposing credentials."""
        _configured_market_data_source()
        normalized = DataProvider.normalize_symbol(symbol, exchange)
        cache_key = f"quote:{exchange.lower()}:{normalized}"
        cached = DataProvider._cache_get(cache_key)
        if cached:
            cached = dict(cached)
            cached["symbol_input"] = symbol.upper()
            cached["exchange"] = exchange.upper()
            return cached

        try:
            from angel_one_api import create_client
            quote = create_client().get_realtime_quote(normalized, exchange)
        except Exception as exc:
            quote = {
                "symbol": normalized,
                "symbol_input": symbol.upper(),
                "exchange": exchange.upper(),
                "market": "INDIA",
                "error": f"Angel One quote unavailable: {exc}",
            }

        DataProvider._cache_set(cache_key, quote)
        return quote

    @staticmethod
    def get_ohlcv(
        symbol: str,
        period: str = "5d",
        interval: str = "15m",
        exchange: str = "nse",
        limit: Optional[int] = 100,
    ) -> Dict[str, Any]:
        """
        Get OHLCV data optimized for intraday trading.
        
        Args:
            symbol: Indian stock ticker symbol
            period: Valid periods: 1d,5d,1mo,3mo,6mo,1y,2y,5y,10y,ytd,max
            interval: Valid intervals: 1m,2m,5m,15m,30m,60m,90m,1h,1d,5d,1wk,1mo,3mo
            exchange: 'nse' (default) or 'bse'
            limit: Maximum candles to return (default 100, None for all)
        """
        _configured_market_data_source()
        normalized = DataProvider.normalize_symbol(symbol, exchange)
        cache_key = f"ohlcv:{exchange.lower()}:{normalized}:{period}:{interval}"
        cached = DataProvider._cache_get(cache_key)
        if cached:
            result = dict(cached)
            result["symbol_input"] = symbol.upper()
            result["exchange"] = exchange.upper()
            if limit and len(result.get("candles", [])) > limit:
                result["candles"] = result["candles"][-limit:]
                result["count"] = len(result["candles"])
            return result

        result = None
        # Retry a few times with escalating back-off. Angel One throttles
        # aggressively during live market hours, returning transient errors;
        # retrying with a pause recovers data instead of poisoning the result.
        for attempt in range(4):
            try:
                from angel_one_api import create_client
                result = create_client().get_ohlcv(normalized, period, interval, exchange)
                if result and not result.get("error") and result.get("candles"):
                    if limit and len(result.get("candles", [])) > limit:
                        result["candles"] = result["candles"][-limit:]
                        result["count"] = len(result["candles"])
                    break
                # Empty/error result: do not cache it; treat as retryable.
                result = None
            except Exception as exc:
                result = {
                    "symbol": normalized,
                    "symbol_input": symbol.upper(),
                    "exchange": exchange.upper(),
                    "market": "INDIA",
                    "error": f"Angel One candles unavailable: {exc}",
                    "candles": [],
                    "count": 0,
                }
            if attempt < 3:
                time.sleep(0.4 * (attempt + 1))

        if result is None:
            result = {
                "symbol": normalized,
                "symbol_input": symbol.upper(),
                "exchange": exchange.upper(),
                "market": "INDIA",
                "error": "Angel One candles unavailable after retries",
                "candles": [],
                "count": 0,
            }

        # Only cache successful results so transient errors are never served
        # to a later caller within the TTL window.
        if not result.get("error"):
            DataProvider._cache_set(cache_key, result)
        return result


class Indicators:
    """Technical indicator calculations optimized for intraday trading."""

    @staticmethod
    def _to_dataframe(candles: List[Dict[str, Any]]) -> pd.DataFrame:
        df = pd.DataFrame(candles)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp")
        df = df.astype(float)
        return df

    @staticmethod
    def _round_series(series: pd.Series, decimals: int = 4) -> List[float]:
        return [round(float(x), decimals) if not pd.isna(x) else None for x in series]

    @staticmethod
    def sma(candles: List[Dict[str, Any]], period: int = 20) -> Dict[str, Any]:
        df = Indicators._to_dataframe(candles)
        sma = df["close"].rolling(window=period).mean()
        return {
            "indicator": "SMA",
            "period": period,
            "values": Indicators._round_series(sma),
            "timestamps": [ts.isoformat() for ts in df.index],
        }

    @staticmethod
    def ema(candles: List[Dict[str, Any]], period: int = 20) -> Dict[str, Any]:
        df = Indicators._to_dataframe(candles)
        ema = df["close"].ewm(span=period, adjust=False).mean()
        return {
            "indicator": "EMA",
            "period": period,
            "values": Indicators._round_series(ema),
            "timestamps": [ts.isoformat() for ts in df.index],
        }

    @staticmethod
    def rsi(candles: List[Dict[str, Any]], period: int = 14) -> Dict[str, Any]:
        df = Indicators._to_dataframe(candles)
        close = df["close"]
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return {
            "indicator": "RSI",
            "period": period,
            "values": Indicators._round_series(rsi),
            "timestamps": [ts.isoformat() for ts in df.index],
        }

    @staticmethod
    def macd(
        candles: List[Dict[str, Any]],
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> Dict[str, Any]:
        df = Indicators._to_dataframe(candles)
        close = df["close"]
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line
        return {
            "indicator": "MACD",
            "fast": fast,
            "slow": slow,
            "signal": signal,
            "macd": Indicators._round_series(macd_line),
            "signal_line": Indicators._round_series(signal_line),
            "histogram": Indicators._round_series(histogram),
            "timestamps": [ts.isoformat() for ts in df.index],
        }

    @staticmethod
    def bollinger_bands(
        candles: List[Dict[str, Any]],
        period: int = 20,
        std_dev: float = 2.0,
    ) -> Dict[str, Any]:
        df = Indicators._to_dataframe(candles)
        middle = df["close"].rolling(window=period).mean()
        std = df["close"].rolling(window=period).std()
        upper = middle + (std * std_dev)
        lower = middle - (std * std_dev)
        return {
            "indicator": "BOLLINGER_BANDS",
            "period": period,
            "std_dev": std_dev,
            "upper": Indicators._round_series(upper),
            "middle": Indicators._round_series(middle),
            "lower": Indicators._round_series(lower),
            "timestamps": [ts.isoformat() for ts in df.index],
        }

    @staticmethod
    def atr(candles: List[Dict[str, Any]], period: int = 14) -> Dict[str, Any]:
        df = Indicators._to_dataframe(candles)
        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [
                high - low,
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = tr.ewm(alpha=1 / period, min_periods=period).mean()
        return {
            "indicator": "ATR",
            "period": period,
            "values": Indicators._round_series(atr),
            "timestamps": [ts.isoformat() for ts in df.index],
        }

    @staticmethod
    def vwap(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Volume Weighted Average Price (TODAY's session only).
        CRITICAL: Uses only today's candles from market open (9:15 AM onwards).
        Never includes previous day's data.
        """
        if not candles:
            return {"indicator": "VWAP", "values": None, "last_vwap": None}
        
        import datetime as dt
        
        df = Indicators._to_dataframe(candles)
        
        # Filter to TODAY's session only (critical for intraday trading)
        tz = zoneinfo.ZoneInfo("Asia/Kolkata")
        today = _session_today(tz)
        df_today = df[df.index.date == today]
        
        if df_today.empty:
            return {"indicator": "VWAP", "values": None, "last_vwap": None}
        
        typical_price = (df_today["high"] + df_today["low"] + df_today["close"]) / 3
        volume = df_today["volume"]
        cumulative_tp_vol = (typical_price * volume).cumsum()
        cumulative_vol = volume.cumsum()
        vwap = cumulative_tp_vol / cumulative_vol
        
        return {
            "indicator": "VWAP",
            "values": Indicators._round_series(vwap),
            "timestamps": [ts.isoformat() for ts in df_today.index],
        }

    @staticmethod
    def support_resistance(
        candles: List[Dict[str, Any]],
        num_levels: int = 3,
    ) -> Dict[str, Any]:
        """
        Identify support and resistance levels optimized for intraday.
        Uses recent swing pivots with tighter tolerance.
        """
        if not candles:
            return {"support_levels": [], "resistance_levels": []}

        closes = [float(c["close"]) for c in candles]
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        current = closes[-1]
        n = len(candles)
        start = max(0, n - int(n * 0.4))
        sw_highs, sw_lows = [], []
        for i in range(start + 3, n - 3):
            if highs[i] == max(highs[i-3:i+4]):
                sw_highs.append(highs[i])
            if lows[i] == min(lows[i-3:i+4]):
                sw_lows.append(lows[i])

        def band(vals, tol=0.01):
            if not vals:
                return []
            vals.sort()
            bands = [[vals[0]]]
            for v in vals[1:]:
                if abs(v - bands[-1][-1]) / bands[-1][-1] <= tol:
                    bands[-1].append(v)
                else:
                    bands.append([v])
            return [round(sum(b)/len(b), 2) for b in bands]

        sups = sorted([l for l in band(sw_lows) if l < current], reverse=True)
        ress = sorted([l for l in band(sw_highs) if l > current])
        return {
            "support_levels": [{"price": s, "strength": 1} for s in sups[:num_levels]],
            "resistance_levels": [{"price": r, "strength": 1} for r in ress[:num_levels]],
        }

    @staticmethod
    def compute_all(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Compute all indicators at once (full data for charting)."""
        return {
            "sma_20": Indicators.sma(candles, 20),
            "sma_50": Indicators.sma(candles, 50),
            "ema_20": Indicators.ema(candles, 20),
            "rsi_14": Indicators.rsi(candles, 14),
            "macd": Indicators.macd(candles),
            "bollinger_bands": Indicators.bollinger_bands(candles),
            "atr_14": Indicators.atr(candles, 14),
            "vwap": Indicators.vwap(candles),
            "support_resistance": Indicators.support_resistance(candles),
        }

    @staticmethod
    def compute_summary(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Compute a compact summary of all indicators (latest values only).
        Optimized for low token usage - returns only the most recent values.
        Includes volume_ratio for intraday analysis.
        """
        df = Indicators._to_dataframe(candles)
        close = df["close"]
        n = len(close)

        # SMA
        sma20 = close.rolling(20).mean().iloc[-1] if n >= 20 else None
        sma50 = close.rolling(50).mean().iloc[-1] if n >= 50 else None

        # EMA
        ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1] if n >= 20 else None

        # RSI
        if n >= 15:
            delta = close.diff()
            gain = delta.clip(lower=0)
            loss = -delta.clip(upper=0)
            avg_gain = gain.ewm(alpha=1 / 14, min_periods=14).mean().iloc[-1]
            avg_loss = loss.ewm(alpha=1 / 14, min_periods=14).mean().iloc[-1]
            rs = avg_gain / avg_loss if avg_loss != 0 else float("inf")
            rsi = 100 - (100 / (1 + rs)) if rs != float("inf") else 100
        else:
            rsi = None

        # MACD
        if n >= 26:
            ema_fast = close.ewm(span=12, adjust=False).mean().iloc[-1]
            ema_slow = close.ewm(span=26, adjust=False).mean().iloc[-1]
            macd_line = ema_fast - ema_slow
            macd_series = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
            signal_line = macd_series.ewm(span=9, adjust=False).mean().iloc[-1]
            histogram = macd_line - signal_line
        else:
            macd_line = signal_line = histogram = None

        # Bollinger Bands
        if n >= 20:
            bb_middle = close.rolling(20).mean().iloc[-1]
            bb_std = close.rolling(20).std().iloc[-1]
            bb_upper = bb_middle + (bb_std * 2)
            bb_lower = bb_middle - (bb_std * 2)
        else:
            bb_upper = bb_middle = bb_lower = None

        # ATR
        if n >= 15:
            high = df["high"]
            low = df["low"]
            prev_close = close.shift(1)
            tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
            atr = tr.ewm(alpha=1 / 14, min_periods=14).mean().iloc[-1]
        else:
            atr = None

        # VWAP (session-only - reset daily at market open)
        if n >= 2:
            # Filter to today's session only
            import datetime as dt
            tz = zoneinfo.ZoneInfo("Asia/Kolkata")
            today = _session_today(tz)
            try:
                # Filter dataframe to today only
                df_today = df[df.index.date == today]
                if len(df_today) >= 2:
                    typical_price = (df_today["high"] + df_today["low"] + df_today["close"]) / 3
                    volume = df_today["volume"]
                    cumulative_tp_vol = (typical_price * volume).cumsum()
                    cumulative_vol = volume.cumsum()
                    vwap_val = (cumulative_tp_vol / cumulative_vol).iloc[-1]
                else:
                    vwap_val = None
            except (AttributeError, TypeError):
                # Fallback if index doesn't have date attribute
                typical_price = (df["high"] + df["low"] + df["close"]) / 3
                volume = df["volume"]
                cumulative_tp_vol = (typical_price * volume).cumsum()
                cumulative_vol = volume.cumsum()
                vwap_val = (cumulative_tp_vol / cumulative_vol).iloc[-1]
        else:
            vwap_val = None

        # Volume ratio (current volume vs average)
        if n >= 10:
            current_vol = df["volume"].iloc[-1]
            avg_vol = df["volume"].rolling(10).mean().iloc[-1]
            volume_ratio = float(current_vol / avg_vol) if avg_vol > 0 else 1.0
        else:
            volume_ratio = 1.0

        # Support/Resistance
        sr = Indicators.support_resistance(candles)
        support = [s["price"] for s in sr.get("support_levels", [])[:3]]
        resistance = [r["price"] for r in sr.get("resistance_levels", [])[:3]]

        return {
            "last_close": round(float(close.iloc[-1]), 2) if n >= 1 else None,
            "sma20": round(float(sma20), 2) if sma20 is not None and not pd.isna(sma20) else None,
            "sma50": round(float(sma50), 2) if sma50 is not None and not pd.isna(sma50) else None,
            "ema20": round(float(ema20), 2) if ema20 is not None and not pd.isna(ema20) else None,
            "rsi14": round(float(rsi), 2) if rsi is not None else None,
            "macd": {
                "macd": round(float(macd_line), 2) if macd_line is not None else None,
                "signal": round(float(signal_line), 2) if signal_line is not None else None,
                "hist": round(float(histogram), 2) if histogram is not None else None,
            },
            "bollinger": {
                "upper": round(float(bb_upper), 2) if bb_upper is not None and not pd.isna(bb_upper) else None,
                "middle": round(float(bb_middle), 2) if bb_middle is not None and not pd.isna(bb_middle) else None,
                "lower": round(float(bb_lower), 2) if bb_lower is not None and not pd.isna(bb_lower) else None,
            },
            "atr14": round(float(atr), 2) if atr is not None and not pd.isna(atr) else None,
            "vwap": round(float(vwap_val), 2) if vwap_val is not None and not pd.isna(vwap_val) else None,
            "volume_ratio": round(float(volume_ratio), 2),
            "support": support,
            "resistance": resistance,
        }


def evaluate_intraday_stock(stock, settings):
    """
    Evaluate a single stock for intraday trading opportunities.
    
    This function delegates to engine.evaluate_stock() which is the
    SINGLE SOURCE OF TRUTH for all analysis. The engine returns the
    canonical record consumed by dashboard, scanner, and MCP server.
    """
    from engine import evaluate_stock, detect_market_regime, STATE_STORE
    
    # Build a market regime for this evaluation
    market_regime = detect_market_regime(settings)
    
    record = evaluate_stock(stock, settings, market_regime)
    
    # Map engine record to legacy-compatible fields expected by dashboard
    state = {
        "symbol": record.get("symbol"),
        "name": record.get("name"),
        "signal": record.get("signal", "WAIT"),
        "price": record.get("price"),
        "confidence": max(record.get("buy_score", 0), record.get("sell_score", 0)),
        "reasons": [record.get("reason", "")] if record.get("reason") else [],
        "detail": record.get("reason", ""),
        "in_zone": record.get("in_zone", False),
        "confirmed": record.get("confirmed", False),
        "bullish_candle": record.get("breakout", False),
        "smart_ok": record.get("buy_hard_gate", False) or record.get("sell_hard_gate", False),
        "factor_status": {
            "daily_trend": record.get("daily_trend"),
            "m15_trend": record.get("m15_trend"),
            "m5_structure": record.get("m5_structure"),
            "rsi": record.get("rsi"),
            "rvol": record.get("rvol"),
            "vwap": record.get("vwap"),
            "volume_signal": record.get("volume_signal"),
            "buy_score": record.get("buy_score"),
            "sell_score": record.get("sell_score"),
            "buy_hard_gate": record.get("buy_hard_gate"),
            "sell_hard_gate": record.get("sell_hard_gate"),
            "setup_id": record.get("setup_id"),
        },
        "missing": record.get("missing", []),
        "satisfied": record.get("satisfied", []),
        "score_breakdown": record.get("score_breakdown", {}),
        "score_summary": record.get("score_summary", []),
        "m15_trend": record.get("m15_trend"),
        "m5_trend": record.get("m5_structure"),
        "m15_rsi": None,
        "m5_rsi": record.get("rsi"),
        "m15_atr": record.get("atr_15m"),
        "m5_atr": record.get("atr_5m"),
        "m1_atr": None,
        "vwap": record.get("vwap"),
        "vwap_signal": "",
        "volume_ratio": record.get("rvol"),
        "volume_signal": record.get("volume_signal"),
        "entry_zone_lo": record.get("entry"),
        "entry_zone_hi": record.get("entry"),
        "confirm_level": record.get("resistance"),
        "stop_loss": record.get("stop_loss"),
        "targets": [t for t in [record.get("target_1"), record.get("target_2"), record.get("target_3")] if t],
        "buy_score": record.get("buy_score"),
        "sell_score": record.get("sell_score"),
        "buy_conditions": record.get("buy_conditions", {}),
        "sell_conditions": record.get("sell_conditions", {}),
        "buy_failed_conditions": record.get("buy_failed_conditions", []),
        "sell_failed_conditions": record.get("sell_failed_conditions", []),
        "market_regime": record.get("market_regime"),
        "liquidity_status": record.get("liquidity_status"),
        "setup_id": record.get("setup_id"),
        "risk_reward": record.get("risk_reward"),
        "support": record.get("support"),
        "resistance": record.get("resistance"),
        "volume_signal": record.get("volume_signal"),
    }
    
    # Add VWAP signal for display
    if record.get("vwap") and record.get("price"):
        if record["price"] > record["vwap"]:
            state["vwap_signal"] = "above VWAP (bullish)"
        elif record["price"] < record["vwap"]:
            state["vwap_signal"] = "below VWAP (bearish)"
    
    return state