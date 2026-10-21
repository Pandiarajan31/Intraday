#!/usr/bin/env python3
"""
Intraday Trading MCP Server for Indian Stocks
=============================================

Provides intraday trading setups using the hard-gate engine (engine.py).
Uses Daily + 15m + 5m timeframe hierarchy:
- Daily trend (primary direction)
- 15m structure (confirmation)
- 5m entry (breakout/breakdown + trigger)

Every analysis produces ONE canonical record consumed by the MCP server,
dashboard, scanner, and alerts - no recalc in different modules.

Output format matches the stock_monitor / swing trading setup for consistency.
"""

import json
import sys
import logging
from typing import Dict, List, Optional, Tuple
import pandas as pd
import numpy as np

# Redirect all logging to stderr to keep stdout clean for MCP stdio protocol
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

from mcp.server.fastmcp import FastMCP

# Import shared + engine (same module used by dashboard)
from shared import DataProvider, Indicators
from engine import (
    evaluate_stock,
    scan_universe,
    top_opportunities,
    detect_market_regime,
    STATE_STORE,
)
from premarket import (
    analyze_market_context,
    analyze_stock_premarket,
    run_premarket_scan,
    audit_premarket_safety,
)

# Create MCP server using FastMCP (same as trading-mcp-server)
mcp = FastMCP(
    "intraday-trading-server",
    instructions="""Indian stock market intraday trading MCP server for day trading analysis.
Provides intraday trading setups using multi-timeframe analysis (Daily, 15m, 5m).
Includes hard-gate BUY/SELL engines, volume analysis, VWAP integration,
risk/reward scoring, and liquidity/event-risk filters.

Supports NSE and BSE stocks. Use Indian ticker symbols like RELIANCE, TCS, HDFCBANK,
INFY, SBIN, NIFTY, SENSEX, BANKNIFTY etc.""",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize(obj):
    """Convert numpy types to Python native types for JSON serialization."""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_sanitize(x) for x in obj]
    elif isinstance(obj, (np.integer,)):
        return int(obj)
    elif isinstance(obj, (np.floating,)):
        return float(obj)
    elif isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj


def _load_settings():
    """Load settings from intraday_config.json."""
    try:
        with open("intraday_config.json") as f:
            config = json.load(f)
        return config.get("settings", {})
    except Exception:
        return {}


def _format_setup_record(record):
    """Format a canonical engine record into a setup-friendly output."""
    setup = {
        "name": record.get("name", record.get("symbol")),
        "symbol": record.get("symbol"),
        "interval": "5m",
        "entry_zone_lo": record.get("entry"),
        "entry_zone_hi": record.get("entry"),
        "confirm_level": record.get("resistance"),
        "cancel_level": record.get("support"),
        "stop_loss": record.get("stop_loss"),
        "targets": [
            t for t in [
                record.get("target_1"),
                record.get("target_2"),
                record.get("target_3"),
            ] if t is not None
        ],
        "risk_reward": record.get("risk_reward"),
        "setup_id": record.get("setup_id"),
    }

    notes_parts = []
    if record.get("signal"):
        notes_parts.append(f"Signal: {record['signal']}")
    if record.get("reason"):
        notes_parts.append(record["reason"])
    if record.get("m15_trend"):
        notes_parts.append(f"15m trend {record['m15_trend']}")
    if record.get("m5_structure"):
        notes_parts.append(f"5m structure {record['m5_structure']}")
    if record.get("daily_trend"):
        notes_parts.append(f"Daily trend {record['daily_trend']}")
    if record.get("volume_signal"):
        notes_parts.append(f"Volume {record['volume_signal']}")
    if record.get("vwap") is not None:
        notes_parts.append(f"VWAP ₹{record['vwap']:.2f}")
    if record.get("rvol") is not None:
        notes_parts.append(f"RVOL {record['rvol']:.2f}x")
    if record.get("buy_score") is not None:
        notes_parts.append(f"BUY score {record['buy_score']}")
    if record.get("sell_score") is not None:
        notes_parts.append(f"SELL score {record['sell_score']}")
    if record.get("buy_hard_gate"):
        notes_parts.append("BUY gate OPEN")
    if record.get("sell_hard_gate"):
        notes_parts.append("SELL gate OPEN")

    setup["notes"] = "; ".join(notes_parts)

    # Ready-to-paste config block (matches stock_monitor format)
    config_block = {
        "name": record.get("name", record.get("symbol")),
        "symbol": record.get("symbol"),
        "interval": "5m",
    }
    for k in ("entry_zone_lo", "entry_zone_hi", "confirm_level", "cancel_level", "stop_loss"):
        if setup.get(k) is not None:
            config_block[k] = round(setup[k], 2) if isinstance(setup[k], float) else setup[k]
    if setup.get("targets"):
        config_block["targets"] = [round(t, 2) for t in setup["targets"]]

    return setup, config_block


