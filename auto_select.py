#!/usr/bin/env python3
"""
Auto-Selection of Intraday Watchlist (IMPROVED)
================================================

Purpose
-------
Build a *daily watchlist* of stocks that are most suitable to MONITOR today.
This module is a WATCHLIST BUILDER only. It NEVER generates an executable
trade signal (no IMMEDIATE_BUY / IMMEDIATE_SELL / BUY_NOW / SELL_NOW).

Strict architecture (per spec):
- engine.evaluate_stock() remains the SINGLE SOURCE OF TRUTH for live signals.
- This module only ranks watchlist *quality* and writes intraday_config.json
  "stocks". It does not modify engine.py / shared.py / premarket.py / server.
- Candidate Quality is a separate, watchlist-only score. It is NEVER merged
  with the engine's BUY/SELL scores and can never authorize an entry.

Pipeline (pre-open):
  1. Fetch universe (NIFTY100 live, curated fallback offline).
  2. Liquidity / eligibility pre-filter (price, volume, traded value, spread).
     Liquidity is a MINIMUM tradability requirement, not the primary ranking.
  3. Wider pre-ranking pool (candidate_pool_size, default 50) before Top-N.
  4. Candidate Quality ranking using ONLY valid pre-open data
     (previous-day trend/structure, ATR suitability, relative strength vs NIFTY,
      sector strength, gap quality, pre-market activity, event risk, liquidity).
     No live 5m breakout / RVOL / VWAP / current session state / current price
     is used for pre-open ranking (those may be N/A before market open).
  5. Top-N Core Watchlist written into intraday_config.json["stocks"].

Optional dynamic opportunity pool (during market hours):
  dynamic_discover() scans the broader universe for stocks outside the morning
  Top-N that now show unusual volume/movement. They must still pass eligibility
  and risk requirements. This never rebuilds the whole universe every minute.

Candidate Quality Score weights (configurable):
  liquidity           20%
  atr_suitability     15%
  prevday_trend       15%
  relative_strength   15%
  sector_strength     10%
  gap_quality         10%
  premarket_activity   5%
  event_risk           5%
  prevday_participation 5%

Missing data stays UNKNOWN / "N/A" and is treated as 0 contribution (never a
free PASS or a perfect score). A high Candidate Quality never implies BUY/SELL.

Usage (CLI):
    python auto_select.py [--config intraday_config.json] [--top 20]

Programmatic:
    from auto_select import select_stocks, write_config, dynamic_discover
    symbols, ranked = select_stocks(settings, top_n=20)
    write_config(symbols, "intraday_config.json")
"""

import argparse
import csv
import io
import json
import sys

import requests

# We import ONLY the live-engine functions we need and re-run them on the
# watchlist at DASHBOARD time. We do NOT use engine.evaluate_stock() to
# pre-rank because it depends on invalid pre-open live data. We reuse the
# premarket context functions (context-only) for Candidate Quality inputs.
from premarket import (
    compute_previous_day_levels,
    compute_gap_analysis,
    compute_gap_quality,
    compute_sector_strength,
    compute_relative_strength,
    compute_premarket_activity,
    classify_event_risk,
    Indicators,
)
from shared import DataProvider

# Re-export for callers that build records
try:
    from engine import detect_market_regime  # used by dashboard for context
except Exception:  # pragma: no cover
    detect_market_regime = None


def _norm_symbol(sym):
    """Normalize symbol to uppercase and strip whitespace."""
    if not sym:
        return sym
    return sym.upper().strip()


