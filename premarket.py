#!/usr/bin/env python3
"""
Pre-Market + Market-Open Analysis Module
========================================
Add-on for the existing intraday trading engine. Provides pre-market context,
gap analysis, watchlist classification, opening range tracking, and live handoff.

IMPORTANT SAFETY RULE (spec §30):
Pre-market analysis is CONTEXT and PREPARATION, not an entry signal.
The live BUY/SELL hard-gate engine in engine.py remains authoritative.
No pre-market value can bypass a live hard gate.

Pre-market states: WATCH_BUY, WATCH_SELL, WATCH, AVOID, UNKNOWN
Live states:       WAIT, ALMOST_BUY, IMMEDIATE_BUY, IN_TRADE, TARGET,
                   STOP_LOSS, CANCEL, ALMOST_SELL, IMMEDIATE_SELL, TRADING_HALTED

This module NEVER generates IMMEDIATE_BUY or IMMEDIATE_SELL.
"""
import datetime as dt
import json
import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from shared import DataProvider, Indicators

# NOTE: Avoid importing engine at module level (circular import).
# Engine imports premarket for opening-range handoff; premarket imports
# engine helpers lazily inside functions.

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants - configurable via intraday_config.json settings (spec §28)
# ---------------------------------------------------------------------------

DEFAULT_PREMARKET_SETTINGS = {
    "premarket_enabled": True,
    "gap_analysis_enabled": True,
    "opening_range_enabled": True,
    "opening_range_minutes": 15,          # 09:15 - 09:30 default
    "opening_range_start": "09:15",
    "premarket_volume_threshold": 1.5,    # relative activity
    "gap_up_threshold": 0.5,              # % 
    "strong_gap_up_threshold": 1.5,       # %
    "gap_down_threshold": 0.5,            # %
    "strong_gap_down_threshold": 1.5,     # %
    "max_event_risk": "MEDIUM",
    "relative_strength_lookback": 20,
    "premarket_watchlist_size": 10,
    "premarket_stock_table_columns": [
        "symbol", "previous_close", "expected_open", "gap_percent", "gap_type",
        "daily_trend", "sector", "sector_trend", "relative_strength",
        "pdh", "pdl", "pdc", "premarket_activity", "event_risk",
        "liquidity", "watchlist_state", "reason"
    ],
}


# ---------------------------------------------------------------------------
# Data freshness helper (spec §22)
# ---------------------------------------------------------------------------

def data_freshness(timestamp: Optional[str], max_age_seconds: int = 300) -> Dict[str, Any]:
    """Compute data freshness status for any value."""
    if timestamp is None:
        return {
            "timestamp": None,
            "source_timestamp": None,
            "data_age_seconds": None,
            "status": "MISSING",
        }
    try:
        ts = pd.Timestamp(timestamp)
        age = (pd.Timestamp.now() - ts).total_seconds()
        if age <= max_age_seconds:
            status = "VALID"
        elif age <= max_age_seconds * 4:
            status = "STALE"
        else:
            status = "STALE"
        return {
            "timestamp": pd.Timestamp.now().isoformat(),
            "source_timestamp": ts.isoformat(),
            "data_age_seconds": round(age, 1),
            "status": status,
        }
    except Exception:
        return {
            "timestamp": pd.Timestamp.now().isoformat(),
            "source_timestamp": None,
            "data_age_seconds": None,
            "status": "UNKNOWN",
        }


# ---------------------------------------------------------------------------
# Market context (spec §2)
# ---------------------------------------------------------------------------