# ---------------------------------------------------------------------------
# INTRADAY SETUP GENERATOR (delegates to engine)
# ---------------------------------------------------------------------------

def generate_intraday_setup(symbol: str, period: str = "5d", exchange: str = "nse") -> str:
    """
    Generate a complete intraday trading setup using the hard-gate engine.
    
    Uses three timeframes:
    - Daily: Primary trend direction
    - 15m: Structure / confirmation
    - 5m: Entry trigger (breakout/breakdown)
    
    Returns a canonical engine record with entry zones, stop loss, targets,
    BUY/SELL scores, hard gates, conditions, and a config block.
    """
    settings = _load_settings()
    settings["benchmark_symbol"] = settings.get("benchmark_symbol", "^NSEI")

    stock = {"symbol": symbol, "name": symbol}

    # Detect market regime once
    market_regime = detect_market_regime(settings)

    # Evaluate via the canonical engine
    record = evaluate_stock(stock, settings, market_regime)

    # If outside market hours / insufficient data, still return useful info
    if record.get("signal") in ("WAIT",) and record.get("failed_conditions"):
        # Build a lighter setup using shared.py analysis for reference
        ticker = DataProvider.normalize_symbol(symbol, exchange)
        m15_result = DataProvider.get_ohlcv(ticker, period="5d", interval="15m", exchange=exchange)
        m5_result = DataProvider.get_ohlcv(ticker, period="2d", interval="5m", exchange=exchange)
        quote = DataProvider.get_realtime_quote(ticker, exchange=exchange)

        setup_fallback = {
            "symbol": record.get("symbol", ticker),
            "signal": record.get("signal", "WAIT"),
            "reason": record.get("reason", ""),
            "market_regime": record.get("market_regime"),
        }
        if "error" not in quote and quote.get("last_price"):
            setup_fallback["current_price"] = quote["last_price"]
        return json.dumps(_sanitize(setup_fallback), indent=2)

    # Format canonical record into a setup-friendly response
    setup, config_block = _format_setup_record(record)

    result = {
        "symbol": record.get("symbol"),
        "current_price": record.get("price"),
        "signal": record.get("signal"),
        "reason": record.get("reason"),
        "market_regime": record.get("market_regime"),
        "setup_id": record.get("setup_id"),
        "trend_analysis": {
            "daily_trend": record.get("daily_trend"),
            "m15_trend": record.get("m15_trend"),
            "m5_structure": record.get("m5_structure"),
            "rsi": record.get("rsi"),
            "volume_signal": record.get("volume_signal"),
            "volume_ratio": record.get("rvol"),
            "vwap": record.get("vwap"),
            "atr_5m": record.get("atr_5m"),
            "atr_15m": record.get("atr_15m"),
            "support": record.get("support"),
            "resistance": record.get("resistance"),
        },
        "scores": {
            "buy_score": record.get("buy_score"),
            "sell_score": record.get("sell_score"),
            "buy_hard_gate": record.get("buy_hard_gate"),
            "sell_hard_gate": record.get("sell_hard_gate"),
        },
        "conditions": {
            "buy_conditions": record.get("buy_conditions", {}),
            "sell_conditions": record.get("sell_conditions", {}),
            "buy_failed_conditions": record.get("buy_failed_conditions", []),
            "sell_failed_conditions": record.get("sell_failed_conditions", []),
        },
        "setup": setup,
        "config_json_block": json.dumps(config_block, indent=2),
        "how_to_use": (
            "Copy the config_json_block into your stock_monitor config.json 'stocks' array. "
            "interval=5m is the entry trigger timeframe; "
            "trend analysis was done on Daily + 15m + 5m timeframes. "
            "Close all positions by 15:15 IST (square-off)."
        ),
    }

    return json.dumps(_sanitize(result), indent=2)


# ---------------------------------------------------------------------------
# MARKET SCANNER (delegates to engine)
# ---------------------------------------------------------------------------