# NSE / NSE Indices publish these as static CSVs. Each entry has a PRIMARY
# url (nseindia.com, matching the pattern the existing NIFTY100 fetch already
# used) and a FALLBACK url (niftyindices.com mirror) tried if the primary
# fails or is blocked (NSE's site is known to rate-limit/block non-browser
# user agents intermittently).
UNIVERSE_CSV_URLS = {
    "NIFTY100": [
        "https://www.nseindia.com/content/indices/ind_nifty100list.csv",
        "https://niftyindices.com/IndexConstituent/ind_nifty100list.csv",
    ],
    "NIFTY200": [
        "https://www.nseindia.com/content/indices/ind_nifty200list.csv",
        "https://niftyindices.com/IndexConstituent/ind_nifty200list.csv",
    ],
    "NIFTY500": [
        "https://www.nseindia.com/content/indices/ind_nifty500list.csv",
        "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    ],
}

# Curated fallback if the network is unavailable. These are liquid NIFTY names
# (substitute for a live constituent fetch) so selection still works offline.
# NOTE: this is a NIFTY100-scoped list only. If a NIFTY200/NIFTY500 fetch
# fails, get_universe() deliberately degrades to THIS SAME list rather than
# a larger hand-typed one - index membership changes twice a year and a
# stale/incorrect 300-400 name list typed from memory is a worse failure
# mode (silently feeding wrong/delisted symbols into a live trading system)
# than an honest, visible fallback to a smaller known-good list.
FALLBACK_UNIVERSE = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "SBIN", "ICICIBANK", "HINDUNILVR",
    "ITC", "LT", "AXISBANK", "BAJFINANCE", "WIPRO", "HCLTECH", "ASIANPAINT",
    "TATAMOTORS", "KOTAKBANK", "BHARTIARTL", "NTPC", "SUNPHARMA", "TITAN",
    "MARUTI", "BAJAJFINSV", "ULTRACEMCO", "ONGC", "POWERGRID",
    "TATASTEEL", "ADANIPORTS", "JSWSTEEL", "CIPLA", "DRREDDY", "TECHM",
    "GRASIM", "BPCL", "COALINDIA", "EICHERMOT", "HEROMOTOCO", "INDUSINDBK",
    "SBILIFE", "HDFCLIFE", "DIVISLAB", "APOLLOHOSP", "BRITANNIA", "SHREECEM",
    "TATACONSUM", "UPL", "NESTLEIND", "TATAPOWER", "M&M", "BAJAJ-AUTO",
]


def _resolve_universe_key(filter_name: str) -> str:
    """Map a free-form filter_name to one of the known CSV keys, defaulting
    to NIFTY100 for anything unrecognized (same safe default as before)."""
    name = str(filter_name or "NIFTY100").upper()
    for key in ("NIFTY500", "NIFTY200", "NIFTY100"):  # check longest/most specific first
        if key in name:
            return key
    return "NIFTY100"


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
def get_universe(filter_name="NIFTY100"):
    """Return the universe as a list of {"symbol", "name"} dicts.

    Supports "NIFTY100" (default), "NIFTY200", and "NIFTY500" via
    settings["universe_filter"]. Tries each filter's live CSV URLs in
    order (NSE primary, niftyindices.com fallback); if both fail, degrades
    to the curated NIFTY100-scoped FALLBACK_UNIVERSE (see its docstring
    for why this doesn't get a separately hand-typed larger list)."""
    key = _resolve_universe_key(filter_name)
    urls = UNIVERSE_CSV_URLS.get(key, UNIVERSE_CSV_URLS["NIFTY100"])
    headers = {"User-Agent": "Mozilla/5.0 (compatible; intraday/1.0)"}

    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200 and "Symbol" in resp.text:
                reader = csv.reader(io.StringIO(resp.text))
                rows = list(reader)
                out = []
                for row in rows[1:]:
                    if not row:
                        continue
                    sym = row[2].strip() if len(row) > 2 else row[0].strip()
                    name = row[0].strip() if row else sym
                    if sym:
                        out.append({"symbol": sym, "name": name})
                if out:
                    print(f"[auto_select] Fetched {len(out)} {key} constituents from {url}.")
                    return out
        except Exception as e:
            print(f"[auto_select] {key} fetch from {url} failed ({e}); trying next source.")

    if key != "NIFTY100":
        print(f"[auto_select] All {key} sources failed - degrading to the smaller curated "
              f"NIFTY100 fallback list ({len(FALLBACK_UNIVERSE)} names). This under-represents "
              f"the {key} universe you requested; check network access to nseindia.com/"
              f"niftyindices.com if this persists.")
    else:
        print(f"[auto_select] Using curated fallback universe ({len(FALLBACK_UNIVERSE)} names).")
    return [{"symbol": s, "name": s} for s in FALLBACK_UNIVERSE]