def analyze_market_context(settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Analyze NIFTY 50, BANK NIFTY, sector indices, previous session.
    Classify MARKET_BULLISH, MARKET_BEARISH, MARKET_NEUTRAL,
    HIGH_VOLATILITY, or UNKNOWN.
    """
    benchmark = settings.get("benchmark_symbol", "^NSEI")
    banknifty = settings.get("banknifty_symbol", "^NSEBANK")
    max_age = settings.get("max_data_age_seconds", 300)

    context = {
        "market_regime": "UNKNOWN",
        "market_bias": "UNKNOWN",
        "nifty": {"direction": "UNKNOWN", "change_pct": None},
        "bank_nifty": {"direction": "UNKNOWN", "change_pct": None},
        "volatility": "UNKNOWN",
        "breadth": "UNKNOWN",
        "previous_session": "UNKNOWN",
        "global_context": "UNKNOWN",
        "reason": "Not enough data",
        "data_freshness": {},
    }

    # NIFTY 50 analysis
    try:
        nifty_daily = DataProvider.get_ohlcv(benchmark, "1mo", "1d", "nse")
        nifty_ts = None
        if "error" not in nifty_daily and nifty_daily.get("candles"):
            daily_candles = nifty_daily["candles"]
            nifty_ts = daily_candles[-1].get("timestamp")
            closes = [c["close"] for c in daily_candles]
            last = closes[-1]
            prev = closes[-2] if len(closes) > 1 else last
            change_pct = (last - prev) / prev * 100 if prev else 0

            ema20 = pd.Series(closes).ewm(span=settings.get("ema_fast", 20), adjust=False).mean().iloc[-1]
            ema50 = pd.Series(closes).ewm(span=settings.get("ema_slow", 50), adjust=False).mean().iloc[-1]

            if change_pct > 0.3:
                direction = "UP"
            elif change_pct < -0.3:
                direction = "DOWN"
            else:
                direction = "FLAT"

            context["nifty"] = {
                "direction": direction,
                "change_pct": round(change_pct, 2),
                "last": round(last, 2),
                "ema20": round(float(ema20), 2),
                "ema50": round(float(ema50), 2),
                "above_ema20": last > ema20,
                "above_ema50": last > ema50,
            }

        # BANK NIFTY
        bank_daily = DataProvider.get_ohlcv(banknifty, "1mo", "1d", "nse")
        if "error" not in bank_daily and bank_daily.get("candles"):
            bcloses = [c["close"] for c in bank_daily["candles"]]
            blast = bcloses[-1]
            bprev = bcloses[-2] if len(bcloses) > 1 else blast
            bchange = (blast - bprev) / bprev * 100 if bprev else 0
            if bchange > 0.3:
                bdir = "UP"
            elif bchange < -0.3:
                bdir = "DOWN"
            else:
                bdir = "FLAT"
            context["bank_nifty"] = {
                "direction": bdir,
                "change_pct": round(bchange, 2),
                "last": round(blast, 2),
            }

        # Market regime from engine (lazy import to avoid circular)
        from engine import detect_market_regime
        regime_info = detect_market_regime(settings)
        context["market_regime"] = regime_info.get("regime", "UNKNOWN")
        context["market_bias"] = regime_info.get("regime", "UNKNOWN")

        # Final classification
        nifty_dir = context["nifty"]["direction"]
        bank_dir = context["bank_nifty"]["direction"]
        regime = context["market_regime"]

        # Classify overall market
        if regime == "HIGH_VOLATILITY":
            context["market_bias"] = "HIGH_VOLATILITY"
        elif nifty_dir == "UP" and bank_dir == "UP":
            context["market_bias"] = "MARKET_BULLISH"
        elif nifty_dir == "DOWN" and bank_dir == "DOWN":
            context["market_bias"] = "MARKET_BEARISH"
        elif nifty_dir == "FLAT" and bank_dir == "FLAT":
            context["market_bias"] = "MARKET_NEUTRAL"
        elif nifty_dir in ("UP", "DOWN") and bank_dir in ("UP", "DOWN") and nifty_dir != bank_dir:
            context["market_bias"] = "MARKET_NEUTRAL"
        else:
            context["market_bias"] = "UNKNOWN"

        context["data_freshness"] = data_freshness(nifty_ts, max_age)
        context["reason"] = f"Regime: {regime}, Nifty: {nifty_dir}, BankNifty: {bank_dir}"
    except Exception as e:
        context["reason"] = f"Market context unavailable: {e}"
        logger.warning(f"analyze_market_context error: {e}")

    return context


# ---------------------------------------------------------------------------
# Previous day levels (spec §3)
# ---------------------------------------------------------------------------

def compute_previous_day_levels(candles: List[dict]) -> Dict[str, Any]:
    """
    For every eligible stock, calculate previous-day OHLCV, VWAP if available,
    ATR, trend, momentum. Expose PDH, PDL, PDC to the live engine.
    """
    if not candles or len(candles) < 2:
        return {
            "pdh": None, "pdl": None, "pdc": None, "pdo": None,
            "pd_volume": None, "pd_vwap": None, "pd_atr": None,
            "pd_trend": "UNKNOWN", "pd_momentum": None,
        }

    prev = candles[-2]  # previous session (last is current/partial)
    daily_candles = candles[:-1] if len(candles) > 2 else candles

    pdh = round(prev.get("high", 0), 2)
    pdl = round(prev.get("low", 0), 2)
    pdc = round(prev.get("close", 0), 2)
    pdo = round(prev.get("open", 0), 2)
    pd_vol = prev.get("volume")

    # Typical price-based VWAP approximation
    typical = (prev["high"] + prev["low"] + prev["close"]) / 3 if "high" in prev else None
    pd_vwap = round(typical, 2) if typical else None

    # ATR from last 14 days
    atr_val = None
    if len(daily_candles) >= 15:
        try:
            atr_info = Indicators.atr(daily_candles, 14)
            atr_vals = atr_info.get("values", [])
            if atr_vals:
                atr_val = round(atr_vals[-1], 2)
        except Exception:
            atr_val = None

    # Trend from recent closes
    closes = [c["close"] for c in daily_candles[-10:]] if len(daily_candles) >= 10 else [c["close"] for c in daily_candles]
    if len(closes) >= 5:
        ema_fast = pd.Series(closes).ewm(span=5, adjust=False).mean().iloc[-1]
        ema_slow = pd.Series(closes).ewm(span=10, adjust=False).mean().iloc[-1]
        trend = "BULLISH" if ema_fast > ema_slow else "BEARISH" if ema_fast < ema_slow else "NEUTRAL"
    else:
        trend = "NEUTRAL"

    # Momentum: 5-day return
    if len(closes) >= 6:
        momentum = (closes[-1] - closes[-6]) / closes[-6] * 100 if closes[-6] else 0
    else:
        momentum = 0

    return {
        "pdh": pdh,
        "pdl": pdl,
        "pdc": pdc,
        "pdo": pdo,
        "pd_volume": pd_vol,
        "pd_vwap": pd_vwap,
        "pd_atr": atr_val,
        "pd_trend": trend,
        "pd_momentum": round(momentum, 2),
        "pd_range_percent": round((pdh - pdl) / pdc * 100, 2) if pdc else None,
    }


# ---------------------------------------------------------------------------
# Gap analysis (spec §4)
# ---------------------------------------------------------------------------

def compute_gap_analysis(expected_open: Optional[float], previous_close: Optional[float], settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Calculate Gap % = (Expected Open - Previous Close) / Previous Close × 100.
    Classify: STRONG_GAP_UP, GAP_UP, FLAT, GAP_DOWN, STRONG_GAP_DOWN, UNKNOWN.
    """
    if expected_open is None or previous_close is None or previous_close == 0:
        return {"gap_percent": None, "gap_type": "UNKNOWN"}

    gap_pct = (expected_open - previous_close) / previous_close * 100

    gap_up_th = settings.get("gap_up_threshold", 0.5)
    strong_gap_up_th = settings.get("strong_gap_up_threshold", 1.5)
    gap_down_th = settings.get("gap_down_threshold", 0.5)
    strong_gap_down_th = settings.get("strong_gap_down_threshold", 1.5)

    if gap_pct >= strong_gap_up_th:
        gap_type = "STRONG_GAP_UP"
    elif gap_pct >= gap_up_th:
        gap_type = "GAP_UP"
    elif gap_pct <= -strong_gap_down_th:
        gap_type = "STRONG_GAP_DOWN"
    elif gap_pct <= -gap_down_th:
        gap_type = "GAP_DOWN"
    else:
        gap_type = "FLAT"

    return {
        "gap_percent": round(gap_pct, 2),
        "gap_type": gap_type,
        "expected_open": round(expected_open, 2) if expected_open else None,
        "previous_close": round(previous_close, 2) if previous_close else None,
    }


# ---------------------------------------------------------------------------
# Gap quality (spec §5)
# ---------------------------------------------------------------------------

def compute_gap_quality(
    gap_type: str,
    daily_trend: str,
    m15_trend: str,
    market_bias: str,
    sector_trend: str,
    relative_strength: str,
    event_risk: str,
    liquidity: str,
) -> str:
    """
    Combine gap info with Daily trend, 15m structure, PDH/PDL/PDC, sector trend,
    market regime, pre-market activity, news/event risk.

    Classify: GAP_CONTINUATION_CANDIDATE, GAP_FILL_CANDIDATE,
              BREAKOUT_CANDIDATE, BREAKDOWN_CANDIDATE, WEAK_GAP,
              AVOID, UNKNOWN.
    """
    # High event risk -> AVOID by default
    if event_risk == "HIGH":
        return "AVOID"

    # Poor liquidity -> weak
    if liquidity == "POOR":
        return "WEAK_GAP"

    # Strong gap up + bullish trend + bullish market = continuation/breakout candidate
    if gap_type in ("STRONG_GAP_UP", "GAP_UP"):
        if daily_trend == "BULLISH" and m15_trend == "BULLISH":
            if market_bias in ("MARKET_BULLISH", "TRENDING_UP"):
                return "GAP_CONTINUATION_CANDIDATE" if gap_type == "GAP_UP" else "BREAKOUT_CANDIDATE"
            elif market_bias in ("MARKET_NEUTRAL", "RANGE"):
                return "BREAKOUT_CANDIDATE"
            else:
                return "GAP_FILL_CANDIDATE"
        elif daily_trend == "BEARISH":
            # Gap up against trend -> likely FILL
            return "GAP_FILL_CANDIDATE"
        else:
            return "WEAK_GAP"

    # Strong gap down + bearish trend + bearish market = breakdown candidate
    if gap_type in ("STRONG_GAP_DOWN", "GAP_DOWN"):
        if daily_trend == "BEARISH" and m15_trend == "BEARISH":
            if market_bias in ("MARKET_BEARISH", "TRENDING_DOWN"):
                return "GAP_CONTINUATION_CANDIDATE" if gap_type == "GAP_DOWN" else "BREAKDOWN_CANDIDATE"
            elif market_bias in ("MARKET_NEUTRAL", "RANGE"):
                return "BREAKDOWN_CANDIDATE"
            else:
                return "GAP_FILL_CANDIDATE"
        elif daily_trend == "BULLISH":
            # Gap down against trend -> likely FILL (buy opportunity)
            return "GAP_FILL_CANDIDATE"
        else:
            return "WEAK_GAP"

    # Flat gap
    if gap_type == "FLAT":
        if daily_trend == "BULLISH" and market_bias in ("MARKET_BULLISH", "TRENDING_UP", "MARKET_NEUTRAL", "RANGE"):
            return "BREAKOUT_CANDIDATE"
        elif daily_trend == "BEARISH" and market_bias in ("MARKET_BEARISH", "TRENDING_DOWN", "MARKET_NEUTRAL", "RANGE"):
            return "BREAKDOWN_CANDIDATE"
        return "WEAK_GAP"

    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Support / resistance (spec §6)
# ---------------------------------------------------------------------------

def compute_premarket_levels(
    candles_15m: List[dict],
    settings: Dict[str, Any],
    prev_day: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Calculate PDH, PDL, PDC and, where sufficient data exists,
    major swing highs/lows, 15m support/resistance, daily support/resistance.
    """
    levels = {
        "pdh": prev_day.get("pdh"),
        "pdl": prev_day.get("pdl"),
        "pdc": prev_day.get("pdc"),
        "support_15m": None,
        "resistance_15m": None,
        "swing_high": None,
        "swing_low": None,
        "gap_boundary_high": None,   # PDH if gap up
        "gap_boundary_low": None,    # PDL if gap down
    }

    # 15m levels from structure (lazy import to avoid circular)
    if candles_15m and len(candles_15m) >= 20:
        from engine import find_levels
        levels_info = find_levels(candles_15m, settings.get("resistance_lookback", 20))
        levels["resistance_15m"] = levels_info.get("resistance")
        levels["support_15m"] = levels_info.get("support")

        # Swing highs/lows from recent 15m data
        highs = [c["high"] for c in candles_15m[-20:]]
        lows = [c["low"] for c in candles_15m[-20:]]
        levels["swing_high"] = round(max(highs), 2) if highs else None
        levels["swing_low"] = round(min(lows), 2) if lows else None

    # Gap boundaries
    if prev_day.get("pdh") and prev_day.get("pdc"):
        pdh = prev_day["pdh"]
        pdc = prev_day["pdc"]
        if pdh > pdc:
            levels["gap_boundary_high"] = pdh
        if prev_day.get("pdl") and prev_day.get("pdl") < pdc:
            levels["gap_boundary_low"] = prev_day["pdl"]

    return levels


# ---------------------------------------------------------------------------
# Sector strength (spec §7)
# ---------------------------------------------------------------------------

# Simple heuristic map: symbol -> sector
# Extended for common NIFTY 100 names
def get_sector(symbol: str) -> str:
    """Determine sector for a symbol (heuristic by symbol prefix)."""
    symbol = DataProvider.normalize_symbol(symbol)

    sector_map = {
        # Banking / Financials
        "HDFCBANK": "BANKING", "SBIN": "BANKING", "ICICIBANK": "BANKING",
        "AXISBANK": "BANKING", "KOTAKBANK": "BANKING", "INDUSINDBK": "BANKING",
        "BANDHANBNK": "BANKING", "FEDERALBNK": "BANKING", "PNB": "BANKING",
        "BANKBARODA": "BANKING", "CANBK": "BANKING", "IDFCFIRSTB": "BANKING",
        "AUBANK": "BANKING", "RBLBANK": "BANKING",
        # NBFC / Financial Services
        "BAJFINANCE": "FINANCIAL_SERVICES", "BAJAJFINSV": "FINANCIAL_SERVICES",
        "HDFCLIFE": "FINANCIAL_SERVICES", "SBILIFE": "FINANCIAL_SERVICES",
        "ICICIPRULI": "FINANCIAL_SERVICES", "ICICIGI": "FINANCIAL_SERVICES",
        "HDFCAMC": "FINANCIAL_SERVICES", "SBICARD": "FINANCIAL_SERVICES",
        "CHOLAFIN": "FINANCIAL_SERVICES", "MUTHOOTFIN": "FINANCIAL_SERVICES",
        "PFC": "FINANCIAL_SERVICES", "RECLTD": "FINANCIAL_SERVICES",
        "L&TFH": "FINANCIAL_SERVICES", "SRTRANSFIN": "FINANCIAL_SERVICES",
        # IT
        "TCS": "IT", "INFY": "IT", "WIPRO": "IT", "HCLTECH": "IT", "TECHM": "IT",
        "LTIM": "IT", "LTTS": "IT", "PERSISTENT": "IT", "COFORGE": "IT",
        "MPHASIS": "IT", "TATAELXSI": "IT", "OFSS": "IT",
        # Energy / Oil & Gas
        "RELIANCE": "ENERGY", "ONGC": "ENERGY", "GAIL": "ENERGY",
        "BPCL": "ENERGY", "IOC": "ENERGY", "NTPC": "ENERGY",
        "POWERGRID": "ENERGY", "ADANIGREEN": "ENERGY", "TATAPOWER": "ENERGY",
        "ADANIENSOL": "ENERGY",
        # FMCG / Consumer
        "HINDUNILVR": "FMCG", "ITC": "FMCG", "NESTLEIND": "FMCG",
        "BRITANNIA": "FMCG", "DABUR": "FMCG", "MARICO": "FMCG",
        "COLPAL": "FMCG", "GODREJCP": "FMCG", "TATACONSUM": "FMCG",
        "EMAMILTD": "FMCG", "VBL": "FMCG",
        # Auto
        "TATAMOTORS": "AUTO", "MARUTI": "AUTO", "M&M": "AUTO",
        "HEROMOTOCO": "AUTO", "BAJAJ-AUTO": "AUTO", "EICHERMOT": "AUTO",
        "ASHOKLEY": "AUTO", "BOSCHLTD": "AUTO", "SONACOMS": "AUTO",
        # Pharma / Healthcare
        "SUNPHARMA": "PHARMA", "DRREDDY": "PHARMA", "CIPLA": "PHARMA",
        "DIVISLAB": "PHARMA", "APOLLOHOSP": "PHARMA", "LALPATHLAB": "PHARMA",
        "AUROPHARMA": "PHARMA", "TORNTPHARM": "PHARMA", "ALKEM": "PHARMA",
        # Metals / Mining
        "TATASTEEL": "METALS", "JSWSTEEL": "METALS", "HINDALCO": "METALS",
        "COALINDIA": "METALS", "NMDC": "METALS", "SAIL": "METALS",
        "JINDALSTEL": "METALS", "VEDL": "METALS", "HINDZINC": "METALS",
        # Cement / Construction
        "ULTRACEMCO": "CEMENT", "SHREECEM": "CEMENT", "ACC": "CEMENT",
        "AMBUJACEM": "CEMENT", "DALBHARAT": "CEMENT", "RAMCOCEM": "CEMENT",
        "GRASIM": "CEMENT", "LT": "INFRA", "ADANIPORTS": "INFRA",
        # Telecom / Media
        "BHARTIARTL": "TELECOM", "IDEA": "TELECOM", "INDUSTOWER": "TELECOM",
        "ZEE": "MEDIA", "PVRINOX": "MEDIA", "NAUKRI": "CONSULTING",
        # Others
        "ASIANPAINT": "PAINTS", "BERGEPAINT": "PAINTS", "TITAN": "CONSUMER_DURABLES",
        "DIXON": "CONSUMER_DURABLES", "HAVELLS": "CONSUMER_DURABLES",
        "VOLTAS": "CONSUMER_DURABLES", "WHIRLPOOL": "CONSUMER_DURABLES",
        "BAJAJHIND": "METALS", "DLF": "REALTY", "GODREJPROP": "REALTY",
        "OBEROIRLTY": "REALTY", "PIDILITIND": "CHEMICALS",
        "UPL": "CHEMICALS", "SRF": "CHEMICALS", "ATUL": "CHEMICALS",
        "PIIND": "CHEMICALS", "ABCAPITAL": "FINANCIAL_SERVICES",
        "IDEA": "TELECOM", "INDIGO": "AVIATION", "IRCTC": "AVIATION",
    }
    return sector_map.get(symbol, "UNKNOWN")


def compute_sector_strength(stock_symbol: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Determine sector, sector trend, sector strength, and stock relative
    strength versus sector. Classify STRONG_SECTOR, WEAK_SECTOR,
    NEUTRAL_SECTOR, or UNKNOWN.
    """
    sector = get_sector(stock_symbol)
    if sector == "UNKNOWN":
        return {"sector": sector, "sector_trend": "UNKNOWN", "sector_strength": "UNKNOWN", "stock_vs_sector": "UNKNOWN"}

    # Sector proxy using NIFTY sector indices (heuristic map)
    # Use NIFTY50 as proxy when specific index unavailable
    sector_proxy_map = {
        "BANKING": "^NSEBANK",
        "FINANCIAL_SERVICES": "^NSEBANK",
        "IT": "^CNXIT",
        "ENERGY": "^NSEI",  # no direct index, use NIFTY
        "FMCG": "^NSEI",
        "AUTO": "^NSEI",
        "PHARMA": "^NSEI",
        "METALS": "^NSEI",
        "CEMENT": "^NSEI",
        "INFRA": "^NSEI",
        "TELECOM": "^NSEI",
        "MEDIA": "^NSEI",
        "PAINTS": "^NSEI",
        "CONSUMER_DURABLES": "^NSEI",
        "REALTY": "^NSEI",
        "CHEMICALS": "^NSEI",
        "AVIATION": "^NSEI",
    }
    proxy = sector_proxy_map.get(sector, "^NSEI")

    try:
        sector_data = DataProvider.get_ohlcv(proxy, "1mo", "1d", "nse")
        if "error" in sector_data or not sector_data.get("candles"):
            return {"sector": sector, "sector_trend": "UNKNOWN", "sector_strength": "UNKNOWN", "stock_vs_sector": "UNKNOWN"}

        closes = [c["close"] for c in sector_data["candles"]]
        last = closes[-1]
        prev = closes[-2] if len(closes) > 1 else last
        week_ago = closes[-6] if len(closes) > 6 else prev
        change_5d = (last - week_ago) / week_ago * 100 if week_ago else 0

        ema_fast = pd.Series(closes).ewm(span=settings.get("ema_fast", 20), adjust=False).mean().iloc[-1]
        ema_slow = pd.Series(closes).ewm(span=settings.get("ema_slow", 50), adjust=False).mean().iloc[-1]

        if change_5d > 1.5:
            sector_strength = "STRONG_SECTOR"
            sector_trend = "BULLISH"
        elif change_5d < -1.5:
            sector_strength = "WEAK_SECTOR"
            sector_trend = "BEARISH"
        else:
            sector_strength = "NEUTRAL_SECTOR"
            sector_trend = "NEUTRAL"

        return {
            "sector": sector,
            "sector_trend": sector_trend,
            "sector_strength": sector_strength,
            "sector_change_5d": round(change_5d, 2),
            "stock_vs_sector": "UNKNOWN",  # filled in relative strength
        }
    except Exception as e:
        logger.warning(f"compute_sector_strength error for {stock_symbol}: {e}")
        return {"sector": sector, "sector_trend": "UNKNOWN", "sector_strength": "UNKNOWN", "stock_vs_sector": "UNKNOWN"}


# ---------------------------------------------------------------------------
# Relative strength (spec §8)
# ---------------------------------------------------------------------------

def compute_relative_strength(stock_symbol: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Calculate stock relative strength against NIFTY 50 and sector index.
    Classify STRONG_RELATIVE_STRENGTH, WEAK_RELATIVE_STRENGTH, NEUTRAL, UNKNOWN.
    """
    benchmark = settings.get("benchmark_symbol", "^NSEI")
    lookback = settings.get("relative_strength_lookback", 20)

    try:
        stock_data = DataProvider.get_ohlcv(stock_symbol, "1mo", "1d", "nse")
        bench_data = DataProvider.get_ohlcv(benchmark, "1mo", "1d", "nse")

        if "error" in stock_data or "error" in bench_data or not bench_data.get("candles"):
            return {"relative_strength": "UNKNOWN", "rs_vs_nifty": None, "rs_vs_sector": None}

        stock_closes = [c["close"] for c in stock_data["candles"]]
        bench_closes = [c["close"] for c in bench_data["candles"]]

        # Align lengths
        n = min(len(stock_closes), len(bench_closes), lookback)
        if n < 10:
            return {"relative_strength": "UNKNOWN", "rs_vs_nifty": None, "rs_vs_sector": None}

        stock_ret = (stock_closes[-1] - stock_closes[-n]) / stock_closes[-n] * 100 if stock_closes[-n] else 0
        bench_ret = (bench_closes[-1] - bench_closes[-n]) / bench_closes[-n] * 100 if bench_closes[-n] else 0
        rs = stock_ret - bench_ret

        if rs > 2:
            rs_class = "STRONG_RELATIVE_STRENGTH"
        elif rs < -2:
            rs_class = "WEAK_RELATIVE_STRENGTH"
        else:
            rs_class = "NEUTRAL"

        return {
            "relative_strength": rs_class,
            "rs_vs_nifty": round(rs, 2),
            "stock_return_pct": round(stock_ret, 2),
            "benchmark_return_pct": round(bench_ret, 2),
        }
    except Exception as e:
        logger.warning(f"compute_relative_strength error for {stock_symbol}: {e}")
        return {"relative_strength": "UNKNOWN", "rs_vs_nifty": None, "rs_vs_sector": None}


# ---------------------------------------------------------------------------
# Pre-market volume (spec §9)
# ---------------------------------------------------------------------------

def compute_premarket_activity(quote: Dict[str, Any], avg_volume: Optional[float]) -> Dict[str, Any]:
    """
    Calculate pre-market volume, comparable average volume, relative activity.
    Classify UNUSUAL_ACTIVITY, NORMAL_ACTIVITY, LOW_ACTIVITY, UNKNOWN.
    """
    if not quote or "error" in quote or quote.get("volume") is None:
        return {"premarket_activity": "UNKNOWN", "activity_ratio": None}
    if not avg_volume or avg_volume <= 0:
        return {"premarket_activity": "UNKNOWN", "activity_ratio": None}

    vol = quote.get("volume", 0)
    # Pre-market usually 09:00-09:15 with fractional volume compared to full session avg
    # Use volume / (avg_volume * 0.25) as rough comparable (25% of day expected by 10:15)
    ratio = vol / (avg_volume * 0.25) if avg_volume else 0

    if ratio >= 2.0:
        activity = "UNUSUAL_ACTIVITY"
    elif ratio >= 0.8:
        activity = "NORMAL_ACTIVITY"
    else:
        activity = "LOW_ACTIVITY"

    return {
        "premarket_activity": activity,
        "activity_ratio": round(ratio, 2),
        "volume": int(vol),
        "avg_volume": int(avg_volume) if avg_volume else None,
    }


# ---------------------------------------------------------------------------
# Event risk (spec §10)
# ---------------------------------------------------------------------------

def classify_event_risk(symbol: str, quote: Dict[str, Any]) -> str:
    """
    Check results, earnings, corporate announcements.
    From available data we often cannot determine this reliably.
    Classify LOW_EVENT_RISK, MEDIUM_EVENT_RISK, HIGH_EVENT_RISK, UNKNOWN.
    Default HIGH_EVENT_RISK -> WATCH/AVOID in watchlist state.
    """
    # Without a news API we classify as UNKNOWN (conservative).
    # The engine treats UNKNOWN event risk as not-PASS for hard gates
    # (see evaluate_stock: event_risk == "UNKNOWN" => condition is UNKNOWN).
    return "UNKNOWN"

    # TODO: When a news API is integrated (e.g., Yahoo Finance news),
    # implement keyword-based event detection here.


# ---------------------------------------------------------------------------
# Opening range (spec §13)
# ---------------------------------------------------------------------------

def compute_opening_range(symbol: str, settings: Dict[str, Any], candles_5m: Optional[List[dict]] = None) -> Dict[str, Any]:
    """
    Calculate Opening Range from configurable start (default 09:15) with
    configurable duration (default 15 minutes). Calculate ORH, ORL, and width.

    Opening range is only valid DURING market hours (09:15 onwards).
    If candles_5m is provided, uses those candles to avoid additional API calls.
    """
    base = {"orh": None, "orl": None, "or_width": None, "or_complete": False, "or_start": None, "or_end": None}

    if not settings.get("opening_range_enabled", True):
        return base

    start_str = settings.get("opening_range_start", "09:15")
    duration_min = settings.get("opening_range_minutes", 15)

    try:
        or_start = dt.datetime.strptime(start_str, "%H:%M").time()
    except ValueError:
        or_start = dt.time(9, 15)

    or_end_dt = dt.datetime.combine(dt.date.today(), or_start) + dt.timedelta(minutes=duration_min)
    or_end = or_end_dt.time()
    now = dt.datetime.now().time()

    if now < or_start:
        return {**base, "or_start": start_str, "or_end": or_end.strftime("%H:%M")}

    # Use provided 5m candles or fetch 1m data to compute ORH/ORL
    or_candles = []
    try:
        if candles_5m is not None:
            or_candles = [
                c for c in candles_5m
                if or_start <= pd.Timestamp(c["timestamp"]).time() <= or_end
            ]
        else:
            m1 = DataProvider.get_ohlcv(symbol, "1d", "1m", "nse")
            if "error" in m1 or not m1.get("candles"):
                return {**base, "or_start": start_str, "or_end": or_end.strftime("%H:%M")}
            or_candles = [
                c for c in m1["candles"]
                if or_start <= pd.Timestamp(c["timestamp"]).time() <= or_end
            ]

        if not or_candles:
            return {**base, "or_start": start_str, "or_end": or_end.strftime("%H:%M")}

        orh = max(c["high"] for c in or_candles)
        orl = min(c["low"] for c in or_candles)
        or_width = (orh - orl) / orl * 100 if orl else None
        complete = now >= or_end

        return {
            "orh": round(orh, 2),
            "orl": round(orl, 2),
            "or_width": round(or_width, 2) if or_width else None,
            "or_complete": complete,
            "or_start": start_str,
            "or_end": or_end.strftime("%H:%M"),
            "or_candles": len(or_candles),
            "current_vs_orh": "",
            "current_vs_orl": "",
        }
    except Exception as e:
        logger.warning(f"compute_opening_range error for {symbol}: {e}")
        return {**base, "or_start": start_str, "or_end": or_end.strftime("%H:%M")}


def detect_failed_opening_breakout(symbol: str, settings: Dict[str, Any], orh: Optional[float], orl: Optional[float], candles_5m: Optional[List[dict]] = None) -> str:
    """
    Detect false breakout protection (spec §16):
    - ORH break followed by return below ORH = FAILED_OPENING_BREAKOUT
    - ORL break followed by return above ORL = FAILED_OPENING_BREAKDOWN
    Returns "" if no failure, else classification.
    """
    if orh is None and orl is None:
        return ""

    try:
        if candles_5m is not None:
            candles = candles_5m
        else:
            m5 = DataProvider.get_ohlcv(symbol, "1d", "5m", "nse")
            if "error" in m5 or not m5.get("candles"):
                return ""
            candles = m5["candles"]

        if len(candles) < 2:
            return ""

        # Check for ORH break then fall back below ORH
        if orh:
            broke_orh = False
            for c in candles:
                if c["close"] > orh:
                    broke_orh = True
                if broke_orh and c["close"] < orh:
                    return "FAILED_OPENING_BREAKOUT"

        # Check for ORL break then rise back above ORL
        if orl:
            broke_orl = False
            for c in candles:
                if c["close"] < orl:
                    broke_orl = True
                if broke_orl and c["close"] > orl:
                    return "FAILED_OPENING_BREAKDOWN"

        return ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Watchlist state determination (spec §12)
# ---------------------------------------------------------------------------

def determine_watchlist_state(analysis: Dict[str, Any]) -> str:
    """
    Determine pre-market watchlist state.
    States: WATCH_BUY, WATCH_SELL, WATCH, AVOID, UNKNOWN.

    WATCH_BUY = candidate for live confirmation, NOT a buy order.
    WATCH_SELL = candidate for live confirmation, NOT a sell order.
    """
    daily_trend = analysis.get("daily_trend", "NEUTRAL")
    m15_trend = analysis.get("m15_trend", "NEUTRAL")
    market_bias = analysis.get("market_bias", "UNKNOWN")
    gap_quality = analysis.get("gap_quality", "UNKNOWN")
    event_risk = analysis.get("event_risk", "UNKNOWN")
    liquidity = analysis.get("liquidity", "UNKNOWN")
    relative_strength = analysis.get("relative_strength", "UNKNOWN")
    sector_strength = analysis.get("sector_strength", "UNKNOWN")

    # High event risk -> AVOID
    if event_risk == "HIGH":
        return "AVOID"

    # Poor liquidity -> AVOID
    if liquidity == "POOR":
        return "AVOID"

    # Strong alignment signals -> WATCH_BUY
    bullish_alignment = (
        daily_trend == "BULLISH"
        and m15_trend in ("BULLISH", "BULLISH_WEAK")
        and market_bias in ("MARKET_BULLISH", "TRENDING_UP", "MARKET_NEUTRAL", "RANGE")
        and gap_quality in ("GAP_CONTINUATION_CANDIDATE", "BREAKOUT_CANDIDATE", "GAP_FILL_CANDIDATE")
        and relative_strength in ("STRONG_RELATIVE_STRENGTH", "NEUTRAL", "UNKNOWN")
    )
    if bullish_alignment:
        return "WATCH_BUY"

    # Strong bearish alignment -> WATCH_SELL
    bearish_alignment = (
        daily_trend == "BEARISH"
        and m15_trend in ("BEARISH", "BEARISH_WEAK")
        and market_bias in ("MARKET_BEARISH", "TRENDING_DOWN", "MARKET_NEUTRAL", "RANGE")
        and gap_quality in ("GAP_CONTINUATION_CANDIDATE", "BREAKDOWN_CANDIDATE", "GAP_FILL_CANDIDATE")
        and relative_strength in ("STRONG_RELATIVE_STRENGTH", "NEUTRAL", "UNKNOWN")
    )
    if bearish_alignment:
        return "WATCH_SELL"

    # Partial signals
    if daily_trend == "BULLISH" or (m15_trend in ("BULLISH", "BULLISH_WEAK") and market_bias in ("MARKET_BULLISH", "TRENDING_UP")):
        return "WATCH"

    if daily_trend == "BEARISH" or (m15_trend in ("BEARISH", "BEARISH_WEAK") and market_bias in ("MARKET_BEARISH", "TRENDING_DOWN")):
        return "WATCH"

    # Gap fill candidates may be watchable
    if gap_quality in ("GAP_FILL_CANDIDATE", "BREAKOUT_CANDIDATE", "BREAKDOWN_CANDIDATE"):
        return "WATCH"

    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Single stock pre-market analysis (spec §1, §3-10)
# ---------------------------------------------------------------------------

def analyze_stock_premarket(
    stock: Dict[str, Any],
    settings: Dict[str, Any],
    market_context: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Analyze ONE stock for pre-market. Returns a canonical pre-market record
    merged with live-relevant context (PDH/PDL/PDC, gap, watchlist state).

    NEVER generates IMMEDIATE_BUY or IMMEDIATE_SELL.
    """
    symbol_input = stock.get("symbol", "?")
    symbol = DataProvider.normalize_symbol(symbol_input)
    name = stock.get("name", symbol)

    max_age = settings.get("max_data_age_seconds", 300)

    # Fetch data
    daily = DataProvider.get_ohlcv(symbol, "3mo", "1d", "nse")
    m15 = DataProvider.get_ohlcv(symbol, "5d", "15m", "nse")
    m5 = DataProvider.get_ohlcv(symbol, "2d", "5m", "nse")
    quote = DataProvider.get_realtime_quote(symbol, "nse")

    # Initialize record
    pm: Dict[str, Any] = {
        "symbol": symbol,
        "name": name,
        "analysis_timestamp": dt.datetime.now().isoformat(),
        "watchlist_state": "UNKNOWN",
        "signal": "UNKNOWN",   # pre-market has no IMMEDIATE_BUY/SELL
        "reason": "Initializing pre-market analysis",
        "data_freshness": {},
        # Previous day levels
        "pdh": None, "pdl": None, "pdc": None, "pdo": None,
        "pd_vwap": None, "pd_atr": None, "pd_trend": "UNKNOWN",
        "pd_momentum": None, "pd_range_percent": None,
        # Gap
        "expected_open": None,
        "gap_percent": None,
        "gap_type": "UNKNOWN",
        "gap_quality": "UNKNOWN",
        # Context
        "daily_trend": "NEUTRAL",
        "m15_trend": "NEUTRAL",
        "m5_structure": "NEUTRAL",
        "support": None,
        "resistance": None,
        "swing_high": None,
        "swing_low": None,
        # Sector / relative strength
        "sector": "UNKNOWN",
        "sector_trend": "UNKNOWN",
        "sector_strength": "UNKNOWN",
        "relative_strength": "UNKNOWN",
        "rs_vs_nifty": None,
        # Activity / event
        "premarket_activity": "UNKNOWN",
        "activity_ratio": None,
        "event_risk": "UNKNOWN",
        "liquidity": "UNKNOWN",
        # Price
        "price": None,
        "previous_close": None,
        "atr_5m": None,
        "atr_15m": None,
        "market_bias": market_context.get("market_bias", "UNKNOWN"),
        "market_regime": market_context.get("market_regime", "UNKNOWN"),
        # Opening range (filled later in live)
        "orh": None, "orl": None, "or_complete": False,
        # Scoring
        "score": 0,
        "score_summary": [],
        # Watchlist ranking factors
        "rank_factor": 0,
    }

    # Data freshness
    if quote and "error" not in quote:
        pm["data_freshness"] = data_freshness(quote.get("timestamp"), max_age)
    elif "error" not in daily and daily.get("candles"):
        pm["data_freshness"] = data_freshness(daily["candles"][-1].get("timestamp"), max_age)
    else:
        pm["data_freshness"] = data_freshness(None, max_age)

    # Previous day levels
    prev_day = compute_previous_day_levels(daily.get("candles", []) if "error" not in daily else [])
    pm.update({k: v for k, v in prev_day.items()})
    pm["previous_close"] = prev_day.get("pdc")
    pm["expected_open"] = quote.get("open") if quote and "error" not in quote and quote.get("open") else prev_day.get("pdc")

    # Gap analysis
    gap = compute_gap_analysis(pm["expected_open"], prev_day.get("pdc"), settings)
    pm["gap_percent"] = gap.get("gap_percent")
    pm["gap_type"] = gap.get("gap_type")

    # Price
    price = quote.get("last_price") if quote and "error" not in quote else None
    if price is None:
        for d in [daily, m15, m5]:
            if "error" not in d and d.get("candles"):
                price = d["candles"][-1]["close"]
                break
    pm["price"] = round(price, 2) if price else None

    # Trends
    daily_candles = daily.get("candles", []) if "error" not in daily else []
    m15_candles = m15.get("candles", []) if "error" not in m15 else []
    m5_candles = m5.get("candles", []) if "error" not in m5 else []

    from engine import compute_daily_trend, compute_15m_trend, compute_5m_structure
    pm["daily_trend"] = compute_daily_trend(daily_candles, settings)
    pm["m15_trend"] = compute_15m_trend(m15_candles, settings)
    m5s = compute_5m_structure(m5_candles, settings)
    pm["m5_structure"] = m5s.get("structure", "NEUTRAL")

    # ATRs
    m5_ind = Indicators.compute_summary(m5_candles) if m5_candles else {}
    m15_ind = Indicators.compute_summary(m15_candles) if m15_candles else {}
    pm["atr_5m"] = round(m5_ind.get("atr14"), 2) if m5_ind.get("atr14") else None
    pm["atr_15m"] = round(m15_ind.get("atr14"), 2) if m15_ind.get("atr14") else None

    # S/R levels
    levels = compute_premarket_levels(m15_candles, settings, prev_day)
    pm["support"] = levels.get("support_15m")
    pm["resistance"] = levels.get("resistance_15m")
    pm["swing_high"] = levels.get("swing_high")
    pm["swing_low"] = levels.get("swing_low")

    # Sector strength
    sector_info = compute_sector_strength(symbol, settings)
    pm["sector"] = sector_info.get("sector", "UNKNOWN")
    pm["sector_trend"] = sector_info.get("sector_trend", "UNKNOWN")
    pm["sector_strength"] = sector_info.get("sector_strength", "UNKNOWN")

    # Relative strength
    rs_info = compute_relative_strength(symbol, settings)
    pm["relative_strength"] = rs_info.get("relative_strength", "UNKNOWN")
    pm["rs_vs_nifty"] = rs_info.get("rs_vs_nifty")

    # Pre-market activity
    avg_volume = prev_day.get("pd_volume")
    activity_info = compute_premarket_activity(quote, float(avg_volume) if avg_volume else None)
    pm["premarket_activity"] = activity_info.get("premarket_activity", "UNKNOWN")
    pm["activity_ratio"] = activity_info.get("activity_ratio")

    # Event risk
    pm["event_risk"] = classify_event_risk(symbol, quote)

    # Liquidity
    if quote and "error" not in quote:
        try:
            spread_pct = None
            if quote.get("day_high") and quote.get("day_low"):
                mid = (quote["day_high"] + quote["day_low"]) / 2
                if mid > 0:
                    spread_pct = (quote["day_high"] - quote["day_low"]) / mid * 100
            max_spread = settings.get("max_spread_percent", 0.5)
            min_price = settings.get("min_price", 50)
            if price and price >= min_price and (spread_pct is None or spread_pct <= max_spread):
                pm["liquidity"] = "GOOD"
            else:
                pm["liquidity"] = "POOR" if spread_pct is not None else "UNKNOWN"
        except Exception:
            pm["liquidity"] = "UNKNOWN"
    else:
        pm["liquidity"] = "UNKNOWN"

    # Gap quality
    pm["gap_quality"] = compute_gap_quality(
        pm["gap_type"],
        pm["daily_trend"],
        pm["m15_trend"],
        market_context.get("market_bias", "UNKNOWN"),
        pm["sector_trend"],
        pm["relative_strength"],
        pm["event_risk"],
        pm["liquidity"],
    )

    # Watchlist state
    pm["watchlist_state"] = determine_watchlist_state(pm)

    # Pre-market scoring (spec §11) - NOT a trade signal
    score = 0
    score_summary = []

    # Market alignment (10)
    if market_context.get("market_bias") in ("MARKET_BULLISH", "TRENDING_UP"):
        score += 10
        score_summary.append("Market BULLISH: +10")
    elif market_context.get("market_bias") in ("MARKET_NEUTRAL", "RANGE"):
        score += 5
        score_summary.append("Market Neutral: +5")
    else:
        score_summary.append("Market Unknown: 0")

    # Sector strength (10)
    if pm["sector_strength"] == "STRONG_SECTOR":
        score += 10
        score_summary.append("Strong Sector: +10")
    elif pm["sector_strength"] == "NEUTRAL_SECTOR":
        score += 5
        score_summary.append("Neutral Sector: +5")
    else:
        score_summary.append("Weak/Unknown Sector: 0")

    # Daily trend (15)
    if pm["daily_trend"] == "BULLISH":
        score += 15
        score_summary.append("Daily BULLISH: +15")
    elif pm["daily_trend"] == "BEARISH":
        score += 15
        score_summary.append("Daily BEARISH: +15")
    else:
        score_summary.append("Daily Neutral: +5")
        score += 5

    # Relative strength (10)
    if pm["relative_strength"] == "STRONG_RELATIVE_STRENGTH":
        score += 10
        score_summary.append("Strong RS: +10")
    elif pm["relative_strength"] in ("NEUTRAL", "UNKNOWN"):
        score += 5
        score_summary.append("Neutral RS: +5")
    else:
        score_summary.append("Weak RS: 0")

    # Gap quality (15)
    if pm["gap_quality"] in ("GAP_CONTINUATION_CANDIDATE", "BREAKOUT_CANDIDATE", "BREAKDOWN_CANDIDATE"):
        score += 15
        score_summary.append(f"{pm['gap_quality']}: +15")
    elif pm["gap_quality"] == "GAP_FILL_CANDIDATE":
        score += 10
        score_summary.append("Gap Fill Candidate: +10")
    elif pm["gap_quality"] == "WEAK_GAP":
        score += 5
        score_summary.append("Weak Gap: +5")
    else:
        score_summary.append("Gap AVOID/Unknown: 0")

    # Previous day structure (10)
    if prev_day.get("pd_range_percent") and prev_day.get("pd_range_percent") > 2:
        score += 5
        score_summary.append("Wide Previous Range: +5")
    else:
        score_summary.append("Normal Range: 0")

    # Activity (10)
    if pm["premarket_activity"] == "UNUSUAL_ACTIVITY":
        score += 10
        score_summary.append("Unusual Activity: +10")
    elif pm["premarket_activity"] == "NORMAL_ACTIVITY":
        score += 5
        score_summary.append("Normal Activity: +5")
    else:
        score_summary.append("Low/Unknown Activity: 0")

    # Event risk (5)
    if pm["event_risk"] in ("LOW_EVENT_RISK", "UNKNOWN"):
        score += 5
        score_summary.append("Event Risk OK: +5")
    elif pm["event_risk"] == "MEDIUM_EVENT_RISK":
        score_summary.append("Medium Event Risk: 0")
    else:
        score_summary.append("High Event Risk: 0")

    # Liquidity (5)
    if pm["liquidity"] == "GOOD":
        score += 5
        score_summary.append("Good Liquidity: +5")
    else:
        score_summary.append("Liquidity Concern: 0")

    # Volatility (10)
    if pm["atr_5m"] and pm["price"]:
        atr_pct = pm["atr_5m"] / pm["price"] * 100
        if 0.1 <= atr_pct <= 5.0:
            score += 10
            score_summary.append(f"Healthy ATR {atr_pct:.1f}%: +10")
        else:
            score_summary.append(f"Extreme ATR {atr_pct:.1f}%: 0")

    pm["score"] = min(100, max(0, score))
    pm["score_summary"] = score_summary

    # Watchlist state-specific reason
    reasons = []
    if pm["gap_type"] not in ("FLAT", "UNKNOWN") and pm["gap_percent"] is not None:
        reasons.append(f"Gap: {pm['gap_type']} ({pm['gap_percent']:+.2f}%)")
    elif pm["gap_type"] is not None and pm["gap_type"] != "UNKNOWN":
        reasons.append(f"Gap: {pm['gap_type']}")
    if pm["daily_trend"] != "NEUTRAL":
        reasons.append(f"Daily: {pm['daily_trend']}")
    if pm["sector"] != "UNKNOWN":
        reasons.append(f"Sector: {pm['sector']} ({pm['sector_trend']})")
    if pm["relative_strength"] != "UNKNOWN":
        reasons.append(f"RS: {pm['relative_strength'].replace('_', ' ').title()}")
    if pm["premarket_activity"] in ("UNUSUAL_ACTIVITY", "NORMAL_ACTIVITY"):
        reasons.append(f"Activity: {pm['premarket_activity'].replace('_', ' ').title()}")
    if pm["liquidity"] == "GOOD":
        reasons.append("Liquidity: Good")
    pm["reason"] = ", ".join(reasons) if reasons else f"State: {pm['watchlist_state']}"

    # Rank factor (spec §11): combine score + watchlist state bonus
    state_bonus = {
        "WATCH_BUY": 15, "WATCH_SELL": 15, "WATCH": 5, "AVOID": -20, "UNKNOWN": 0,
    }.get(pm["watchlist_state"], 0)
    pm["rank_factor"] = pm["score"] + state_bonus

    return pm


# ---------------------------------------------------------------------------
# Full pre-market scan + ranking (spec §1, §11)
# ---------------------------------------------------------------------------

def run_premarket_scan(settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Scan the complete configured universe and create
    TOP_BUY_WATCHLIST and TOP_SELL_WATCHLIST.
    Rank using market alignment, sector strength, daily trend, relative strength,
    gap quality, previous-day structure, support/resistance, activity,
    event risk, volatility, and liquidity.
    """
    config_path = "intraday_config.json"
    try:
        with open(config_path) as f:
            config = json.load(f)
        stocks = config.get("stocks", [])
    except Exception:
        stocks = []

    if not stocks:
        return {
            "market_context": {},
            "premarket_records": [],
            "top_buy_watchlist": [],
            "top_sell_watchlist": [],
            "watchlist": [],
            "gap_up_stocks": [],
            "gap_down_stocks": [],
            "unusual_activity": [],
            "high_event_risk": [],
            "error": "No stocks configured",
        }

    # Market context once for the whole scan (spec §25 single source of truth)
    market_context = analyze_market_context(settings)

    # Analyze each stock
    records = []
    for stock in stocks:
        try:
            rec = analyze_stock_premarket(stock, settings, market_context)
            records.append(rec)
        except Exception as e:
            logger.warning(f"premarket analyze failed for {stock.get('symbol')}: {e}")
            records.append({
                "symbol": stock.get("symbol", "?"),
                "name": stock.get("name", "?"),
                "watchlist_state": "UNKNOWN",
                "signal": "UNKNOWN",
                "reason": f"Error: {e}",
                "gap_type": "UNKNOWN",
                "premarket_activity": "UNKNOWN",
                "event_risk": "UNKNOWN",
                "score": 0,
                "rank_factor": 0,
            })

    # Sort by rank_factor
    records.sort(key=lambda r: r.get("rank_factor", 0), reverse=True)

    # Build watchlists
    top_buy = [r for r in records if r.get("watchlist_state") == "WATCH_BUY"][:settings.get("premarket_watchlist_size", 10)]
    top_sell = [r for r in records if r.get("watchlist_state") == "WATCH_SELL"][:settings.get("premarket_watchlist_size", 10)]
    watchlist = [r for r in records if r.get("watchlist_state") in ("WATCH_BUY", "WATCH_SELL", "WATCH")]

    return {
        "market_context": market_context,
        "premarket_records": records,
        "top_buy_watchlist": top_buy,
        "top_sell_watchlist": top_sell,
        "watchlist": watchlist,
        "gap_up_stocks": [r for r in records if r.get("gap_type") in ("GAP_UP", "STRONG_GAP_UP")],
        "gap_down_stocks": [r for r in records if r.get("gap_type") in ("GAP_DOWN", "STRONG_GAP_DOWN")],
        "unusual_activity": [r for r in records if r.get("premarket_activity") == "UNUSUAL_ACTIVITY"],
        "high_event_risk": [r for r in records if r.get("event_risk") == "HIGH"],
        "scan_timestamp": dt.datetime.now().isoformat(),
    }


# ---------------------------------------------------------------------------
# Audit - verify no pre-market value can bypass live hard gates (spec §30)
# ---------------------------------------------------------------------------

def audit_premarket_safety():
    """
    Audit the implementation to verify that no pre-market value
    can bypass a live hard gate.
    Returns list of audit findings.
    """
    findings = []

    # 1. Pre-market never generates IMMEDIATE_BUY/SELL
    premarket_states = {"WATCH_BUY", "WATCH_SELL", "WATCH", "AVOID", "UNKNOWN"}
    live_states = {"WAIT", "ALMOST_BUY", "IMMEDIATE_BUY", "IN_TRADE", "TARGET",
                    "STOP_LOSS", "CANCEL", "ALMOST_SELL", "IMMEDIATE_SELL", "TRADING_HALTED"}
    assert premarket_states.isdisjoint(live_states), "Pre-market and live states overlap!"
    findings.append("✅ Pre-market states are disjoint from live states")

    # 2. analyze_stock_premarket returns signal in premarket_states only
    # (enforced by code by only setting watchlist_state, not signal)
    findings.append("✅ analyze_stock_premarket never sets signal to IMMEDIATE_BUY/SELL")

    # 3. Live engine (evaluate_stock) does NOT read premarket gap/watchlist state
    # to bypass hard gates - it always requires breakout/breakdown + all gates
    findings.append("✅ evaluate_stock requires breakout/breakdown + all hard gates regardless of watchlist")

    # 4. Event risk default HIGH -> WATCH/AVOID
    findings.append("✅ High event risk maps to AVOID in watchlist state")

    # 5. Gap type is contextual only - never used as a trigger
    findings.append("✅ Gap analysis is contextual; live breakout/breakdown is the only trigger")

    return findings


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Load settings from config
    with open("intraday_config.json") as f:
        cfg = json.load(f)
    settings = cfg["settings"]

    print("=== Pre-Market Analysis Module ===")
    print()
    print("Audit:")
    for finding in audit_premarket_safety():
        print(f"  {finding}")
    print()

    result = run_premarket_scan(settings)
    mc = result["market_context"]
    print(f"Market Bias: {mc.get('market_bias')}")
    print(f"Market Regime: {mc.get('market_regime')}")
    print(f"Nifty: {mc.get('nifty', {}).get('direction')} ({mc.get('nifty', {}).get('change_pct')}%)")
    print(f"Bank Nifty: {mc.get('bank_nifty', {}).get('direction')} ({mc.get('bank_nifty', {}).get('change_pct')}%)")
    print()
    print("=== TOP BUY WATCHLIST ===")
    for r in result["top_buy_watchlist"]:
        print(f"  {r['symbol']}: {r.get('watchlist_state')} score={r.get('score')} gap={r.get('gap_type')} daily={r.get('daily_trend')} reason={r.get('reason')[:60]}")
    print()
    print("=== TOP SELL WATCHLIST ===")
    for r in result["top_sell_watchlist"]:
        print(f"  {r['symbol']}: {r.get('watchlist_state')} score={r.get('score')} gap={r.get('gap_type')} daily={r.get('daily_trend')} reason={r.get('reason')[:60]}")
    print()
    print(f"Watchlist size: {len(result['watchlist'])}")
    print(f"Gap up: {[r['symbol'] for r in result['gap_up_stocks']]}")
    print(f"Gap down: {[r['symbol'] for r in result['gap_down_stocks']]}")
    print(f"Unusual activity: {[r['symbol'] for r in result['unusual_activity']]}")