def scan_intraday_market(exchange: str = "nse") -> str:
    """
    Scan the configured stock universe for intraday opportunities.
    
    Uses the canonical engine to evaluate every stock, then returns
    ranked TOP_BUY_CANDIDATES and TOP_SELL_CANDIDATES based on
    hard-gate validity, score, risk/reward, volume, and liquidity.
    """
    settings = _load_settings()

    try:
        results = scan_universe(settings)
    except Exception as e:
        logging.warning(f"scan_universe failed: {e}")
        # Fallback: evaluate individual liquid stocks directly
        liquid_stocks = [
            "RELIANCE", "TCS", "HDFCBANK", "INFY", "SBIN",
            "ICICIBANK", "KOTAKBANK", "HINDUNILVR", "ITC", "LT",
            "AXISBANK", "BAJFINANCE", "WIPRO", "HCLTECH", "ASIANPAINT",
        ]
        market_regime = detect_market_regime(settings)
        results = []
        for symbol in liquid_stocks:
            try:
                record = evaluate_stock({"symbol": symbol, "name": symbol}, settings, market_regime)
                results.append(record)
            except Exception:
                continue

    top = top_opportunities(results, top_n=10)

    def _fmt(r):
        return {
            "symbol": r.get("symbol"),
            "name": r.get("name"),
            "signal": r.get("signal"),
            "buy_score": r.get("buy_score"),
            "sell_score": r.get("sell_score"),
            "buy_hard_gate": r.get("buy_hard_gate"),
            "sell_hard_gate": r.get("sell_hard_gate"),
            "price": r.get("price"),
            "entry": r.get("entry"),
            "stop_loss": r.get("stop_loss"),
            "risk_reward": r.get("risk_reward"),
            "reason": r.get("reason"),
            "volume_signal": r.get("volume_signal"),
            "rvol": r.get("rvol"),
            "liquidity_status": r.get("liquidity_status"),
        }

    return json.dumps(_sanitize({
        "scan_time": pd.Timestamp.now().isoformat(),
        "exchange": exchange.upper(),
        "total_scanned": len(results),
        "top_buy_candidates": [_fmt(r) for r in top.get("top_buy_candidates", [])],
        "top_sell_candidates": [_fmt(r) for r in top.get("top_sell_candidates", [])],
        "market_regime": detect_market_regime(settings).get("regime", "UNKNOWN"),
    }), indent=2)


# ---------------------------------------------------------------------------
# INTRADAY INDICATORS (uses shared.py for multi-timeframe detail)
# ---------------------------------------------------------------------------

def get_intraday_indicators(symbol: str, period: str = "1d", exchange: str = "nse") -> str:
    """
    Get technical indicators for intraday trading.
    
    Returns indicators across 15m, 5m, and 1m timeframes.
    """
    ticker = DataProvider.normalize_symbol(symbol, exchange)

    # Fetch data using shared.py's get_ohlcv()
    m15_result = DataProvider.get_ohlcv(ticker, period=period, interval="15m", exchange=exchange)
    m5_result = DataProvider.get_ohlcv(ticker, period=period, interval="5m", exchange=exchange)
    m1_result = DataProvider.get_ohlcv(ticker, period="1d", interval="1m", exchange=exchange)

    m15_data = m15_result.get("candles", []) if "error" not in m15_result else []
    m5_data = m5_result.get("candles", []) if "error" not in m5_result else []
    m1_data = m1_result.get("candles", []) if "error" not in m1_result else []

    # Current price
    quote = DataProvider.get_realtime_quote(ticker, exchange=exchange)
    current_price = quote.get("last_price") if quote and "error" not in quote else None

    # Compute indicators for each timeframe
    def compute_indicators(candles, label):
        if not candles or len(candles) < 20:
            return {"timeframe": label, "error": "Insufficient data"}

        summary = Indicators.compute_summary(candles)

        closes = [c["close"] for c in candles]
        sma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
        sma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else None

        if sma10 is not None and sma20 is not None and closes[-1] > sma10:
            trend = "bullish"
        elif sma10 is not None and sma20 is not None and closes[-1] < sma10:
            trend = "bearish"
        else:
            trend = "neutral"

        # VWAP signal
        vwap_signal = ""
        if summary.get("vwap") and closes:
            vwap_signal = "above" if closes[-1] >= summary["vwap"] else "below"

        return {
            "timeframe": label,
            "current_price": closes[-1],
            "rsi": summary.get("rsi14"),
            "atr": summary.get("atr14"),
            "sma10": round(sma10, 2) if sma10 else None,
            "sma20": round(sma20, 2) if sma20 else None,
            "trend": trend,
            "vwap": summary.get("vwap"),
            "price_vs_vwap": vwap_signal,
            "volume_ratio": summary.get("volume_ratio"),
            "bollinger": summary.get("bollinger"),
            "data_points": len(candles),
        }

    result = {
        "symbol": ticker,
        "current_price": current_price,
        "indicators": {
            "m15": compute_indicators(m15_data, "15m"),
            "m5": compute_indicators(m5_data, "5m"),
            "m1": compute_indicators(m1_data, "1m"),
        }
    }

    return json.dumps(_sanitize(result), indent=2)