# ---------------------------------------------------------------------------
# Eligibility / liquidity pre-filter (MINIMUM tradability requirement)
# ---------------------------------------------------------------------------
def _fast_liquidity(symbol, min_price, min_vol, min_val):
    """Check liquidity using Angel One quote and daily OHLCV history.

    Returns (20-day average volume, estimated average traded value), or None
    when broker data is unavailable or fails the configured requirements.
    """
    try:
        quote = DataProvider.get_realtime_quote(symbol, "nse")
        daily = DataProvider.get_ohlcv(symbol, "1mo", "1d", "nse", limit=25)
        candles = daily.get("candles", []) if isinstance(daily, dict) else []
        if quote.get("error") or daily.get("error") or not candles:
            return None
        price = quote.get("last_price")
        volumes = [float(c.get("volume") or 0) for c in candles[-20:]]
        avg_vol = sum(volumes) / len(volumes) if volumes else 0
        if not price or avg_vol <= 0:
            return None
        traded_value = price * avg_vol
        if price >= min_price and avg_vol >= min_vol and traded_value >= min_val:
            return avg_vol, traded_value
    except Exception:
        return None
    return None


def _spread_ok(symbol, max_spread_pct, settings):
    """Validate spread only when the configured data adapter provides it.

    Angel One's LTP endpoint does not guarantee bid/ask fields, so missing
    spread data remains explicitly unknown rather than being fabricated.
    """
    if not max_spread_pct:
        return True
    try:
        quote = DataProvider.get_realtime_quote(symbol, "nse")
        bid, ask, mid = quote.get("bid"), quote.get("ask"), quote.get("last_price")
        if bid and ask and mid:
            spread_pct = (ask - bid) / mid * 100 if mid else 0
            return spread_pct <= max_spread_pct
    except Exception:
        return True
    return True


# ---------------------------------------------------------------------------
# Candidate Quality Score (watchlist-ranking ONLY, never a trade signal)
# ---------------------------------------------------------------------------
DEFAULT_WEIGHTS = {
    "liquidity": 20,
    "atr_suitability": 15,
    "prevday_trend": 15,
    "relative_strength": 15,
    "sector_strength": 10,
    "gap_quality": 10,
    "premarket_activity": 5,
    "event_risk": 5,
    "prevday_participation": 5,
}


def _norm_weights(settings):
    w = dict(DEFAULT_WEIGHTS)
    cfg = settings.get("candidate_score_weights") or {}
    for k, v in cfg.items():
        if k in w and isinstance(v, (int, float)):
            w[k] = float(v)
    total = sum(w.values()) or 1.0
    return {k: v / total for k, v in w.items()}


def _score_liquidity(traded_value, min_val):
    """0-100 liquidity/tradability sub-score. Higher traded value => better.
    Missing data => 0 contribution (never free PASS)."""
    if not traded_value or traded_value <= 0:
        return 0.0
    # Map traded value to a 0-100 scale relative to a comfortable minimum.
    # 1x min => ~40, 5x min => ~80, 20x+ min => 100.
    ratio = traded_value / max(min_val, 1.0)
    if ratio <= 1:
        return 40.0
    if ratio >= 20:
        return 100.0
    # log-ish scaling
    import math
    s = 40 + 60 * (math.log(ratio) / math.log(20))
    return max(0.0, min(100.0, s))


def _score_atr_suitability(atr_pct):
    """0-100: prefer intraday move of ~1.5%-4% (enough to trade, not chaos).
    Missing => 0 contribution."""
    if atr_pct is None:
        return 0.0
    if atr_pct < 1.0:
        return 30.0       # too quiet
    if atr_pct <= 4.0:
        return 100.0      # ideal band
    if atr_pct <= 6.0:
        return 70.0
    return 40.0          # very volatile / risky


def _score_prevday_trend(trend):
    """BULLISH/strong structure slightly favored for watchlist suitability."""
    return {
        "BULLISH": 100.0,
        "BEARISH": 80.0,   # still tradable (short side)
        "NEUTRAL": 60.0,
        "UNKNOWN": 0.0,
    }.get(trend, 0.0)