# ---------------------------------------------------------------------------
# MCP TOOLS
# ---------------------------------------------------------------------------

@mcp.tool(
    name="intraday_trade_setup",
    description="Generate a complete intraday trading setup for an Indian stock using the hard-gate engine (Daily + 15m + 5m). Returns BUY/SELL scores, hard gates, entry zones, stop loss, targets, conditions, and a ready-to-paste config_json_block for stock_monitor."
)
def intraday_trade_setup_tool(symbol: str, period: str = "5d", exchange: str = "nse") -> str:
    return generate_intraday_setup(symbol, period, exchange)


@mcp.tool(
    name="intraday_market_scanner",
    description="Scan the Indian market for intraday opportunities using the hard-gate engine. Returns TOP_BUY_CANDIDATES and TOP_SELL_CANDIDATES ranked by hard-gate validity, score, risk/reward, volume, and liquidity."
)
def intraday_market_scanner_tool(exchange: str = "nse") -> str:
    return scan_intraday_market(exchange)


@mcp.tool(
    name="get_intraday_indicators",
    description="Get technical indicators for intraday trading across 15m, 5m, and 1m timeframes. Returns RSI, ATR, SMA, VWAP, trend direction, and Bollinger Bands for each timeframe."
)
def get_intraday_indicators_tool(symbol: str, period: str = "1d", exchange: str = "nse") -> str:
    return get_intraday_indicators(symbol, period, exchange)


# ---------------------------------------------------------------------------
# PRE-MARKET TOOLS
# ---------------------------------------------------------------------------

@mcp.tool(
    name="premarket_scanner",
    description="Scan the Indian market pre-market for intraday opportunities. Returns market context, TOP_BUY_WATCHLIST and TOP_SELL_WATCHLIST, gap-up/gap-down stocks, unusual activity, and high event risk. IMPORTANT: Pre-market analysis is CONTEXT, not an entry signal - the live hard-gate engine remains authoritative."
)
def premarket_scanner_tool(exchange: str = "nse") -> str:
    """
    Pre-market market scan. Provides pre-market watchlists for live confirmation.
    NEVER generates IMMEDIATE_BUY or IMMEDIATE_SELL.
    """
    settings = _load_settings()
    
    result = run_premarket_scan(settings)
    
    def _fmt(r, include_detail=True):
        base = {
            "symbol": r.get("symbol"),
            "name": r.get("name"),
            "watchlist_state": r.get("watchlist_state"),
            "reason": r.get("reason"),
            "score": r.get("score"),
            "gap_type": r.get("gap_type"),
            "gap_percent": r.get("gap_percent"),
            "daily_trend": r.get("daily_trend"),
            "m15_trend": r.get("m15_trend"),
            "m5_structure": r.get("m5_structure"),
            "sector": r.get("sector"),
            "sector_trend": r.get("sector_trend"),
            "relative_strength": r.get("relative_strength"),
            "premarket_activity": r.get("premarket_activity"),
            "event_risk": r.get("event_risk"),
            "liquidity": r.get("liquidity"),
            "expected_open": r.get("expected_open"),
            "previous_close": r.get("previous_close"),
        }
        if include_detail:
            base.update({
                "pdh": r.get("pdh"),
                "pdl": r.get("pdl"),
                "pdc": r.get("pdc"),
                "support": r.get("support"),
                "resistance": r.get("resistance"),
                "swing_high": r.get("swing_high"),
                "swing_low": r.get("swing_low"),
                "price": r.get("price"),
                "atr_5m": r.get("atr_5m"),
                "atr_15m": r.get("atr_15m"),
                "rs_vs_nifty": r.get("rs_vs_nifty"),
                "activity_ratio": r.get("activity_ratio"),
                "score_summary": r.get("score_summary", []),
                "rank_factor": r.get("rank_factor"),
            })
        return base
    
    mc = result.get("market_context", {})
    return json.dumps(_sanitize({
        "scan_type": "PRE_MARKET",
        "scan_time": result.get("scan_timestamp"),
        "market_context": {
            "market_regime": mc.get("market_regime"),
            "market_bias": mc.get("market_bias"),
            "nifty": mc.get("nifty"),
            "bank_nifty": mc.get("bank_nifty"),
            "reason": mc.get("reason"),
        },
        "total_scanned": len(result.get("premarket_records", [])),
        "top_buy_watchlist": [_fmt(r) for r in result.get("top_buy_watchlist", [])],
        "top_sell_watchlist": [_fmt(r) for r in result.get("top_sell_watchlist", [])],
        "watchlist": [_fmt(r, include_detail=False) for r in result.get("watchlist", [])],
        "gap_up_stocks": [_fmt(r, include_detail=False) for r in result.get("gap_up_stocks", [])],
        "gap_down_stocks": [_fmt(r, include_detail=False) for r in result.get("gap_down_stocks", [])],
        "unusual_activity": [_fmt(r, include_detail=False) for r in result.get("unusual_activity", [])],
        "high_event_risk": [_fmt(r, include_detail=False) for r in result.get("high_event_risk", [])],
        "important_note": "Pre-market analysis is CONTEXT and PREPARATION, not an entry signal. The live BUY/SELL hard-gate engine in engine.py remains authoritative.",
    }), indent=2)


@mcp.tool(
    name="premarket_stock_analysis",
    description="Analyze ONE Indian stock for pre-market and market-open context. Returns previous day levels (PDH/PDL/PDC), gap analysis, trend, sector strength, relative strength, pre-market activity, event risk, watchlist state, and opening range levels. NEVER generates an entry signal."
)
def premarket_stock_analysis_tool(symbol: str, exchange: str = "nse") -> str:
    """
    Analyze ONE stock for pre-market context.
    """
    settings = _load_settings()
    stock = {"symbol": symbol, "name": symbol}
    
    # Market context first
    market_context = analyze_market_context(settings)
    
    # Analyze stock pre-market
    rec = analyze_stock_premarket(stock, settings, market_context)
    
    base = {
        "symbol": rec.get("symbol"),
        "name": rec.get("name"),
        "analysis_timestamp": rec.get("analysis_timestamp"),
        "watchlist_state": rec.get("watchlist_state"),
        "reason": rec.get("reason"),
        "score": rec.get("score"),
        "score_summary": rec.get("score_summary", []),
        "market_bias": rec.get("market_bias"),
        "market_regime": rec.get("market_regime"),
        "previous_day": {
            "pdh": rec.get("pdh"),
            "pdl": rec.get("pdl"),
            "pdc": rec.get("pdc"),
            "pdo": rec.get("pdo"),
            "pd_vwap": rec.get("pd_vwap"),
            "pd_atr": rec.get("pd_atr"),
            "pd_trend": rec.get("pd_trend"),
            "pd_momentum": rec.get("pd_momentum"),
            "pd_range_percent": rec.get("pd_range_percent"),
        },
        "gap": {
            "expected_open": rec.get("expected_open"),
            "gap_percent": rec.get("gap_percent"),
            "gap_type": rec.get("gap_type"),
            "gap_quality": rec.get("gap_quality"),
        },
        "trend_analysis": {
            "daily_trend": rec.get("daily_trend"),
            "m15_trend": rec.get("m15_trend"),
            "m5_structure": rec.get("m5_structure"),
            "support": rec.get("support"),
            "resistance": rec.get("resistance"),
            "swing_high": rec.get("swing_high"),
            "swing_low": rec.get("swing_low"),
        },
        "sector": {
            "sector": rec.get("sector"),
            "sector_trend": rec.get("sector_trend"),
            "sector_strength": rec.get("sector_strength"),
        },
        "relative_strength": {
            "class": rec.get("relative_strength"),
            "rs_vs_nifty": rec.get("rs_vs_nifty"),
        },
        "activity": {
            "premarket_activity": rec.get("premarket_activity"),
            "activity_ratio": rec.get("activity_ratio"),
        },
        "risk": {
            "event_risk": rec.get("event_risk"),
            "liquidity": rec.get("liquidity"),
        },
        "price": rec.get("price"),
        "previous_close": rec.get("previous_close"),
        "atr_5m": rec.get("atr_5m"),
        "atr_15m": rec.get("atr_15m"),
        "important_note": "This is pre-market context only. The live BUY/SELL hard-gate engine is the only source of entry signals.",
    }
    
    return json.dumps(_sanitize(base), indent=2)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Ensure all logging goes to stderr, never stdout
    logging.getLogger().handlers = [logging.StreamHandler(sys.stderr)]
    mcp.run(transport="stdio")