def _score_relative_strength(rs):
    return {
        "STRONG_RELATIVE_STRENGTH": 100.0,
        "NEUTRAL": 60.0,
        "WEAK_RELATIVE_STRENGTH": 30.0,
        "UNKNOWN": 0.0,
    }.get(rs, 0.0)


def _score_sector_strength(s):
    return {
        "STRONG_SECTOR": 100.0,
        "NEUTRAL_SECTOR": 60.0,
        "WEAK_SECTOR": 30.0,
        "UNKNOWN": 0.0,
    }.get(s, 0.0)


def _score_gap_quality(gq):
    """Gap quality is context (directional). For watchlist suitability we
    reward a *clear, non-extreme* gap (more likely to move). UNKNOWN => 0."""
    return {
        "GAP_CONTINUATION_CANDIDATE": 100.0,
        "BREAKOUT_CANDIDATE": 95.0,
        "BREAKDOWN_CANDIDATE": 90.0,
        "GAP_FILL_CANDIDATE": 60.0,
        "WEAK_GAP": 40.0,
        "AVOID": 20.0,
        "UNKNOWN": 0.0,
    }.get(gq, 0.0)


def _score_premarket_activity(a):
    return {
        "UNUSUAL_ACTIVITY": 100.0,
        "NORMAL_ACTIVITY": 70.0,
        "LOW_ACTIVITY": 40.0,
        "UNKNOWN": 0.0,
    }.get(a, 0.0)


def _score_event_risk(e):
    """Default UNKNOWN is conservative (treated as not-PASS)."""
    return {
        "LOW_EVENT_RISK": 100.0,
        "MEDIUM_EVENT_RISK": 60.0,
        "HIGH_EVENT_RISK": 30.0,
        "UNKNOWN": 50.0,   # unknown => neutral, NOT free pass
    }.get(e, 50.0)


def _score_participation(pd_volume, avg_volume):
    """Previous-day participation vs average volume. Missing => 0."""
    if not pd_volume or not avg_volume or avg_volume <= 0:
        return 0.0
    ratio = pd_volume / avg_volume
    if ratio >= 1.5:
        return 100.0
    if ratio >= 1.0:
        return 80.0
    if ratio >= 0.7:
        return 60.0
    return 40.0


def compute_candidate_quality(stock, settings):
    """Compute a 0-100 Candidate Quality score using ONLY valid pre-open data.

    Returns (score_0_100, components_dict). Never returns a BUY/SELL signal.
    This is a watchlist-ranking score only.
    """
    symbol = stock.get("symbol")
    weights = _norm_weights(settings)
    min_val = settings.get("min_avg_traded_value", 1_00_00_000)
    max_age = settings.get("max_data_age_seconds", 3000)

    # Fetch only daily (valid pre-open) + optional realtime quote for gap/pre-mkt.
    daily = DataProvider.get_ohlcv(symbol, "3mo", "1d", "nse")
    quote = DataProvider.get_realtime_quote(symbol, "nse")
    quote_ok = isinstance(quote, dict) and "error" not in quote

    prev_day = compute_previous_day_levels(daily.get("candles", []) if "error" not in daily else [])
    prev_close = prev_day.get("pdc")
    expected_open = (quote.get("open") if quote_ok and quote.get("open") else prev_close)

    gap = compute_gap_analysis(expected_open, prev_close, settings)
    # gap_quality is context-only; we feed daily trend + sector + rs + event.
    sector_info = compute_sector_strength(symbol, settings)
    rs_info = compute_relative_strength(symbol, settings)
    activity_info = compute_premarket_activity(quote if quote_ok else {}, float(prev_day.get("pd_volume")) if prev_day.get("pd_volume") else None)
    event_risk = classify_event_risk(symbol, quote if quote_ok else {})
    gap_q = compute_gap_quality(
        gap.get("gap_type", "UNKNOWN"),
        prev_day.get("pd_trend", "UNKNOWN"),
        "NEUTRAL",  # 15m trend may be invalid pre-open; default neutral
        settings.get("market_bias", "UNKNOWN"),
        sector_info.get("sector_strength", "UNKNOWN"),
        rs_info.get("relative_strength", "UNKNOWN"),
        event_risk,
        "UNKNOWN",  # liquidity handled separately by candidate-quality
    )
    if isinstance(gap_q, dict):
        gap_q = gap_q.get("quality", "UNKNOWN")

    # ATR suitability from previous-day range % (valid pre-open)
    atr_pct = prev_day.get("pd_range_percent")
    if atr_pct is None:
        # fall back to ATR14 / close
        atr14 = prev_day.get("pd_atr")
        if atr14 and prev_close:
            atr_pct = atr14 / prev_close * 100

    # liquidity from pre-filter info if available
    liq_val = stock.get("_traded_value") or (prev_day.get("pd_volume") and prev_close and prev_day["pd_volume"] * prev_close) or 0

    comp = {
        "liquidity": _score_liquidity(liq_val, min_val),
        "atr_suitability": _score_atr_suitability(atr_pct),
        "prevday_trend": _score_prevday_trend(prev_day.get("pd_trend", "UNKNOWN")),
        "relative_strength": _score_relative_strength(rs_info.get("relative_strength", "UNKNOWN")),
        "sector_strength": _score_sector_strength(sector_info.get("sector_strength", "UNKNOWN")),
        "gap_quality": _score_gap_quality(gap_q if isinstance(gap_q, str) else "UNKNOWN"),
        "premarket_activity": _score_premarket_activity(activity_info.get("premarket_activity", "UNKNOWN")),
        "event_risk": _score_event_risk(event_risk),
        "prevday_participation": _score_participation(prev_day.get("pd_volume"), stock.get("_avg_volume")),
    }

    score = sum(comp.get(k, 0.0) * weights.get(k, 0.0) for k in weights)
    return round(score, 1), comp


# ---------------------------------------------------------------------------
# Selection pipeline
# ---------------------------------------------------------------------------
def select_stocks(settings, top_n=None, write_summary=True):
    """Build the daily Core Watchlist.

    Steps:
      1. Universe (NIFTY100 / fallback).
      2. Eligibility/liquidity pre-filter (MINIMUM tradability requirement).
      3. Wider pre-ranking pool (candidate_pool_size) to avoid the old ~25
         bottleneck excluding useful candidates.
      4. Candidate Quality ranking using only valid pre-open data.
      5. Top-N Core Watchlist (watchlist_size).

    Returns (symbols_list, ranked_list_of_dicts).
    """
    top_n = top_n or settings.get("watchlist_size", 30)
    max_sym = settings.get("max_symbols", 40)
    top_n = min(int(top_n), int(max_sym))
    pool_size = settings.get("candidate_pool_size", 50)

    universe_filter = settings.get("universe_filter", "NIFTY100")
    min_price = settings.get("min_price", 50)
    min_vol = settings.get("min_avg_volume", 100_000)
    min_val = settings.get("min_avg_traded_value", 1_00_00_000)
    max_spread = settings.get("max_spread_percent", 0.5)

    universe = get_universe(universe_filter)
    print(f"[auto_select] Universe size: {len(universe)}")

    # Step 2+: eligibility pre-filter + liquidity capture, keep a wide pool
    eligible = []
    for item in universe:
        sym = _norm_symbol(item["symbol"])
        liq = _fast_liquidity(sym, min_price, min_vol, min_val)
        if liq is None:
            continue
        avg_vol, traded_val = liq
        if not _spread_ok(sym, max_spread, settings):
            continue
        eligible.append({
            "symbol": sym,
            "name": item.get("name", sym),
            "_avg_volume": avg_vol,
            "_traded_value": traded_val,
        })
    # sort by liquidity (tradability) then take a WIDE pool for quality ranking
    eligible.sort(key=lambda x: (x.get("_traded_value") or 0), reverse=True)
    pool = eligible[:pool_size]
    print(f"[auto_select] Eligible after liquidity filter: {len(eligible)}; "
          f"pre-ranking pool (top {pool_size}): {len(pool)}")

    # Step 4: Candidate Quality ranking (pre-open safe)
    ranked = []
    for stock in pool:
        try:
            score, comp = compute_candidate_quality(stock, settings)
        except Exception as e:
            print(f"[auto_select] Candidate Quality failed for {stock['symbol']}: {e}")
            score, comp = 0.0, {}
        ranked.append({
            "symbol": stock["symbol"],
            "name": stock.get("name", stock["symbol"]),
            "candidate_quality": score,
            "candidate_components": comp,
        })

    ranked.sort(key=lambda x: x["candidate_quality"], reverse=True)
    top = ranked[:top_n]

    if write_summary:
        print(f"[auto_select] Top {len(top)} Core Watchlist by Candidate Quality:")
        for r in top:
            print(f"   {r['symbol']:14s} CQ={r['candidate_quality']:.1f}")

    # Return bare symbols + full ranked detail
    symbols = [r["symbol"] for r in top]
    return symbols, ranked


def write_config(symbols, config_path):
    """Rewrite intraday_config.json['stocks'] with bare symbols.

    Preserves ALL other config keys/settings (non-regression). Only the stocks
    list is replaced.
    """
    try:
        with open(config_path, "r") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    # Engine expects a list of {"symbol": "..."} objects (NOT bare strings).
    cfg["stocks"] = [{"symbol": s} for s in symbols]
    with open(config_path, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"[auto_select] Wrote {len(symbols)} symbols to {config_path}['stocks'].")
    return cfg


# ---------------------------------------------------------------------------
# Optional Dynamic Opportunity Pool (during market hours)
# ---------------------------------------------------------------------------
def dynamic_discover(settings, current_symbols, pool_extra=40):
    """Scan beyond the morning Top-N for stocks now showing unusual
    volume/movement. They must still pass eligibility/liquidity/risk.

    Returns a list of dicts (symbol, reason) up to dynamic_opportunity_pool_size.
    This NEVER generates trade signals; it only surfaces candidates for the
    live engine to evaluate.
    """
    if not settings.get("dynamic_scan_enabled", True):
        return []
    size = settings.get("dynamic_opportunity_pool_size", 5)
    universe = get_universe(settings.get("universe_filter", "NIFTY100"))
    min_price = settings.get("min_price", 50)
    min_vol = settings.get("min_avg_volume", 100_000)
    min_val = settings.get("min_avg_traded_value", 1_00_00_000)

    current = set(_norm_symbol(s) for s in current_symbols)
    candidates = []
    for item in universe:
        sym = _norm_symbol(item["symbol"])
        if sym in current:
            continue
        liq = _fast_liquidity(sym, min_price, min_vol, min_val)
        if liq is None:
            continue
        avg_vol, _ = liq
        if not _spread_ok(sym, settings.get("max_spread_percent", 0.5), settings):
            continue
        # unusual relative volume proxy: compare current quote volume to avg
        quote = DataProvider.get_realtime_quote(sym, "nse")
        if isinstance(quote, dict) and "error" not in quote:
            vol = quote.get("volume") or 0
            rvol = vol / avg_vol if avg_vol else 0
            if rvol >= settings.get("dynamic_min_rvol", 2.0):
                candidates.append({"symbol": sym, "name": item.get("name", sym),
                                    "reason": f"Unusual volume (RVOL~{rvol:.1f})", "rvol": round(rvol, 2)})
        if len(candidates) >= size:
            break
    return candidates[:size]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Build intraday Core Watchlist (Candidate Quality).")
    ap.add_argument("--config", default="intraday_config.json")
    ap.add_argument("--top", type=int, default=None)
    ap.add_argument("--pool", type=int, default=None)
    args = ap.parse_args()

    try:
        with open(args.config) as f:
            settings = json.load(f)
    except Exception as e:
        print(f"[auto_select] Could not load {args.config}: {e}")
        sys.exit(1)

    if args.pool:
        settings["candidate_pool_size"] = args.pool

    symbols, ranked = select_stocks(settings, top_n=args.top)
    write_config(symbols, args.config)


if __name__ == "__main__":
    main()