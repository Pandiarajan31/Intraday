#!/usr/bin/env python3
"""
Intraday Trading Engine for Indian Stocks
=========================================
Implements the general-purpose Indian equity intraday trading engine.

Core architecture:
- Daily + 15m + 5m timeframe hierarchy (Daily trend, 15m structure, 5m entry)
- Independent BUY and SELL engines with hard gates
- Hard gates ALWAYS override scores (scores are secondary)
- setup_id state machine: WAIT -> ALMOST -> IMMEDIATE -> IN_TRADE -> TARGET/STOP_LOSS/CANCEL
- Every analysis produces ONE canonical record used by dashboard, scanner, and MCP server
- Single source of truth - no recalc in different modules
"""
import datetime as dt
import hashlib
import json
import os
import time
import zoneinfo
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from shared import DataProvider, Indicators
import decision_log as decision_log_module
from patterns import evaluate_all_patterns

# Pre-market + opening-range addon (spec §17 handoff)
from premarket import (
    compute_opening_range,
    detect_failed_opening_breakout,
    data_freshness as prem_data_freshness,
    compute_sector_strength,
    compute_relative_strength,
    get_sector,
)
import positions as positions_module


def _engine_now() -> dt.datetime:
    """Current naive local time (== IST on the trading box), EXCEPT during a
    backtest where an as-of clock is active, in which case it returns the
    simulated instant so the engine only sees past data (no look-ahead).

    The backtest.clock import is lazy + guarded so live deployments that don't
    ship the backtest package behave exactly as before.
    """
    try:
        from backtest import clock as _bt_clock
        if _bt_clock.is_active():
            return _bt_clock.now_naive()
    except Exception:
        pass
    return dt.datetime.now()


# ---------------------------------------------------------------------------
# #16 Primary rejection reason — fixed priority order (expert review doc #16)
# A WAIT/ALMOST is explained by the HIGHEST-priority FAIL, with the rest
# listed as secondary. PASS/UNKNOWN alone never count as a blocker.
# ---------------------------------------------------------------------------
GATE_PRIORITY = [
    "data_quality", "liquidity", "session", "daily_trend", "15m_trend",
    "5m_structure", "market_regime", "vwap", "volume", "atr", "rr",
    "breakout", "breakdown", "overextension", "no_active_reversal", "not_exhausted",
    "index_alignment", "entry_freshness", "current_rr", "candle_quality", "score",
]


def _detect_reversal_candle(candles: List[dict], direction: str):
    """Well-known single/two-candle reversal patterns, checked against the
    LAST candle. Used two ways in this file:
      - As a block on a FRESH entry in `direction` (a bearish pattern is
        risk to a fresh BUY; a bullish pattern is risk to a fresh SELL).
      - As supporting evidence FOR an early-reversal signal in the
        opposite direction (see analyze_early_reversal below).
    Mirrors positions.py's _is_reversal_candle - kept as an independent
    copy here rather than a cross-module import, so engine.py's entry
    logic doesn't depend on the position-tracking module."""
    if len(candles) < 2:
        return False, None
    c = candles[-1]
    prev = candles[-2]
    body = abs(c["close"] - c["open"])
    rng = max(c["high"] - c["low"], 1e-9)
    upper_wick = c["high"] - max(c["close"], c["open"])
    lower_wick = min(c["close"], c["open"]) - c["low"]

    if direction == "BUY":
        shooting_star = (upper_wick / rng > 0.55) and (body / rng < 0.35)
        bearish_engulf = (
            c["close"] < c["open"] and prev["close"] > prev["open"]
            and c["open"] >= prev["close"] and c["close"] <= prev["open"]
        )
        if shooting_star:
            return True, "SHOOTING_STAR"
        if bearish_engulf:
            return True, "BEARISH_ENGULFING"
        return False, None
    else:
        hammer = (lower_wick / rng > 0.55) and (body / rng < 0.35)
        bullish_engulf = (
            c["close"] > c["open"] and prev["close"] < prev["open"]
            and c["open"] <= prev["close"] and c["close"] >= prev["open"]
        )
        if hammer:
            return True, "HAMMER_REVERSAL"
        if bullish_engulf:
            return True, "BULLISH_ENGULFING"
        return False, None


def analyze_early_reversal(m5_candles: List[dict], support: Optional[float], resistance: Optional[float],
                            atr_5m: Optional[float], rvol: Optional[float], settings: Dict[str, Any]) -> Dict[str, Any]:
    """A SEPARATE, explicitly lower-confidence signal type: fires closer to
    an actual turn than the confirmed BUY/SELL hard-gate does, by looking
    for exhaustion evidence BEFORE a breakout/breakdown confirms - i.e.
    the "before it goes high / before it goes down" signal, as distinct
    from the "confirmed, already moving" signal the hard gate produces.

    THIS IS A HEURISTIC, NOT A PREDICTION. It will be wrong more often
    than the confirmed signal - that's inherent to being early, not a
    bug. Treat it as a smaller, faster-managed, separately-labeled idea,
    never as a like-for-like substitute for a confirmed signal. It does
    NOT feed into buy_hard_gate/sell_hard_gate and never blends into the
    main BUY/SELL call.

    Looks for, in the direction of a potential bottom (EARLY_REVERSAL_BUY)
    or top (EARLY_REVERSAL_SELL):
      - price near the relevant level (within a small ATR multiple)
      - a reversal candle at that level
      - bullish/bearish RSI divergence (price extends further than RSI does)
      - a volume climax on the exhaustion candle
    """
    result = {
        "signal": None,           # "EARLY_REVERSAL_BUY" / "EARLY_REVERSAL_SELL" / None
        "confidence": 0,
        "reasons": [],
    }
    if not m5_candles or len(m5_candles) < 20:
        return result

    price = m5_candles[-1]["close"]
    proximity_atr_mult = settings.get("early_reversal_proximity_atr", 0.75)
    atr = atr_5m or 0

    rsi_series = Indicators.rsi(m5_candles, settings.get("rsi_period", 14)).get("values") or []
    window = m5_candles[-20:]
    rsi_window = rsi_series[-20:] if len(rsi_series) >= 20 else []

    def _score_direction(direction: str) -> Dict[str, Any]:
        score = 0
        reasons = []
        level = support if direction == "BUY" else resistance
        if level is None:
            return {"score": 0, "reasons": []}

        # 1. Proximity to the level
        if atr > 0:
            dist_atr = abs(price - level) / atr
            if dist_atr <= proximity_atr_mult:
                score += 30
                reasons.append(f"Price within {dist_atr:.2f}x ATR of {'support' if direction=='BUY' else 'resistance'}")
            else:
                return {"score": 0, "reasons": []}  # too far from the level - not an exhaustion setup, skip
        else:
            return {"score": 0, "reasons": []}

        # 2. Reversal candle in the FAVORABLE direction for this new signal
        #    (a bullish candle supports EARLY_REVERSAL_BUY, bearish supports SELL)
        is_rev, pattern = _detect_reversal_candle(window, "SELL" if direction == "BUY" else "BUY")
        if is_rev:
            score += 30
            reasons.append(f"Reversal candle at the level ({pattern})")

        # 3. RSI divergence: price makes a fresh extreme but RSI does NOT -
        #    momentum failing to confirm the move, classic exhaustion tell.
        if len(rsi_window) >= 10:
            closes = [c["close"] for c in window]
            if direction == "BUY":
                prior_low_idx = closes[:-1].index(min(closes[:-1]))
                if closes[-1] <= closes[prior_low_idx] and rsi_window[-1] > rsi_window[prior_low_idx]:
                    score += 25
                    reasons.append("Bullish RSI divergence (price lower low, RSI higher low)")
            else:
                prior_high_idx = closes[:-1].index(max(closes[:-1]))
                if closes[-1] >= closes[prior_high_idx] and rsi_window[-1] < rsi_window[prior_high_idx]:
                    score += 25
                    reasons.append("Bearish RSI divergence (price higher high, RSI lower high)")

        # 4. Volume climax on the exhaustion candle
        vols = [c.get("volume", 0) for c in window]
        if len(vols) >= 6 and vols[-1] > (sum(vols[-6:-1]) / 5) * 1.5:
            score += 15
            reasons.append("Volume climax on the exhaustion candle")

        return {"score": score, "reasons": reasons}

    buy_result = _score_direction("BUY")
    sell_result = _score_direction("SELL")

    # Require a real minimum bar (proximity ALONE is not enough - need at
    # least one of candle/divergence/volume on top of it) before firing.
    min_score = settings.get("early_reversal_min_score", 55)
    if buy_result["score"] >= min_score and buy_result["score"] >= sell_result["score"]:
        result["signal"] = "EARLY_REVERSAL_BUY"
        result["confidence"] = min(100, buy_result["score"])
        result["reasons"] = buy_result["reasons"]
    elif sell_result["score"] >= min_score:
        result["signal"] = "EARLY_REVERSAL_SELL"
        result["confidence"] = min(100, sell_result["score"])
        result["reasons"] = sell_result["reasons"]

    return result


def compute_exhaustion(m5_candles: List[dict], daily_candles: List[dict], price: Optional[float],
                       vwap: Optional[float], atr_5m: Optional[float], rsi: Optional[float],
                       early_reversal_signal: Optional[str], settings: Dict[str, Any]) -> Dict[str, Any]:
    """How STRETCHED is the current move, per direction (0-100 each).

    The BUY/SELL score is built almost entirely from trend-following
    inputs (trend, structure, VWAP side, breakout) that are strongest
    AFTER a move has already run - so on its own it peaks near the top
    (BUY) / bottom (SELL), exactly where a fresh entry is most likely to
    reverse on you. This measures the opposite question: how much of the
    move is already used up. exhaustion_buy high = an up-move that is
    late / likely to stall; exhaustion_sell high = a down-move that is
    late / likely to bounce.

    Inputs used (each adds points to the side it's stretching):
      - VWAP stretch: (price - vwap) / atr_5m beyond exhaustion_vwap_atr
      - Daily range used: today's high-low vs 14-day daily ATR, with price
        sitting at the extreme end of today's range
      - RSI extreme (>=70 / <=30)
      - Run length: N+ consecutive 5m closes in one direction
      - Momentum fading: last 3 bodies shrinking while price still pushes
      - analyze_early_reversal() agreeing that a turn is forming
    Heuristic, not a prediction - it says "late", not "will reverse".
    """
    result = {"exhaustion_buy": 0, "exhaustion_sell": 0,
              "exhaustion_reasons_buy": [], "exhaustion_reasons_sell": [],
              "vwap_stretch_atr": None, "day_range_used": None}
    if not m5_candles or price is None:
        return result

    buy = 0
    sell = 0
    rb: List[str] = []
    rs: List[str] = []

    # 1. VWAP stretch in ATR units
    vwap_lim = settings.get("exhaustion_vwap_atr", 2.0)
    if vwap and atr_5m:
        stretch = (price - vwap) / atr_5m
        result["vwap_stretch_atr"] = round(stretch, 2)
        if stretch >= vwap_lim:
            buy += 30
            rb.append(f"Price {stretch:.1f}x ATR above VWAP (limit {vwap_lim})")
        elif stretch <= -vwap_lim:
            sell += 30
            rs.append(f"Price {abs(stretch):.1f}x ATR below VWAP (limit {vwap_lim})")

    # 2. Daily range used vs normal daily ATR (today's partial day excluded
    #    from the ATR, same convention as compute_daily_liquidity)
    try:
        last_day = pd.Timestamp(m5_candles[-1]["timestamp"]).date()
        today = [c for c in m5_candles if pd.Timestamp(c["timestamp"]).date() == last_day]
    except Exception:
        today = []
    hist_daily = daily_candles[:-1] if daily_candles and len(daily_candles) > 15 else []
    d_atr_vals = (Indicators.atr(hist_daily, 14).get("values") or []) if hist_daily else []
    d_atr = d_atr_vals[-1] if d_atr_vals else None
    if today and d_atr:
        day_high = max(c["high"] for c in today)
        day_low = min(c["low"] for c in today)
        day_rng = day_high - day_low
        used = day_rng / d_atr
        result["day_range_used"] = round(used, 2)
        range_lim = settings.get("exhaustion_range_used", 0.8)
        if used >= range_lim and day_rng > 0:
            pos = (price - day_low) / day_rng  # 0 = at day low, 1 = at day high
            if pos >= 0.8:
                buy += 25
                rb.append(f"Already moved {used:.0%} of normal daily range, price near day high")
            elif pos <= 0.2:
                sell += 25
                rs.append(f"Already moved {used:.0%} of normal daily range, price near day low")

    # 3. RSI extreme
    if rsi is not None:
        if rsi >= 70:
            buy += 20
            rb.append(f"RSI {rsi:.0f} overbought")
        elif rsi <= 30:
            sell += 20
            rs.append(f"RSI {rsi:.0f} oversold")

    # 4. Run length - consecutive closes in one direction
    run_lim = settings.get("exhaustion_run_bars", 5)
    closes = [c["close"] for c in m5_candles[-(run_lim + 6):]]
    up_run = down_run = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] > closes[i - 1] and down_run == 0:
            up_run += 1
        elif closes[i] < closes[i - 1] and up_run == 0:
            down_run += 1
        else:
            break
    if up_run >= run_lim:
        buy += 15
        rb.append(f"{up_run} green closes in a row")
    elif down_run >= run_lim:
        sell += 15
        rs.append(f"{down_run} red closes in a row")

    # 5. Momentum fading - bodies shrinking while price still pushes
    if len(m5_candles) >= 3:
        last3 = m5_candles[-3:]
        bodies = [abs(c["close"] - c["open"]) for c in last3]
        shrinking = bodies[0] > bodies[1] > bodies[2]
        if shrinking and last3[-1]["close"] > last3[0]["close"]:
            buy += 10
            rb.append("Candle bodies shrinking on the push up (momentum fading)")
        elif shrinking and last3[-1]["close"] < last3[0]["close"]:
            sell += 10
            rs.append("Candle bodies shrinking on the push down (momentum fading)")

    # 6. Early-reversal detector agrees a turn is forming
    if early_reversal_signal == "EARLY_REVERSAL_BUY":
        sell += 25
        rs.append("Early-reversal signs of a bottom forming")
    elif early_reversal_signal == "EARLY_REVERSAL_SELL":
        buy += 25
        rb.append("Early-reversal signs of a top forming")

    result["exhaustion_buy"] = min(100, buy)
    result["exhaustion_sell"] = min(100, sell)
    result["exhaustion_reasons_buy"] = rb
    result["exhaustion_reasons_sell"] = rs
    return result


ENTRY_ZONE_RANK = {"GOOD": 5, "EARLY": 4, "WAIT": 3, "LATE": 2, "DONT_CHASE": 1, "NO_TRADE": 0}


def _parse_hhmm(value: str) -> dt.time:
    return dt.datetime.strptime(value, "%H:%M").time()


def compute_entry_zone(direction: str, score: int, price: Optional[float], level: Optional[float],
                       broken: bool, atr_5m: Optional[float], vwap: Optional[float],
                       exhausted: bool, exhaustion_score: int, early_reversal_signal: Optional[str],
                       current_rr: Optional[float], entry_too_far: bool, obstacle: bool,
                       m5_candles: List[dict], now_t: dt.time,
                       index_ok: bool, index_name: str, index_dir: str,
                       day_status: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    """THE consolidated manual-entry verdict for `direction` - one label
    that already includes every check, so the trader doesn't have to read
    individual values:

      GOOD        - level broken (5m close >= confirm %), score >= 75,
                    <= 0.5 ATR past the level, fresh move -> full size
      EARLY       - within 0.3% before the level, score >= 65, fresh move
                    -> half size
      WAIT        - setup OK but not ready (too far from level, break not
                    confirmed, opening 30 min, lunch hours)
      LATE        - stretched from VWAP / poor RR left / drifted from
                    entry / stop would be too wide
      DONT_CHASE  - exhausted, > 0.5 ATR past the level, or a turn is
                    forming against you - wait for a pullback
      NO_TRADE    - daily limit hit, too late in the day, against the live
                    index, score too low, wrong side of VWAP, or the move
                    is too small to cover charges

    Also returns the ready-to-use plan: entry, structural stop, T1/T2/T3
    (1R/2R/3R), quantity (risk-based, from settings.paper_risk) and the
    reward:risk at T2 AFTER charges.

    Heuristic, not a guarantee - it removes the entries it can see are
    bad; it cannot know about news or large orders that haven't printed.
    """
    is_buy = direction == "BUY"
    reasons: List[str] = []
    out: Dict[str, Any] = {
        "zone": "NO_TRADE", "size": "NONE", "reasons": reasons,
        "entry": None, "stop_loss": None, "t1": None, "t2": None, "t3": None,
        "risk_per_share": None, "quantity": None, "net_rr_t2": None,
        "extension_atr": None, "distance_pct": None, "vwap_stretch_atr": None,
    }

    def _done(zone: str, reason: Optional[str] = None) -> Dict[str, Any]:
        out["zone"] = zone
        if reason:
            reasons.append(reason)
        if zone not in ("GOOD", "EARLY"):
            out["size"] = "NONE"
            out["quantity"] = None
        return out

    good_score = settings.get("entry_zone_good_score", 75)
    early_score = settings.get("entry_zone_early_score", 65)
    min_score = settings.get("entry_zone_min_score", 55)
    early_dist = settings.get("entry_zone_early_distance_pct", 0.3)
    max_ext = settings.get("max_entry_extension_atr", 0.5)
    max_vwap = settings.get("entry_zone_max_vwap_atr", 1.5)
    min_rr = settings.get("min_rr", 2.0)
    min_net_rr = settings.get("entry_zone_min_net_rr", 1.5)
    entry_start = _parse_hhmm(settings.get("entry_start_time", "09:45"))
    entry_end = _parse_hhmm(settings.get("entry_end_time", "14:30"))
    lunch_start = _parse_hhmm(settings.get("lunch_start_time", "12:00"))
    lunch_end = _parse_hhmm(settings.get("lunch_end_time", "13:30"))
    in_lunch = lunch_start <= now_t < lunch_end

    if price is None or not atr_5m:
        return _done("NO_TRADE", "No live price / ATR")

    # 1. Discipline + clock - nothing else matters if these fail
    if day_status.get("blocked"):
        return _done("NO_TRADE", "Daily limit: " + "; ".join(day_status.get("reasons", [])))
    if now_t > entry_end:
        return _done("NO_TRADE", f"After {entry_end.strftime('%H:%M')} - too little time left for targets")
    if now_t < entry_start:
        return _done("WAIT", f"Opening volatility - fake breakouts common before {entry_start.strftime('%H:%M')}")

    # 2. Move already used up / turning against you
    against = "EARLY_REVERSAL_SELL" if is_buy else "EARLY_REVERSAL_BUY"
    if early_reversal_signal == against:
        return _done("DONT_CHASE", f"Possible turn {'DOWN' if is_buy else 'UP'} forming against this trade")
    if exhausted:
        return _done("DONT_CHASE", f"Move exhausted (exhaustion {exhaustion_score})")

    # 3. Market + setup quality
    if not index_ok:
        return _done("NO_TRADE", f"{index_name} is {index_dir} - don't {direction} against the index")
    if score < min_score:
        return _done("NO_TRADE", f"Score {score} < {min_score}")
    if vwap:
        stretch = (price - vwap) / atr_5m if is_buy else (vwap - price) / atr_5m
        out["vwap_stretch_atr"] = round(stretch, 2)
        if stretch <= 0:
            return _done("NO_TRADE", f"Price on wrong side of VWAP for {direction}")

    # 4. Trade plan - structural stop, R targets, charges
    plan = positions_module.structural_stop_plan(m5_candles, direction, price, atr_5m, settings)
    if not plan:
        return _done("NO_TRADE", "Not enough candles for a stop")
    risk = plan["risk_per_share"]
    cost = positions_module.round_trip_cost(price, settings)
    net_rr_t2 = (2 * risk - cost) / (risk + cost) if risk > 0 else 0
    out.update({
        "entry": round(price, 2), "stop_loss": plan["stop_loss"],
        "t1": plan["t1"], "t2": plan["t2"], "t3": plan["t3"],
        "risk_per_share": risk, "net_rr_t2": round(net_rr_t2, 2),
    })
    if net_rr_t2 < min_net_rr:
        return _done("NO_TRADE", f"Move too small - after charges RR at T2 is only {net_rr_t2:.2f} (need {min_net_rr})")

    late_reasons = []
    if plan["too_wide"]:
        late_reasons.append(f"Stop would be {plan['stop_atr']}x ATR away - price too far from structure")
    if out["vwap_stretch_atr"] is not None and out["vwap_stretch_atr"] > max_vwap:
        late_reasons.append(f"{out['vwap_stretch_atr']}x ATR from VWAP (max {max_vwap})")
    if current_rr is not None and current_rr < min_rr:
        late_reasons.append(f"Room left to target too small (RR {current_rr}, need {min_rr})")
    if entry_too_far:
        late_reasons.append("Price drifted too far from the original entry")

    # 5. Where is price vs the level
    if broken and level:
        ext = (price - level) / atr_5m if is_buy else (level - price) / atr_5m
        out["extension_atr"] = round(ext, 2)
        if ext > max_ext:
            return _done("DONT_CHASE", f"{ext:.2f}x ATR past the level (max {max_ext}) - wait for pullback to {level}")
        if late_reasons:
            reasons.extend(late_reasons)
            return _done("LATE")
        need = good_score + (settings.get("lunch_score_bonus", 5) if in_lunch else 0)
        if score < need:
            return _done("WAIT", f"Level broken but score {score} < {need}"
                         + (" (lunch hours need more)" if in_lunch else "") + " - wait for more confirmation")
        out["size"] = "FULL"
        _done("GOOD", f"Level {level} broken, score {score}, {ext:.2f}x ATR past level")
    elif level:
        dist = (level - price) / price * 100 if is_buy else (price - level) / price * 100
        out["distance_pct"] = round(dist, 2)
        if late_reasons:
            reasons.extend(late_reasons)
            return _done("LATE")
        if dist < 0:
            return _done("WAIT", f"Price past {level} but no confirming 5m close yet")
        if score < early_score:
            return _done("NO_TRADE", f"Score {score} < {early_score} for an early entry")
        if dist > early_dist:
            return _done("WAIT", f"Good setup but {dist:.2f}% from {level} (enter within {early_dist}%)")
        if in_lunch:
            return _done("WAIT", "Lunch hours - early entries before the break fail more often")
        out["size"] = "HALF"
        _done("EARLY", f"{dist:.2f}% before {level}, score {score} - add the other half if it turns GOOD")
    else:
        return _done("NO_TRADE", f"No {'resistance' if is_buy else 'support'} level found")

    # 6. Size adjustments + quantity
    if exhaustion_score >= 25 and out["size"] == "FULL":
        out["size"] = "HALF"
        reasons.append(f"Move stretched (exhaustion {exhaustion_score}) - half size")
    if obstacle and out["size"] == "FULL":
        out["size"] = "HALF"
        reasons.append("Structure level sits before T1 - half size")
    pr = settings.get("paper_risk", {}) or {}
    equity = pr.get("account_equity", 100000)
    risk_amt = equity * pr.get("risk_per_trade_percent", 0.5) / 100
    qty = int(risk_amt // (risk + cost)) if risk + cost > 0 else 0
    max_value = equity * pr.get("max_position_value_percent", 100) / 100
    qty = min(qty, int(max_value // price))
    if out["size"] == "HALF":
        qty //= 2
    out["quantity"] = qty
    if qty < 1:
        return _done("NO_TRADE", "Risk limits don't allow even 1 share at this stop")
    return out


def find_primary_blocker(record, buy_conditions, sell_conditions,
                         buy_score, sell_score, settings):
    """Return (primary_blocker, secondary_blockers) explaining a non-IMMEDIATE
    outcome, using the canonical priority order from review doc #16."""
    failed = []
    bc = buy_conditions or {}
    sc = sell_conditions or {}
    # Data-quality failures (e.g. insufficient candles) live in
    # record["failed_conditions"], not in the per-side gate dicts.
    data_quality_failed = bool(record.get("failed_conditions")) and "data_quality" in record.get("failed_conditions", [])

    def _status(key):
        bv = bc.get(key)
        sv = sc.get(key)
        if bv == "FAIL" or sv == "FAIL":
            return "FAIL"
        if bv == "N/A" or sv == "N/A":
            return "N/A"
        return "PASS"

    for key in GATE_PRIORITY:
        if key == "data_quality":
            if data_quality_failed:
                failed.append(key)
        elif key in ("breakout", "breakdown"):
            if (bc.get("breakout") == "FAIL") or (sc.get("breakdown") == "FAIL"):
                failed.append(key)
        else:
            st = _status(key)
            if st in ("FAIL", "N/A"):
                failed.append(key)

    if not failed:
        if buy_score < settings.get("buy_score_threshold", 65) or \
           sell_score < settings.get("sell_score_threshold", 65):
            failed.append("score")

    if not failed:
        return (None, [])

    return (failed[0], [k for k in failed[1:]])


def _structural_swing_levels(candles: List[dict], settings: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """#12 helper: most recent structural swing low/high from completed candles.

    A swing high is a bar whose high exceeds its left and right neighbours by at
    least `swing_tolerance`; a swing low is the mirror. Returns the most recent
    confirmed swing low (for BUY stop) and swing high (for SELL stop).
    """
    tol = settings.get("swing_tolerance_percent", 0.1) / 100
    if not candles or len(candles) < 5:
        return {"swing_low": None, "swing_high": None}
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    n = len(candles)
    swing_low = swing_high = None
    for i in range(2, n - 2):
        h, l = highs[i], lows[i]
        if all(h >= highs[j] * (1 - tol) for j in (i - 2, i - 1, i + 1, i + 2)):
            swing_high = h  # last assignment keeps the most recent
        if all(l <= lows[j] * (1 + tol) for j in (i - 2, i - 1, i + 1, i + 2)):
            swing_low = l
    return {"swing_low": swing_low, "swing_high": swing_high}



# State machine (per symbol per day)
# ---------------------------------------------------------------------------

class SetupStateStore:
    """Tracks setup state per symbol to prevent duplicate signals."""
    
    def __init__(self):
        self._states: Dict[str, Dict[str, Any]] = {}
    
    def _key(self, symbol: str) -> str:
        # State resets each trading day
        return f"{symbol}:{dt.date.today().isoformat()}"
    
    def get(self, symbol: str) -> Dict[str, Any]:
        key = self._key(symbol)
        return self._states.get(key, {
            "setup_id": None,
            "signal_state": "WAIT",
            "candle_timestamp": None,
            "entry_price": None,
            "status": "NO_SETUP",
        })
    
    def update(self, symbol: str, state: Dict[str, Any]):
        key = self._key(symbol)
        self._states[key] = state
    
    def clear_old(self):
        today = dt.date.today().isoformat()
        expired = [k for k in self._states if not k.endswith(f":{today}")]
        for k in expired:
            del self._states[k]


# Global state store (shared across dashboard + MCP server)
STATE_STORE = SetupStateStore()



def get_completed_candles(candles: List[dict], interval_minutes: int, now=None) -> List[dict]:
    """
    P0-3: Return only COMPLETED candles for a given interval.

    A candle is "completed" only if its [open, close] window is fully in the
    past relative to `now`.  The currently-forming candle (e.g. 10:35-10:40 at
    10:37) is excluded so the engine never makes a final trend/structure/
    breakout decision on a candle that can still change.

    `now` defaults to the current Asia/Kolkata time.  Candle timestamps that
    are tz-naive are localized to Asia/Kolkata.
    """
    if not candles:
        return []
    if now is None:
        # Respect an active backtest as-of clock so "completed" is judged
        # against the simulated instant, not wall-clock time.
        try:
            from backtest import clock as _bt_clock
            now = _bt_clock.now_ist() if _bt_clock.is_active() else dt.datetime.now(zoneinfo.ZoneInfo("Asia/Kolkata"))
        except Exception:
            now = dt.datetime.now(zoneinfo.ZoneInfo("Asia/Kolkata"))
    elif now.tzinfo is None:
        now = now.replace(tzinfo=zoneinfo.ZoneInfo("Asia/Kolkata"))
    completed = []
    for c in candles:
        ts = c.get("timestamp")
        if ts is None:
            continue
        try:
            ct = pd.Timestamp(ts)
        except Exception:
            continue
        if ct.tzinfo is None:
            ct = ct.tz_localize("Asia/Kolkata")
        else:
            ct = ct.tz_convert("Asia/Kolkata")
        # The candle covers [ct, ct + interval). It is complete only if its
        # close time is strictly before `now`.
        if ct + pd.Timedelta(minutes=interval_minutes) <= now:
            completed.append(c)
    return completed

def normalize_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Flatten the nested `thresholds` sub-object into the top-level settings dict
    so the engine reads EXACTLY the values defined in intraday_config.json.

    The config nests threshold-related knobs under `settings.thresholds`
    (e.g. buy_score_threshold, sell_score_threshold, min_risk_reward, ...).
    Several engine code paths read these as top-level keys. Without this
    flattening, edits to intraday_config.json would have no effect.

    Nested threshold keys take precedence only when the top-level key is absent,
    so explicit top-level overrides still work.
    """
    th = settings.get("thresholds") or {}
    merged = dict(settings)
    for k, v in th.items():
        if k not in merged:
            merged[k] = v
    # Alias min_risk_reward -> min_rr (engine historically uses min_rr)
    if "min_risk_reward" in merged and "min_rr" not in settings:
        merged["min_rr"] = merged["min_risk_reward"]
    return merged


def generate_setup_id(symbol: str, candle_ts: str, direction: str) -> str:
    """Generate a stable setup_id based on symbol + candle timestamp + direction."""
    raw = f"{symbol}:{candle_ts}:{direction}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Market regime detection
# ---------------------------------------------------------------------------

def detect_market_regime(settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Determine overall market regime using benchmark (NIFTY 50).
    Classify: TRENDING_UP, TRENDING_DOWN, RANGE, HIGH_VOLATILITY, UNKNOWN.
    """
    settings = normalize_settings(settings)
    benchmark = settings.get("benchmark_symbol", "^NSEI")
    try:
        daily = DataProvider.get_ohlcv(benchmark, "1mo", "1d", "nse")
        m15 = DataProvider.get_ohlcv(benchmark, "5d", "15m", "nse")
        
        if "error" in daily or "error" in m15 or len(daily["candles"]) < 20:
            return {"regime": "UNKNOWN", "reason": "insufficient benchmark data"}
        
        closes = [c["close"] for c in daily["candles"]]
        last = closes[-1]
        
        # Check volatility (ATR%)
        atr = Indicators.atr(daily["candles"], settings.get("atr_period", 14))
        atr_vals = atr.get("values", [])
        atr_val = atr_vals[-1] if atr_vals else None
        
        m15_closes = [c["close"] for c in m15["candles"]]
        m15_last = m15_closes[-1]
        m15_prev = m15_closes[-10] if len(m15_closes) >= 10 else m15_closes[-2]
        
        # EMA20/50 for trend
        ema_fast = pd.Series(closes).ewm(span=settings.get("ema_fast", 20), adjust=False).mean().iloc[-1]
        ema_slow = pd.Series(closes).ewm(span=settings.get("ema_slow", 50), adjust=False).mean().iloc[-1]
        
        atr_pct = (atr_val / last * 100) if atr_val and last else None
        
        # High volatility check
        if atr_pct and atr_pct > 2.5:
            return {"regime": "HIGH_VOLATILITY", "reason": f"ATR% {atr_pct:.1f}% > 2.5%"}
        
        # Trend direction
        if ema_fast > ema_slow and m15_last > m15_prev:
            regime = "TRENDING_UP"
            reason = f"EMA20 {ema_fast:.0f} > EMA50 {ema_slow:.0f}, 15m rising"
        elif ema_fast < ema_slow and m15_last < m15_prev:
            regime = "TRENDING_DOWN"
            reason = f"EMA20 {ema_fast:.0f} < EMA50 {ema_slow:.0f}, 15m falling"
        else:
            regime = "RANGE"
            reason = "EMA20/50 mixed, price within range"
        
        return {
            "regime": regime,
            "reason": reason,
            "atr_pct": round(atr_pct, 2) if atr_pct else None,
            "ema20": round(float(ema_fast), 2),
            "ema50": round(float(ema_slow), 2),
            "benchmark": benchmark,
            "nifty_intraday": compute_index_intraday(benchmark, settings),
            "banknifty_intraday": compute_index_intraday(settings.get("banknifty_symbol", "^NSEBANK"), settings),
        }
    except Exception as e:
        return {"regime": "UNKNOWN", "reason": str(e)}


def compute_index_intraday(index_symbol: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    """What the index is doing RIGHT NOW (the regime above is daily-EMA
    based and can say TRENDING_UP on a day Nifty is falling hard). Most
    large caps follow the index intraday, so buying while it's falling is
    swimming against the tide.

    UP   = last price above today's open AND above 5m EMA20
    DOWN = below both;  FLAT = mixed.
    Index candles carry no volume, so EMA20 stands in for VWAP here."""
    out = {"symbol": index_symbol, "direction": "UNKNOWN", "change_from_open_pct": None}
    try:
        data = DataProvider.get_ohlcv(index_symbol, "5d", "5m", "nse", limit=200)
        candles = data.get("candles") or []
        if "error" in data or len(candles) < 21:
            return out
        last_day = pd.Timestamp(candles[-1]["timestamp"]).date()
        today = [c for c in candles if pd.Timestamp(c["timestamp"]).date() == last_day]
        if not today:
            return out
        day_open = today[0]["open"]
        last = candles[-1]["close"]
        ema20 = pd.Series([c["close"] for c in candles]).ewm(span=20, adjust=False).mean().iloc[-1]
        chg = (last - day_open) / day_open * 100 if day_open else 0
        if last > day_open and last > ema20:
            direction = "UP"
        elif last < day_open and last < ema20:
            direction = "DOWN"
        else:
            direction = "FLAT"
        out.update({"direction": direction, "change_from_open_pct": round(chg, 2),
                    "last": round(last, 2), "ema20_5m": round(float(ema20), 2)})
    except Exception as e:
        out["error"] = str(e)
    return out


# ---------------------------------------------------------------------------
# Trend detection
# ---------------------------------------------------------------------------

def compute_daily_trend(candles: List[dict], settings: Dict[str, Any]) -> str:
    """Compute DAILY trend: BULLISH / BEARISH / NEUTRAL."""
    if not candles or len(candles) < 50:
        return "NEUTRAL"
    
    closes = pd.Series([c["close"] for c in candles])
    ema_fast = closes.ewm(span=settings.get("ema_fast", 20), adjust=False).mean()
    ema_slow = closes.ewm(span=settings.get("ema_slow", 50), adjust=False).mean()
    
    last_fast = ema_fast.iloc[-1]
    last_slow = ema_slow.iloc[-1]
    last_close = closes.iloc[-1]
    close_5_ago = closes.iloc[-6] if len(closes) >= 6 else closes.iloc[0]
    
    # Higher highs / higher lows for BULLISH
    highs = [c["high"] for c in candles[-10:]]
    lows = [c["low"] for c in candles[-10:]]
    hh = highs[-1] > highs[-6] if len(highs) >= 6 else False
    hl = lows[-1] > lows[-6] if len(lows) >= 6 else False
    
    # Lower highs / lower lows for BEARISH
    lh = highs[-1] < highs[-6] if len(highs) >= 6 else False
    ll = lows[-1] < lows[-6] if len(lows) >= 6 else False
    
    if last_fast > last_slow and last_close > last_slow and hh and hl:
        return "BULLISH"
    elif last_fast < last_slow and last_close < last_slow and lh and ll:
        return "BEARISH"
    elif last_fast > last_slow and last_close > last_slow:
        return "BULLISH"  # trend but no structure confirmation
    elif last_fast < last_slow and last_close < last_slow:
        return "BEARISH"
    return "NEUTRAL"


def compute_15m_trend(candles: List[dict], settings: Dict[str, Any]) -> str:
    """Compute ONE authoritative 15m trend: BULLISH / BEARISH / NEUTRAL."""
    if not candles or len(candles) < 50:
        return "NEUTRAL"
    
    closes = pd.Series([c["close"] for c in candles])
    ema_fast = closes.ewm(span=settings.get("ema_fast", 20), adjust=False).mean()
    ema_slow = closes.ewm(span=settings.get("ema_slow", 50), adjust=False).mean()
    
    last_fast = ema_fast.iloc[-1]
    last_slow = ema_slow.iloc[-1]
    last_close = closes.iloc[-1]
    
    # VWAP check
    vwap = Indicators.vwap(candles)
    vwap_vals = vwap.get("values", [])
    vwap_last = vwap_vals[-1] if vwap_vals else None
    
    # Momentum (last 5 closes)
    mom = closes.iloc[-1] > closes.iloc[-5] if len(closes) >= 5 else False
    
    bullish = last_fast > last_slow and last_close > last_slow
    bearish = last_fast < last_slow and last_close < last_slow
    
    if vwap_last is not None:
        bullish = bullish and last_close > vwap_last
        bearish = bearish and last_close < vwap_last
    
    if bullish and mom:
        return "BULLISH"
    elif bearish and not mom:
        return "BEARISH"
    elif bullish or bearish:
        # Partial alignment - use momentum to decide
        return "BULLISH" if mom else "BEARISH"
    return "NEUTRAL"


def compute_5m_structure(candles: List[dict], settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compute 5m entry structure: bullish/bearish confirmation.
    Does NOT generate trades from 5m trend alone.
    """
    if not candles or len(candles) < 20:
        return {"structure": "NEUTRAL", "rsi": None, "rvol": None, "body_ratio": None}
    
    closes = pd.Series([c["close"] for c in candles])
    ema_fast = closes.ewm(span=settings.get("ema_fast", 20), adjust=False).mean()
    ema_slow = closes.ewm(span=settings.get("ema_slow", 50), adjust=False).mean()
    last_close = closes.iloc[-1]
    last_fast = ema_fast.iloc[-1]
    last_slow = ema_slow.iloc[-1]
    
    # RSI
    ind = Indicators.compute_summary(candles)
    rsi = ind.get("rsi14")
    
    # RVOL (relative volume): TIME-OF-DAY comparison (current 10:35 vs historical 10:35)
    volumes = [c["volume"] for c in candles]
    current_vol = volumes[-1]
    
    # Try time-of-day RVOL if timestamps available
    try:
        if candles and "timestamp" in candles[-1]:
            current_time = pd.to_datetime(candles[-1]["timestamp"])
            current_time_slot = current_time.time()
            
            # Find all historical candles at same time slot (excluding current candle)
            same_time_vols = []
            for c in candles[:-1]:
                if "timestamp" in c:
                    c_time = pd.to_datetime(c["timestamp"]).time()
                    # Match hour and minute (5m slot)
                    if c_time.hour == current_time_slot.hour and c_time.minute == current_time_slot.minute:
                        same_time_vols.append(c["volume"])
            
            # Need at least 5 historical samples for reliable time-of-day comparison
            if len(same_time_vols) >= 5:
                avg_vol_tod = sum(same_time_vols) / len(same_time_vols)
                rvol = current_vol / avg_vol_tod if avg_vol_tod > 0 else 1.0
            else:
                # CRITICAL: No fallback - mark as UNKNOWN if < 5 samples
                # Expert requirement: RVOL must use time-of-day comparison
                # If insufficient historical data → RVOL = None → no IMMEDIATE signal
                rvol = None
        else:
            # No timestamps - cannot do time-of-day comparison
            rvol = None
    except Exception:
        # On any error - mark as UNKNOWN
        rvol = None
    
    # Body ratio of last candle
    last_c = candles[-1]
    hl = last_c["high"] - last_c["low"]
    body = abs(last_c["close"] - last_c["open"])
    body_ratio = body / hl if hl > 0 else 0
    
    # Determine structure
    if last_fast > last_slow and last_close > last_fast:
        structure = "BULLISH"
        # RSI rising
        if rsi is not None and rsi < settings.get("rsi_bull_min", 50):
            structure = "BULLISH_WEAK"  # RSI below threshold but trend up
    elif last_fast < last_slow and last_close < last_fast:
        structure = "BEARISH"
        if rsi is not None and rsi > settings.get("rsi_bear_max", 50):
            structure = "BEARISH_WEAK"
    else:
        structure = "NEUTRAL"
    
    return {
        "structure": structure,
            "rsi": round(rsi, 2) if rsi is not None else None,
            "rvol": round(rvol, 2) if rvol is not None else None,
            "body_ratio": round(body_ratio, 2) if body_ratio is not None else None,
        "ema20": round(float(last_fast), 2) if last_fast is not None else None,
        "ema50": round(float(last_slow), 2) if last_slow is not None else None,
    }


# ---------------------------------------------------------------------------
# Support/Resistance and breakout detection
# ---------------------------------------------------------------------------

def find_levels(candles: List[dict], lookback: int = 20) -> Dict[str, Any]:
    """Find meaningful support/resistance from swing points."""
    if not candles or len(candles) < lookback:
        return {"support": None, "resistance": None, "near_support": [], "near_resistance": []}
    
    use = candles[-lookback:]
    closes = [c["close"] for c in use]
    current = closes[-1]
    
    # Swing pivots
    highs, lows = [], []
    for i in range(2, len(use) - 2):
        if use[i]["high"] > use[i-1]["high"] and use[i]["high"] > use[i-2]["high"] and \
           use[i]["high"] > use[i+1]["high"] and use[i]["high"] > use[i+2]["high"]:
            highs.append(use[i]["high"])
        if use[i]["low"] < use[i-1]["low"] and use[i]["low"] < use[i-2]["low"] and \
           use[i]["low"] < use[i+1]["low"] and use[i]["low"] < use[i+2]["low"]:
            lows.append(use[i]["low"])
    
    # Cluster nearby levels (within 0.3%)
    def cluster(values):
        if not values:
            return []
        values.sort()
        clusters = [[values[0]]]
        for v in values[1:]:
            if abs(v - clusters[-1][-1]) / clusters[-1][-1] < 0.003:
                clusters[-1].append(v)
            else:
                clusters.append([v])
        return [round(sum(c)/len(c), 2) for c in clusters]
    
    resistances = [r for r in cluster(highs) if r > current]
    supports = [s for s in cluster(lows) if s < current]
    
    # Nearest resistance (just above price)
    resistance = min(resistances) if resistances else None
    # Nearest support (just below price)
    support = max(supports) if supports else None
    
    return {
        "support": support,
        "resistance": resistance,
        "near_support": supports[:2],
        "near_resistance": resistances[:2],
    }


def detect_breakout(candles: List[dict], level: float, direction: str, settings: Dict[str, Any]) -> bool:
    """
    Detect breakout (close > resistance for BUY) or breakdown (close < support for SELL).
    Uses CLOSED candles only - never triggers on incomplete candles.
    """
    if not candles or level is None:
        return False
    
    last = candles[-1]  # This MUST be a closed 5m candle
    tolerance = settings.get("breakout_tolerance_percent", 0.05) / 100
    
    if direction == "BUY":
        # Close above resistance (wick above is NOT a breakout)
        return last["close"] > level * (1 + tolerance) and last["close"] > last["open"]
    else:
        # Close below support (wick below is NOT a breakdown)
        return last["close"] < level * (1 - tolerance) and last["close"] < last["open"]


# ---------------------------------------------------------------------------
# Main evaluation engine
# ---------------------------------------------------------------------------


def compute_daily_liquidity(daily_candles: List[dict]) -> Dict[str, Any]:
    """
    P0-4: Genuine 20-day daily liquidity from DAILY candles only.

    Uses the previous ~20 valid trading sessions of DAILY data:
        avg_volume_20d      = mean(daily volume)
        avg_traded_value_20d = mean(close * volume)

    Crucially, the *current* (most recent) daily candle is the partial
    trading-day and is EXCLUDED from the historical average.  Today's partial
    volume is never used as a historical substitute.

    Returns UNKNOWN (avg_volume=None) when fewer than 20 valid sessions are
    available, so the engine cannot manufacture a false liquidity PASS.
    """
    MIN_SESSIONS = 20
    if not daily_candles or len(daily_candles) < MIN_SESSIONS + 1:
        return {"avg_volume": None, "avg_traded_value": None,
                "sessions_used": len(daily_candles) if daily_candles else 0,
                "status": "INSUFFICIENT_HISTORY"}
    # Exclude today's partial session (last candle).
    hist = daily_candles[:-1][-MIN_SESSIONS:]
    vols = [c.get("volume") for c in hist if c.get("volume") is not None]
    clos = [c.get("close") for c in hist if c.get("close") is not None]
    if len(vols) < MIN_SESSIONS or len(clos) < MIN_SESSIONS:
        return {"avg_volume": None, "avg_traded_value": None,
                "sessions_used": len(hist), "status": "INSUFFICIENT_HISTORY"}
    avg_volume = float(sum(vols)) / len(vols)
    avg_traded_value = float(sum(v * c for v, c in zip(vols, clos))) / len(vols)
    return {"avg_volume": avg_volume, "avg_traded_value": avg_traded_value,
            "sessions_used": len(hist), "status": "OK"}

def evaluate_stock(stock: Dict[str, Any], settings: Dict[str, Any], market_regime: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluate a single stock. Returns the canonical analysis record.
    
    This is the SINGLE SOURCE OF TRUTH. Dashboard, scanner, MCP server, and alerts
    all consume this same record.
    """
    settings = normalize_settings(settings)
    symbol = DataProvider.normalize_symbol(stock["symbol"])
    name = stock.get("name", symbol)

    now = _engine_now()
    now_t = now.time()
    
    # ------------------------------------------------------------------
    # Initialize canonical record
    # ------------------------------------------------------------------
    record: Dict[str, Any] = {
        "symbol": symbol,
        "name": name,
        "analysis_timestamp": now.isoformat(),
        "data_timestamp": None,
        "data_age_seconds": None,
        "signal": "WAIT",
        "setup_id": None,
        "candle_timestamp": None,
        "price": None,
        "buy_score": 0,
        "sell_score": 0,
        "buy_hard_gate": False,
        "sell_hard_gate": False,
        "buy_conditions": {},
        "sell_conditions": {},
        "buy_failed_conditions": [],
        "sell_failed_conditions": [],
        "market_regime": market_regime.get("regime", "UNKNOWN"),
        "daily_trend": "NEUTRAL",
        "m15_trend": "NEUTRAL",
        "m5_structure": "NEUTRAL",
        "vwap": None,
        "rsi": None,
        "rvol": None,
        "atr_5m": None,
        "atr_15m": None,
        "support": None,
        "resistance": None,
        "entry": None,
        "stop_loss": None,
        "target_1": None,
        "target_2": None,
        "target_3": None,
        "risk_reward": None,
        "liquidity_status": "UNKNOWN",
        "event_risk": "UNKNOWN",
        "session_status": "UNKNOWN",
        "failed_conditions": [],
        "reason": "Initializing",
        "factor_status": {},
        "satisfied": [],
        "missing": [],
        "score_breakdown": {},
        "score_summary": [],
        "in_zone": False,
        "confirmed": False,
        "breakout": False,
        "breakdown": False,
        "volume_signal": "NORMAL",
        "body_ratio": None,
        "overextended": False,
        "spread_percent": None,
        "avg_volume": None,
        "avg_traded_value": None,
        "condition_detail": {},
    }
    
    # ------------------------------------------------------------------
    # Check trading session
    # ------------------------------------------------------------------
    entry_start = dt.datetime.strptime(settings.get("entry_start_time", "09:45"), "%H:%M").time()
    entry_end = dt.datetime.strptime(settings.get("entry_end_time", "14:45"), "%H:%M").time()
    # #9 Explicit WATCH/ALMOST start time. WATCH-tier monitoring may begin at
    # market open (09:15) for early visibility, but IMMEDIATE trade eligibility
    # still requires the entry window (09:45+). Defaults to 09:15.
    watch_start = dt.datetime.strptime(settings.get("watch_start_time", "09:15"), "%H:%M").time()
    square_off = dt.datetime.strptime(settings.get("square_off_time", "15:15"), "%H:%M").time()
    market_open = dt.time(9, 15)
    market_close = dt.time(15, 30)
    record["watch_start_time"] = watch_start.strftime("%H:%M")
    
    if now_t < market_open or now_t > market_close:
        record["signal"] = "WAIT"
        record["reason"] = "Outside market hours"
        record["session_status"] = "MARKET_CLOSED"
        record["failed_conditions"] = ["session"]
        return record
    
    if now_t >= square_off:
        record["signal"] = "WAIT"
        record["reason"] = "Square-off time reached"
        record["session_status"] = "SQUARE_OFF"
        record["failed_conditions"] = ["session"]
        return record
    
    in_entry_window = entry_start <= now_t <= entry_end
    record["session_status"] = "ENTRY_WINDOW" if in_entry_window else "NO_ENTRY"
    
    # ------------------------------------------------------------------
    # Fetch data
    # ------------------------------------------------------------------
    daily = DataProvider.get_ohlcv(symbol, "3mo", "1d", "nse")
    # NOTE: Angel One returns an empty dataset (no error) for very short 5m
    # periods (1d/2d). Use a longer window so we reliably get 5m candles; the
    # broker also self-heals with period fallbacks if a short period is passed.
    # NOTE: RVOL (in compute_5m_structure) needs >=5 PRIOR trading days at
    # the same time-of-day slot. Two separate things were capping the
    # history actually available to it:
    #   1) period="5d" includes today, leaving at most 4 prior days - fixed
    #      below by requesting "10d".
    #   2) DataProvider.get_ohlcv defaults to limit=100 candles REGARDLESS
    #      of the period requested - 100 five-minute candles is only ~1.3
    #      trading days, so even "10d" of underlying data was being
    #      silently truncated before compute_5m_structure ever saw it.
    #      Fixed by passing an explicit limit (800 comfortably covers 10
    #      trading days at ~75 candles/day with headroom).
    m5 = DataProvider.get_ohlcv(symbol, "10d", "5m", "nse", limit=800)
    m15 = DataProvider.get_ohlcv(symbol, "5d", "15m", "nse")
    
    if (not m15 or "error" in m15 or not m15.get("candles") or len(m15.get("candles", [])) < 15) and m5 and "error" not in m5 and m5.get("candles"):
        resample_res = DataProvider.resample_5m_to_15m(m5)
        if resample_res and resample_res.get("candles"):
            m15 = resample_res
    quote = DataProvider.get_realtime_quote(symbol, "nse")
    
    # Data freshness (P0-2): Angel One quotes are NAIVE IST timestamps.
    # Localize to Asia/Kolkata and compare against current IST so the computed
    # age is correct (previously compared naive-vs-UTC, inflating staleness).
    if quote and "error" not in quote and quote.get("timestamp"):
        try:
            data_ts = pd.Timestamp(quote["timestamp"])
            if data_ts.tzinfo is None:
                data_ts = data_ts.tz_localize("Asia/Kolkata")
            else:
                data_ts = data_ts.tz_convert("Asia/Kolkata")
            # Compare against the SIMULATED now during a backtest so a
            # historical quote isn't judged stale against the real wall clock.
            try:
                from backtest import clock as _bt_clock
                now_ist = (pd.Timestamp(_bt_clock.now_ist()).tz_convert("Asia/Kolkata")
                           if _bt_clock.is_active() else pd.Timestamp.now(tz="Asia/Kolkata"))
            except Exception:
                now_ist = pd.Timestamp.now(tz="Asia/Kolkata")
            data_age = (now_ist - data_ts).total_seconds()
            record["data_timestamp"] = data_ts.isoformat()
            record["data_age_seconds"] = round(data_age, 1)
        except Exception:
            pass
    
    max_age = settings.get("max_data_age_seconds", 300)
    if record["data_age_seconds"] is not None and record["data_age_seconds"] > max_age:
        record["signal"] = "WAIT"
        record["reason"] = f"STALE_DATA (age {record['data_age_seconds']}s)"
        record["failed_conditions"] = ["data_freshness"]
        return record
    
    # ------------------------------------------------------------------
    # CRITICAL DATA QUALITY GATES (per expert recommendation)
    # ------------------------------------------------------------------
    data_quality_issues = []
    
    # Check essential data availability
    if not daily or "error" in daily or not daily.get("candles"):
        data_quality_issues.append("daily_data_missing")
    if not m15 or "error" in m15 or not m15.get("candles"):
        data_quality_issues.append("15m_data_missing")
    if not m5 or "error" in m5 or not m5.get("candles"):
        data_quality_issues.append("5m_data_missing")
    
    # Check if we have minimum candles needed for indicators
    if daily.get("candles") and len(daily["candles"]) < 30:
        data_quality_issues.append("insufficient_daily_candles")
    if m15.get("candles") and len(m15["candles"]) < 15:
        data_quality_issues.append("insufficient_15m_candles")
    if m5.get("candles") and len(m5["candles"]) < 15:
        data_quality_issues.append("insufficient_5m_candles")
    
    if data_quality_issues:
        record["signal"] = "WAIT"
        record["reason"] = f"DATA_INSUFFICIENT: {', '.join(data_quality_issues)}"
        record["failed_conditions"] = data_quality_issues
        return record

    # ------------------------------------------------------------------
    # CORE ANALYSIS (canonical block - computes every value used below)
    # ------------------------------------------------------------------
    daily_candles = daily["candles"]
    # P0-3: use ONLY completed candles for trend/structure/breakout decisions.
    # The currently-forming candle is excluded so we never trigger a final
    # signal on a candle that can still change shape.
    m15_candles = get_completed_candles(m15.get("candles", []), 15)
    m5_candles = get_completed_candles(m5.get("candles", []), 5)

    # Live price (realtime quote, fallback to last 5m close)
    price = None
    if quote and "error" not in quote and quote.get("last_price") is not None:
        price = float(quote["last_price"])
    elif m5_candles:
        price = float(m5_candles[-1]["close"])
    record["price"] = price

    # Trend + structure
    daily_trend = compute_daily_trend(daily_candles, settings)
    m15_trend = compute_15m_trend(m15_candles, settings)
    m5_struct = compute_5m_structure(m5_candles, settings)
    record["daily_trend"] = daily_trend
    record["m15_trend"] = m15_trend
    record["m5_structure"] = m5_struct.get("structure", "NEUTRAL")

    # Indicators
    m5_summary = Indicators.compute_summary(m5_candles)
    rsi = m5_struct.get("rsi")
    rvol = m5_struct.get("rvol")
    body_ratio = m5_struct.get("body_ratio")
    record["rsi"] = rsi
    record["rvol"] = rvol
    record["body_ratio"] = body_ratio

    vwap_obj = Indicators.vwap(m5_candles)
    vwap_vals = vwap_obj.get("values", [])
    vwap = vwap_vals[-1] if vwap_vals else None
    record["vwap"] = vwap

    atr_5m_obj = Indicators.atr(m5_candles, settings.get("atr_period", 14))
    atr_5m_vals = atr_5m_obj.get("values", [])
    atr_5m = atr_5m_vals[-1] if atr_5m_vals else None
    atr_15m_obj = Indicators.atr(m15_candles, settings.get("atr_period", 14))
    atr_15m_vals = atr_15m_obj.get("values", [])
    atr_15m = atr_15m_vals[-1] if atr_15m_vals else None
    record["atr_5m"] = atr_5m
    record["atr_15m"] = atr_15m

    levels = find_levels(m15_candles, settings.get("resistance_lookback", 20))
    support = levels.get("support")
    resistance = levels.get("resistance")
    record["support"] = support
    record["resistance"] = resistance

    # ------------------------------------------------------------------
    # Pattern Analysis (8-Pattern Intraday Expansion)
    # Evaluated BEFORE breakout/breakdown so confirmed patterns can
    # satisfy the trigger gate (spec §9 pipeline fix).
    # ------------------------------------------------------------------
    or_info = compute_opening_range(symbol, settings, m5_candles)
    or_levels = {
        "orh": or_info.get("orh"),
        "orl": or_info.get("orl"),
        "orm": or_info.get("orm"),
    }

    pd_levels = {}
    if len(daily_candles) >= 2:
        prev_d = daily_candles[-2]
        pd_levels = {"pdh": prev_d.get("high"), "pdl": prev_d.get("low"), "pdc": prev_d.get("close")}

    gap_info = {}
    if pd_levels.get("pdc") and m5_candles:
        open_price = m5_candles[0].get("open")
        pdc = pd_levels["pdc"]
        if open_price and pdc:
            gap_info = {
                "gap_percent": (open_price - pdc) / pdc * 100,
                "open_price": open_price,
                "pdc": pdc,
            }

    pattern_report = evaluate_all_patterns(
        symbol=symbol,
        candles_5m=m5_candles,
        candles_15m=m15_candles,
        candles_daily=daily_candles,
        quote=quote,
        indicators={
            "vwap": vwap,
            "rvol": rvol,
            "rsi": rsi,
            "atr_5m": atr_5m,
            "daily_trend": daily_trend,
            "m15_trend": m15_trend,
        },
        levels=levels,
        pd_levels=pd_levels,
        or_levels=or_levels,
        gap_info=gap_info,
        settings=settings,
    )

    record["pattern_report"] = pattern_report
    record["primary_pattern"] = pattern_report.get("primary_pattern")
    record["pattern_agreement_count"] = pattern_report.get("pattern_agreement_count", 0)
    record["agreement_level"] = pattern_report.get("agreement_level", "LOW")

    # ------------------------------------------------------------------
    # CONFIRMED pattern extraction — feeds into breakout/breakdown trigger
    # ------------------------------------------------------------------
    _active = pattern_report.get("active_patterns", [])
    confirmed_buy_patterns = [
        p for p in _active
        if p.get("status") == "CONFIRMED" and p.get("direction") == "BUY"
    ]
    confirmed_sell_patterns = [
        p for p in _active
        if p.get("status") == "CONFIRMED" and p.get("direction") == "SELL"
    ]
    record["confirmed_buy_patterns"] = confirmed_buy_patterns
    record["confirmed_sell_patterns"] = confirmed_sell_patterns

    # ------------------------------------------------------------------
    # Breakout / Breakdown trigger (spec §9 pipeline fix):
    # A trigger is satisfied if EITHER:
    #   (a) A confirmed BUY/SELL pattern exists in pattern_report, OR
    #   (b) The legacy pivot breakout/breakdown fires.
    # This bridges the gap where patterns detected signals but the old
    # single-pivot detect_breakout() did not, blocking the hard gate.
    # ------------------------------------------------------------------
    legacy_breakout = bool(resistance) and detect_breakout(m5_candles, resistance, "BUY", settings)
    legacy_breakdown = bool(support) and detect_breakout(m5_candles, support, "SELL", settings)

    has_pattern_buy = len(confirmed_buy_patterns) > 0
    has_pattern_sell = len(confirmed_sell_patterns) > 0

    breakout = has_pattern_buy or legacy_breakout
    breakdown = has_pattern_sell or legacy_breakdown
    record["breakout"] = breakout
    record["breakdown"] = breakdown
    record["legacy_breakout"] = legacy_breakout
    record["legacy_breakdown"] = legacy_breakdown
    record["pattern_triggered_buy"] = has_pattern_buy
    record["pattern_triggered_sell"] = has_pattern_sell
    record["confirmed"] = breakout or breakdown

    # #14 Distance-to-trigger (review doc #14): for ALMOST/WATCH candidates the
    # engine must show exactly what is missing and how far price is from the
    # level that would confirm the setup. Computed for any candidate that has a
    # level and a live price (used by dashboard + decision log).
    distance_to_trigger = None
    trigger_level = None
    if price and resistance and not breakout:
        trigger_level = resistance
        distance_to_trigger = (resistance - price) / price * 100
    elif price and support and not breakdown:
        trigger_level = support
        distance_to_trigger = (price - support) / price * 100
    record["trigger_level"] = round(trigger_level, 2) if trigger_level else None
    record["distance_to_trigger"] = round(distance_to_trigger, 2) if distance_to_trigger is not None else None

    # ------------------------------------------------------------------
    # Confluence & Entry Decision (spec §11-§15)
    # ------------------------------------------------------------------
    buy_pattern_count = len(confirmed_buy_patterns)
    sell_pattern_count = len(confirmed_sell_patterns)
    has_conflict = buy_pattern_count > 0 and sell_pattern_count > 0

    # Confluence score: independent pattern families
    def _confluence_score(count):
        if count >= 4: return 90
        if count == 3: return 80
        if count == 2: return 65
        if count == 1: return 50
        return 0

    def _confluence_level(count):
        if count >= 4: return "VERY_HIGH"
        if count == 3: return "HIGH"
        if count == 2: return "MODERATE"
        if count == 1: return "LOW"
        return "NONE"

    confluence_buy = _confluence_score(buy_pattern_count)
    confluence_sell = _confluence_score(sell_pattern_count)
    confluence_level_buy = _confluence_level(buy_pattern_count)
    confluence_level_sell = _confluence_level(sell_pattern_count)

    record["confluence_buy"] = confluence_buy
    record["confluence_sell"] = confluence_sell
    record["confluence_level_buy"] = confluence_level_buy
    record["confluence_level_sell"] = confluence_level_sell
    record["pattern_conflict"] = has_conflict


    # Liquidity (P0-4): genuine 20-session DAILY liquidity, EXCLUDING today's
    # partial session. Intraday 5m last-20 bars are NOT a valid daily-liquidity
    # proxy and previously manufactured false PASSes. With <21 clean sessions we
    # return INSUFFICIENT_HISTORY (avg None) and never assume GOOD.
    liq = compute_daily_liquidity(daily["candles"])
    avg_volume = liq.get("avg_volume")
    avg_traded_value = liq.get("avg_traded_value")
    record["avg_volume"] = avg_volume
    record["avg_traded_value"] = avg_traded_value
    if liq.get("status") == "OK":
        liquidity_good = (
            avg_volume is not None and avg_volume >= settings.get("min_avg_volume", 100000)
            and avg_traded_value is not None and avg_traded_value >= settings.get("min_avg_traded_value", 10000000)
        )
        liquidity_status = "GOOD" if liquidity_good else "POOR"
    else:
        liquidity_status = "INSUFFICIENT_HISTORY"
    record["liquidity_status"] = liquidity_status

    # Spread / overextension - DIRECTION-AWARE (bug fix).
    # Previously this only checked price > resistance + max_ext (correct
    # for BUY), and that SAME value was reused for SELL's gate too - so
    # SELL could never fail this check no matter how far price had
    # already fallen below support before the breakdown fired. That's
    # exactly the "entering after the move is already exhausted" failure
    # mode. Now each direction checks its own side of the range.
    overextended_buy = False
    overextended_sell = False
    spread_percent = None
    if price and support and resistance:
        rng = resistance - support
        if rng > 0:
            spread_percent = abs(price - resistance) / rng * 100
            max_ext = settings.get("max_entry_extension_atr", 0.5) * (atr_5m or 0)
            overextended_buy = price > resistance + max_ext
            overextended_sell = price < support - max_ext
    record["overextended_buy"] = overextended_buy
    record["overextended_sell"] = overextended_sell
    record["overextended"] = overextended_buy or overextended_sell  # kept for any existing display/back-compat use
    record["spread_percent"] = spread_percent

    # ------------------------------------------------------------------
    # Reversal-risk check at ENTRY (new) - previously NOTHING in the
    # entry decision checked "does this move already look exhausted /
    # about to snap back". Reversal-aware logic existed elsewhere in the
    # app (position exit monitoring) but was never connected to the
    # entry gate itself. This blocks a FRESH entry in a direction when
    # there's already live reversal evidence against it - it is
    # deliberately conservative (candle pattern OR RSI rollover is
    # enough to block), since letting a bad entry through costs more
    # than skipping a borderline one.
    reversal_risk_buy = False
    reversal_risk_sell = False
    reversal_risk_reasons_buy: List[str] = []
    reversal_risk_reasons_sell: List[str] = []
    if m5_candles and len(m5_candles) >= 4:
        is_rev, rev_pattern = _detect_reversal_candle(m5_candles, "BUY")   # bearish pattern = risk to a fresh BUY
        if is_rev:
            reversal_risk_buy = True
            reversal_risk_reasons_buy.append(f"Bearish reversal candle present ({rev_pattern})")
        is_rev, rev_pattern = _detect_reversal_candle(m5_candles, "SELL")  # bullish pattern = risk to a fresh SELL
        if is_rev:
            reversal_risk_sell = True
            reversal_risk_reasons_sell.append(f"Bullish reversal candle present ({rev_pattern})")

        rsi_vals = [v for v in (Indicators.rsi(m5_candles, settings.get("rsi_period", 14)).get("values") or []) if v is not None]
        if len(rsi_vals) >= 2:
            rsi_now, rsi_prev = rsi_vals[-1], rsi_vals[-2]
            if rsi_now >= 70 and rsi_now < rsi_prev:
                reversal_risk_buy = True
                reversal_risk_reasons_buy.append(f"RSI overbought and turning down ({rsi_prev:.0f} -> {rsi_now:.0f})")
            if rsi_now <= 30 and rsi_now > rsi_prev:
                reversal_risk_sell = True
                reversal_risk_reasons_sell.append(f"RSI oversold and turning up ({rsi_prev:.0f} -> {rsi_now:.0f})")
    record["reversal_risk_buy"] = reversal_risk_buy
    record["reversal_risk_sell"] = reversal_risk_sell
    record["reversal_risk_reasons_buy"] = reversal_risk_reasons_buy
    record["reversal_risk_reasons_sell"] = reversal_risk_reasons_sell

    # ------------------------------------------------------------------
    # EARLY_REVERSAL signal (new) - a SEPARATE, distinctly-labeled,
    # explicitly lower-confidence signal type. Fires closer to an actual
    # turn than the confirmed hard-gate BUY/SELL call, using exhaustion
    # evidence (proximity to level + reversal candle + RSI divergence +
    # volume climax). Does NOT feed into buy_hard_gate/sell_hard_gate and
    # is reported under its own field so it's never confused with a
    # confirmed signal - see analyze_early_reversal()'s docstring for why
    # this is a genuinely different, higher-risk tool, not an upgrade.
    early_reversal = analyze_early_reversal(m5_candles, support, resistance, atr_5m, rvol, settings)
    record["early_reversal_signal"] = early_reversal["signal"]
    record["early_reversal_confidence"] = early_reversal["confidence"]
    record["early_reversal_reasons"] = early_reversal["reasons"]

    # ------------------------------------------------------------------
    # Exhaustion (new) - how much of the move is already used up. Feeds a
    # 'not_exhausted' hard gate and a score cap below, so a stretched move
    # can't show a high BUY score near the top / SELL score near the bottom.
    exhaustion = compute_exhaustion(m5_candles, daily_candles, price, vwap, atr_5m, rsi,
                                    early_reversal["signal"], settings)
    record.update(exhaustion)
    exhaustion_block = settings.get("exhaustion_block_score", 50)
    exhausted_buy = exhaustion["exhaustion_buy"] >= exhaustion_block
    exhausted_sell = exhaustion["exhaustion_sell"] >= exhaustion_block
    record["exhausted_buy"] = exhausted_buy
    record["exhausted_sell"] = exhausted_sell

    # ------------------------------------------------------------------
    # Live index alignment (new). Bank stocks follow Bank Nifty, the rest
    # Nifty. BUY allowed when the index is UP, or FLAT with the stock
    # outperforming it today, or the stock is outperforming by a wide
    # margin (index_rs_override_pct) - genuine relative strength. SELL
    # mirrored. Missing index data fails OPEN (same policy as the regime).
    sector = get_sector(symbol)
    idx = (market_regime.get("banknifty_intraday") if sector == "BANKING"
           else market_regime.get("nifty_intraday")) or {}
    idx_dir = idx.get("direction", "UNKNOWN")
    idx_chg = idx.get("change_from_open_pct")
    stock_chg = None
    try:
        last_day = pd.Timestamp(m5_candles[-1]["timestamp"]).date()
        first_today = next(c for c in m5_candles if pd.Timestamp(c["timestamp"]).date() == last_day)
        if price and first_today["open"]:
            stock_chg = (price - first_today["open"]) / first_today["open"] * 100
    except Exception:
        pass
    rs_today = (stock_chg - idx_chg) if (stock_chg is not None and idx_chg is not None) else None
    rs_override = settings.get("index_rs_override_pct", 1.0)
    if idx_dir == "UNKNOWN":
        index_ok_buy = index_ok_sell = True
    else:
        index_ok_buy = idx_dir == "UP" or (rs_today is not None and (
            (idx_dir == "FLAT" and rs_today > 0) or rs_today >= rs_override))
        index_ok_sell = idx_dir == "DOWN" or (rs_today is not None and (
            (idx_dir == "FLAT" and rs_today < 0) or rs_today <= -rs_override))
    record["index_name"] = "BANKNIFTY" if sector == "BANKING" else "NIFTY"
    record["index_direction"] = idx_dir
    record["index_change_pct"] = idx_chg
    record["stock_change_pct"] = round(stock_chg, 2) if stock_chg is not None else None
    record["rs_vs_index_today"] = round(rs_today, 2) if rs_today is not None else None
    record["index_ok_buy"] = index_ok_buy
    record["index_ok_sell"] = index_ok_sell

    # Daily discipline limits (trades/day, losing streak, daily loss)
    try:
        day_status = positions_module.trading_day_status(settings)
    except Exception as e:
        day_status = {"blocked": False, "reasons": [f"day status unavailable: {e}"]}
    record["trading_day_status"] = day_status

    # Volume signal
    vol_ok_buy = rvol is not None and rvol >= settings.get("min_rvol", 1.5)
    vol_ok_sell = rvol is not None and rvol >= settings.get("min_rvol", 1.5)
    if rvol is not None and rvol >= settings.get("preferred_rvol", 2.0):
        volume_signal = "HIGH"
    elif vol_ok_buy:
        volume_signal = "NORMAL"
    else:
        volume_signal = "LOW"
    record["volume_signal"] = volume_signal

    # Entry / Stop / Targets (Phase 3 Authoritative Setup Integration)
    atr_mult_sl = settings.get("atr_multiplier_sl", 1.0)
    atr_mult_tgts = settings.get("atr_multiplier_targets", [1.0, 1.5, 2.0])
    confirm_pct = settings.get("confirm_candle_breakout_pct", 0.15) / 100
    
    entry_buy = stop_buy = t1_buy = t2_buy = t3_buy = None
    entry_sell = stop_sell = t1_sell = t2_sell = t3_sell = None
    rr_buy = rr_sell = None
    rr_for_gate_buy = rr_for_gate_sell = None
    
    auth = pattern_report.get("authoritative_setup")
    
    # 1. Authoritative Pattern Setup (BUY)
    if auth and auth["direction"] == "BUY":
        entry_buy = auth["entry_price"]
        stop_buy = auth["stop_loss"]
        t1_buy = auth["t1"]
        t2_buy = auth["t2"]
        t3_buy = auth["t3"]
        rr_buy = auth["rr_t1"]
        rr_for_gate_buy = auth["rr_t3"]
    # 2. Fallback Legacy Setup (BUY)
    elif price and resistance and atr_5m:
        entry_buy = resistance * (1 + confirm_pct)
        stop_buy = entry_buy - atr_mult_sl * atr_5m
        t1_buy = entry_buy + atr_mult_tgts[0] * atr_5m
        t2_buy = entry_buy + atr_mult_tgts[1] * atr_5m
        t3_buy = entry_buy + atr_mult_tgts[2] * atr_5m
        if stop_buy and entry_buy > stop_buy:
            rr_buy = (t1_buy - entry_buy) / (entry_buy - stop_buy)
            rr_for_gate_buy = (t3_buy - entry_buy) / (entry_buy - stop_buy)

    # 1. Authoritative Pattern Setup (SELL)
    if auth and auth["direction"] == "SELL":
        entry_sell = auth["entry_price"]
        stop_sell = auth["stop_loss"]
        t1_sell = auth["t1"]
        t2_sell = auth["t2"]
        t3_sell = auth["t3"]
        rr_sell = auth["rr_t1"]
        rr_for_gate_sell = auth["rr_t3"]
    # 2. Fallback Legacy Setup (SELL)
    elif price and support and atr_5m:
        entry_sell = support * (1 - confirm_pct)
        stop_sell = entry_sell + atr_mult_sl * atr_5m
        t1_sell = entry_sell - atr_mult_tgts[0] * atr_5m
        t2_sell = entry_sell - atr_mult_tgts[1] * atr_5m
        t3_sell = entry_sell - atr_mult_tgts[2] * atr_5m
        if stop_sell and stop_sell > entry_sell:
            rr_sell = (entry_sell - t1_sell) / (stop_sell - entry_sell)
            rr_for_gate_sell = (entry_sell - t3_sell) / (stop_sell - entry_sell)

    rr = rr_buy or rr_sell
    record["risk_reward"] = round(rr, 2) if rr else None

    # P0-1: hard-gate RR uses the worst realistic target (T3), not T1.
    rr_ok_buy = rr_for_gate_buy is not None and rr_for_gate_buy >= settings.get("min_rr", 2.0)
    rr_ok_sell = rr_for_gate_sell is not None and rr_for_gate_sell >= settings.get("min_rr", 2.0)

    # ------------------------------------------------------------------
    # Entry-freshness gates (new). entry_buy/entry_sell/rr_buy/rr_sell
    # above answer "was this ever a good setup" - they're computed from
    # either the pattern's authoritative entry price (fixed at whenever
    # the pattern first triggered) or, in the fallback path,
    # resistance*(1+confirm_pct) - a value that NEVER LOOKS AT CURRENT
    # PRICE AT ALL. So a stock that broke out at 100 and has since run to
    # 105 still shows the exact same "great RR" it had at the moment of
    # breakout, because the RR math is anchored to 100, not 105. These
    # two gates answer the actually-important question instead: is
    # entering RIGHT NOW, at the CURRENT price, still a good idea.
    #
    # Gate 1 - entry distance: how far has price drifted from the
    # original entry, measured in R (risk units), where 1R = the
    # original entry-to-stop distance. More than max_entry_drift_r
    # (default 0.30R) means you'd be chasing, not entering fresh.
    #
    # Gate 2 - current-price RR: reward-to-risk measured from THE PRICE
    # YOU'D ACTUALLY PAY RIGHT NOW to the stop and T3, not from the
    # stale original entry. This can and often does differ sharply from
    # rr_for_gate_buy/sell once price has moved.
    max_entry_drift_r = settings.get("max_entry_drift_r", 0.30)

    entry_distance_buy_r = None
    entry_too_far_buy = False
    current_rr_buy = None
    if price and entry_buy and stop_buy and entry_buy > stop_buy:
        risk_unit_buy = entry_buy - stop_buy
        if risk_unit_buy > 0:
            entry_distance_buy_r = round((price - entry_buy) / risk_unit_buy, 2)
            entry_too_far_buy = entry_distance_buy_r > max_entry_drift_r
        if t3_buy is not None and price > stop_buy:
            current_rr_buy = round((t3_buy - price) / (price - stop_buy), 2)

    entry_distance_sell_r = None
    entry_too_far_sell = False
    current_rr_sell = None
    if price and entry_sell and stop_sell and stop_sell > entry_sell:
        risk_unit_sell = stop_sell - entry_sell
        if risk_unit_sell > 0:
            entry_distance_sell_r = round((entry_sell - price) / risk_unit_sell, 2)
            entry_too_far_sell = entry_distance_sell_r > max_entry_drift_r
        if t3_sell is not None and price < stop_sell:
            current_rr_sell = round((price - t3_sell) / (stop_sell - price), 2)

    current_rr_ok_buy = current_rr_buy is not None and current_rr_buy >= settings.get("min_rr", 2.0)
    current_rr_ok_sell = current_rr_sell is not None and current_rr_sell >= settings.get("min_rr", 2.0)

    record["entry_distance_buy_r"] = entry_distance_buy_r
    record["entry_distance_sell_r"] = entry_distance_sell_r
    record["entry_too_far_buy"] = entry_too_far_buy
    record["entry_too_far_sell"] = entry_too_far_sell
    record["current_rr_buy"] = current_rr_buy
    record["current_rr_sell"] = current_rr_sell

    # #12 Structural RR (review doc #12) — feature-flagged, OFF by default.
    # Instead of ATR-fixed stops/targets, use the most recent structural swing
    # low (BUY) / high (SELL) as the stop and the next structural swing level as
    # the target. RR gate then uses the structural distance. Disabled by default
    # to preserve the verified green suite.
    record["structural_rr_applied"] = False
    if settings.get("use_structural_rr", False):
        try:
            struct = _structural_swing_levels(m15_candles, settings)
            s_swing_low = struct["swing_low"]
            s_swing_high = struct["swing_high"]
            if price and resistance and atr_5m and s_swing_low:
                s_entry = resistance * (1 + confirm_pct)
                s_stop = s_swing_low
                if s_stop < s_entry:
                    s_t1 = s_entry + (s_entry - s_stop)
                    s_t3 = s_entry + 2 * (s_entry - s_stop)
                    rr_buy = (s_t1 - s_entry) / (s_entry - s_stop)
                    rr_for_gate_buy = (s_t3 - s_entry) / (s_entry - s_stop)
                    entry_buy, stop_buy, t1_buy, t2_buy, t3_buy = s_entry, s_stop, s_t1, s_t1, s_t3
            if price and support and atr_5m and s_swing_high:
                s_entry = support * (1 - confirm_pct)
                s_stop = s_swing_high
                if s_stop > s_entry:
                    s_t1 = s_entry - (s_stop - s_entry)
                    s_t3 = s_entry - 2 * (s_stop - s_entry)
                    rr_sell = (s_entry - s_t1) / (s_stop - s_entry)
                    rr_for_gate_sell = (s_entry - s_t3) / (s_stop - s_entry)
                    entry_sell, stop_sell, t1_sell, t2_sell, t3_sell = s_entry, s_stop, s_t1, s_t1, s_t3
            rr = rr_buy or rr_sell
            record["risk_reward"] = round(rr, 2) if rr else None
            rr_ok_buy = rr_for_gate_buy is not None and rr_for_gate_buy >= settings.get("min_rr", 2.0)
            rr_ok_sell = rr_for_gate_sell is not None and rr_for_gate_sell >= settings.get("min_rr", 2.0)
            record["structural_rr_applied"] = True
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("structural_rr eval failed: %s", e)


    # Market regime alignment
    raw_regime = market_regime.get("regime", "UNKNOWN")
    if raw_regime == "UNKNOWN":
        # FAIL OPEN, not closed. An UNKNOWN regime means the Nifty
        # benchmark fetch itself failed or returned too little data -
        # that's a DATA problem, not a genuine signal that the market is
        # in some special state that should block every stock's signal,
        # in both directions, for the whole scan cycle. Treat it as
        # RANGE - the neutral value already present in both the BUY and
        # SELL allow-lists below - so one bad benchmark fetch can't
        # silently zero out an entire session. The raw value is still
        # recorded (market_regime_raw / market_regime_failed_open) so
        # this is visible in decision_log, not hidden.
        regime = "RANGE"
    else:
        regime = raw_regime
    record["market_regime_raw"] = raw_regime
    record["market_regime_failed_open"] = (raw_regime == "UNKNOWN")

    use_regime_filter = settings.get("use_market_regime_filter", True)
    if use_regime_filter:
        if regime == "TRENDING_UP":
            market_alignment_ok = daily_trend in ("BULLISH", "NEUTRAL")
        elif regime == "TRENDING_DOWN":
            market_alignment_ok = daily_trend in ("BEARISH", "NEUTRAL")
        else:
            market_alignment_ok = True
    else:
        market_alignment_ok = True

    # Hard-gate condition dictionaries
    buy_conditions = {
        "liquidity": "PASS" if liquidity_status == "GOOD" else "FAIL",
        "session": "PASS" if record.get("session_status") == "ENTRY_WINDOW" else "FAIL",
        "market_alignment": "PASS" if market_alignment_ok else "FAIL",
        "daily_trend": "PASS" if daily_trend == "BULLISH" else "FAIL",
        "15m_trend": "PASS" if m15_trend == "BULLISH" else "FAIL",
        "5m_structure": "PASS" if m5_struct.get("structure") in ("BULLISH", "BULLISH_WEAK") else "FAIL",
        "market_regime": "PASS" if regime in ("TRENDING_UP", "RANGE", "HIGH_VOLATILITY") else "FAIL",
        "vwap": "PASS" if (vwap is not None and price is not None and price > vwap) else "FAIL",
        "volume": "PASS" if vol_ok_buy else "FAIL",
        "breakout": "PASS" if breakout else "FAIL",
        "overextension": "PASS" if not overextended_buy else "FAIL",
        "no_active_reversal": "PASS" if not reversal_risk_buy else "FAIL",
        "not_exhausted": "PASS" if not exhausted_buy else "FAIL",
        "index_alignment": "PASS" if index_ok_buy else "FAIL",
        "entry_freshness": "PASS" if not entry_too_far_buy else "FAIL",
        "current_rr": "PASS" if current_rr_ok_buy else "FAIL",
        "atr": "PASS" if atr_5m is not None else "FAIL",
        "rr": "PASS" if rr_ok_buy else "FAIL",
        "candle_quality": "PASS" if (body_ratio is not None and body_ratio >= settings.get("min_body_ratio", 0.55)) else "FAIL",
    }
    sell_conditions = {
        "liquidity": "PASS" if liquidity_status == "GOOD" else "FAIL",
        "session": "PASS" if record.get("session_status") == "ENTRY_WINDOW" else "FAIL",
        "market_alignment": "PASS" if market_alignment_ok else "FAIL",
        "daily_trend": "PASS" if daily_trend == "BEARISH" else "FAIL",
        "15m_trend": "PASS" if m15_trend == "BEARISH" else "FAIL",
        "5m_structure": "PASS" if m5_struct.get("structure") in ("BEARISH", "BEARISH_WEAK") else "FAIL",
        "market_regime": "PASS" if regime in ("TRENDING_DOWN", "RANGE", "HIGH_VOLATILITY") else "FAIL",
        "vwap": "PASS" if (vwap is not None and price is not None and price < vwap) else "FAIL",
        "volume": "PASS" if vol_ok_sell else "FAIL",
        "breakdown": "PASS" if breakdown else "FAIL",
        "overextension": "PASS" if not overextended_sell else "FAIL",
        "no_active_reversal": "PASS" if not reversal_risk_sell else "FAIL",
        "not_exhausted": "PASS" if not exhausted_sell else "FAIL",
        "index_alignment": "PASS" if index_ok_sell else "FAIL",
        "entry_freshness": "PASS" if not entry_too_far_sell else "FAIL",
        "current_rr": "PASS" if current_rr_ok_sell else "FAIL",
        "atr": "PASS" if atr_5m is not None else "FAIL",
        "rr": "PASS" if rr_ok_sell else "FAIL",
        "candle_quality": "PASS" if (body_ratio is not None and body_ratio >= settings.get("min_body_ratio", 0.55)) else "FAIL",
    }

    # #11 Relative-strength exception (review doc #11) — feature-flagged, OFF.
    # When enabled, a FAIL on the market_regime gate is "PASS-but-flagged" if
    # EVERY other hard-gate condition already passes AND the stock's sector is
    # STRONG and the stock is outperforming its benchmark (relative strength).
    # This lets a genuinely strong stock trade against a weak index. Disabled by
    # default to preserve the verified green suite and conservative posture.
    record["rs_exception_applied"] = False
    if settings.get("allow_rs_exception", False):
        for side, conds in (("BUY", buy_conditions), ("SELL", sell_conditions)):
            if conds.get("market_regime") != "FAIL":
                continue
            # Every OTHER condition must already PASS for the exception to fire.
            other_fail = [k for k, v in conds.items() if k != "market_regime" and v != "PASS"]
            if other_fail:
                continue
            try:
                sector = compute_sector_strength(symbol, settings)
                rs = compute_relative_strength(symbol, settings)
                sector_strong = sector.get("sector_strength") == "STRONG_SECTOR"
                stock_outperform = rs.get("relative_strength") in (
                    "STRONG_RELATIVE_STRENGTH", "NEUTRAL"
                ) and (rs.get("rs_vs_nifty") or 0) > 0
                if sector_strong and stock_outperform:
                    conds["market_regime"] = "PASS"
                    record["rs_exception_applied"] = True
                    record.setdefault("condition_detail", {})["market_regime"] = \
                        "RS exception: strong sector + stock outperforming"
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning("rs_exception eval failed: %s", e)

    buy_trend_ok = daily_trend == "BULLISH" and m15_trend == "BULLISH"
    sell_trend_ok = daily_trend == "BEARISH" and m15_trend == "BEARISH"

    record["factor_status"] = {
        "liquidity": liquidity_status,
        "session": record.get("session_status"),
        "market_alignment": "OK" if market_alignment_ok else "FAIL",
        "daily_trend": daily_trend,
        "15m_trend": m15_trend,
        "5m_structure": m5_struct.get("structure"),
        "market_regime": regime,
        "vwap": "above" if (vwap and price and price > vwap) else ("below" if (vwap and price) else "n/a"),
        "volume": volume_signal,
        "breakout": breakout,
        "breakdown": breakdown,
        "atr": atr_5m,
    }

    # ------------------------------------------------------------------
    # Gate model (Task 6): "strict" (legacy, default) vs "tiered".
    #
    #   strict  - ALL conditions below are mandatory (ANDed). This is the
    #             original behavior; a single FAIL => no signal. It is why
    #             IMMEDIATE calls were extremely rare.
    #   tiered  - only MUST-HAVE conditions block a signal. The remaining
    #             conditions become "boosters": a failed booster does NOT
    #             block the trade, it subtracts `booster_penalty` points from
    #             the score (applied later, in the score section). Quality is
    #             still enforced via the score threshold, so a setup failing
    #             several boosters can't reach IMMEDIATE - but one that only
    #             trips a booster or two can.
    #
    # Controlled by settings["gate_mode"] (default "strict" so nothing changes
    # until explicitly enabled). The must-have set is config-overridable via
    # settings["hard_gate_must_haves"] so Task 8 can sweep where the line falls.
    # The trigger key (breakout/breakdown) is ALWAYS a must-have regardless of
    # mode - there is no trade without a trigger.
    # ------------------------------------------------------------------
    ALL_BUY_KEYS = [
        "liquidity", "session", "market_alignment", "daily_trend", "15m_trend",
        "5m_structure", "market_regime", "vwap", "volume",
        "breakout", "overextension", "no_active_reversal", "not_exhausted",
        "index_alignment", "entry_freshness", "current_rr", "atr", "rr", "candle_quality",
    ]
    ALL_SELL_KEYS = [
        "liquidity", "session", "market_alignment", "daily_trend", "15m_trend",
        "5m_structure", "market_regime", "vwap", "volume",
        "breakdown", "overextension", "no_active_reversal", "not_exhausted",
        "index_alignment", "entry_freshness", "current_rr", "atr", "rr", "candle_quality",
    ]

    gate_mode = settings.get("gate_mode", "strict")

    # Default tiered must-have set: the non-negotiables for a sane intraday
    # entry. Everything else becomes a booster. 'breakout'/'breakdown' are added
    # per-direction below and are always required.
    default_must_haves = [
        "liquidity", "session", "atr", "daily_trend", "15m_trend",
        "vwap", "current_rr", "rr",
    ]
    must_haves = settings.get("hard_gate_must_haves", default_must_haves)

    if gate_mode == "tiered":
        buy_must = list(dict.fromkeys(list(must_haves) + ["breakout"]))
        sell_must = list(dict.fromkeys(list(must_haves) + ["breakdown"]))
        # Only keep keys that actually exist in the condition dicts.
        buy_hard_gate_keys = [k for k in buy_must if k in buy_conditions]
        sell_hard_gate_keys = [k for k in sell_must if k in sell_conditions]
        # Boosters = every ALL_* key that is NOT a must-have.
        buy_booster_keys = [k for k in ALL_BUY_KEYS if k not in buy_hard_gate_keys]
        sell_booster_keys = [k for k in ALL_SELL_KEYS if k not in sell_hard_gate_keys]
    else:
        buy_hard_gate_keys = ALL_BUY_KEYS
        sell_hard_gate_keys = ALL_SELL_KEYS
        buy_booster_keys = []
        sell_booster_keys = []

    record["gate_mode"] = gate_mode
    record["buy_booster_keys"] = buy_booster_keys
    record["sell_booster_keys"] = sell_booster_keys

    def hard_gate_passes(conditions, keys):
        """A hard gate passes ONLY if every mandatory (must-have) condition
        is PASS. In tiered mode `keys` is the small must-have set; in strict
        mode it is the full list."""
        for k in keys:
            if conditions.get(k) != "PASS":
                return False
        return True

    # Additional gate: setup must exist (breakout/breakdown) - included in keys.
    buy_hard_gate = hard_gate_passes(buy_conditions, buy_hard_gate_keys)
    sell_hard_gate = hard_gate_passes(sell_conditions, sell_hard_gate_keys)

    # Failed BOOSTERS (tiered mode): conditions that failed but did NOT block.
    # They feed a score penalty in the scoring section below.
    buy_failed_boosters = [k for k in buy_booster_keys if buy_conditions.get(k) not in ("PASS", None)]
    sell_failed_boosters = [k for k in sell_booster_keys if sell_conditions.get(k) not in ("PASS", None)]
    record["buy_failed_boosters"] = buy_failed_boosters
    record["sell_failed_boosters"] = sell_failed_boosters

    record["buy_conditions"] = buy_conditions
    record["sell_conditions"] = sell_conditions
    # failed_conditions = failed MUST-HAVES only (what actually blocks); this
    # keeps the dashboard's blocker display focused on true blockers in tiered
    # mode while remaining the full failed list in strict mode.
    record["buy_failed_conditions"] = [k for k in buy_hard_gate_keys if buy_conditions.get(k) != "PASS"]
    record["sell_failed_conditions"] = [k for k in sell_hard_gate_keys if sell_conditions.get(k) != "PASS"]
    
    # ------------------------------------------------------------------
    # SCORES (secondary - never override gates)
    # ------------------------------------------------------------------
    buy_score = 0
    sell_score = 0
    score_breakdown = {}
    score_summary = []
    satisfied = []
    missing = []
    
    # Weights from spec §27
    W = {
        "market_alignment": 10, "daily_trend": 10, "m15_trend": 15,
        "m5_momentum": 10, "vwap": 10, "structure": 10, "breakout": 10,
        "volume": 10, "rsi": 5, "candle_quality": 5, "sector": 5, "rr": 5,
    }
    
    def add_score(label, pts, reason):
        nonlocal buy_score, sell_score
        buy_score += pts
        sell_score += pts
        score_breakdown[label] = pts
        score_summary.append(f"{label}: {pts:+d}")
    
    # Market alignment
    if regime == "TRENDING_UP":
        add_score("Market Alignment", W["market_alignment"], "TRENDING_UP")
        satisfied.append("market_alignment")
    elif regime == "RANGE":
        add_score("Market Alignment", 5, "Range")
    elif regime == "TRENDING_DOWN":
        buy_score -= W["market_alignment"] // 2
        score_summary.append("Market Alignment: -5")
        missing.append("market_alignment")
    
    # Daily trend
    if daily_trend == "BULLISH":
        add_score("Daily Trend", W["daily_trend"], "bullish")
        satisfied.append("daily_trend")
    elif daily_trend == "BEARISH":
        sell_score += W["daily_trend"]
        score_summary.append("Daily Trend (SELL): +10")
        buy_score -= W["daily_trend"] // 2
        missing.append("daily_trend")
    
    # 15m trend
    if m15_trend == "BULLISH":
        buy_score += W["m15_trend"]
        score_summary.append(f"15m Trend BULLISH: +{W['m15_trend']}")
        satisfied.append("m15_trend")
    elif m15_trend == "BEARISH":
        sell_score += W["m15_trend"]
        score_summary.append(f"15m Trend BEARISH (SELL): +{W['m15_trend']}")
        missing.append("m15_trend")
    
    # 5m momentum
    if m5_struct["structure"] in ("BULLISH", "BULLISH_WEAK"):
        buy_score += W["m5_momentum"]
        score_summary.append(f"5m Structure {m5_struct['structure']}: +{W['m5_momentum']}")
        satisfied.append("m5_structure")
    elif m5_struct["structure"] in ("BEARISH", "BEARISH_WEAK"):
        sell_score += W["m5_momentum"]
        score_summary.append(f"5m Structure {m5_struct['structure']} (SELL): +{W['m5_momentum']}")
    
    # VWAP
    if vwap and price:
        if price > vwap:
            buy_score += W["vwap"]
            score_summary.append(f"Price > VWAP: +{W['vwap']}")
            satisfied.append("vwap")
        elif price < vwap:
            sell_score += W["vwap"]
            score_summary.append(f"Price < VWAP (SELL): +{W['vwap']}")
    
    # Structure (support/resistance proximity) - only once the level has
    # actually broken. Before the break, sitting near support is where a
    # bounce is most likely, so rewarding SELL there (and BUY near
    # resistance) pushed the score up at exactly the wrong spot.
    if breakout and resistance and abs(price - resistance) / price * 100 < 0.5:
        buy_score += W["structure"]
        score_summary.append(f"Near Resistance {resistance}: +{W['structure']}")
    if breakdown and support and abs(price - support) / price * 100 < 0.5:
        sell_score += W["structure"]
        score_summary.append(f"Near Support {support} (SELL): +{W['structure']}")
    
    # Breakout/Breakdown
    if breakout:
        buy_score += W["breakout"]
        score_summary.append(f"Breakout above {resistance}: +{W['breakout']}")
        satisfied.append("breakout")
    if breakdown:
        sell_score += W["breakout"]
        score_summary.append(f"Breakdown below {support} (SELL): +{W['breakout']}")
    
    # Volume
    if rvol is not None and rvol >= settings.get("preferred_rvol", 2.0):
        add_score("Volume", W["volume"], f"RVOL {rvol:.2f} >= 2.0")
        satisfied.append("volume")
    elif rvol is not None and rvol >= settings.get("min_rvol", 1.5):
        buy_score += W["volume"] // 2
        sell_score += W["volume"] // 2
        score_summary.append(f"Volume RVOL {rvol:.2f}: +{W['volume']//2}")
        satisfied.append("volume")
    
    # RSI - only the healthy momentum band earns points; overbought (>70)
    # / oversold (<30) is late in the move, not confirmation of it.
    if rsi is not None:
        if settings.get("rsi_bull_min", 50) <= rsi < 70:
            buy_score += W["rsi"]
            score_summary.append(f"RSI {rsi:.1f} in 50-70: +{W['rsi']}")
            satisfied.append("rsi")
        elif 30 < rsi <= settings.get("rsi_bear_max", 50):
            sell_score += W["rsi"]
            score_summary.append(f"RSI {rsi:.1f} in 30-50 (SELL): +{W['rsi']}")
    
    # Candle quality
    if body_ratio and body_ratio >= settings.get("min_body_ratio", 0.55):
        add_score("Candle Quality", W["candle_quality"], f"body {body_ratio:.2f}")
        satisfied.append("candle_quality")
    
    # RR - uses CURRENT-PRICE RR (current_rr_buy/sell), not the stale
    # entry-anchored rr_buy/rr_sell. rr_buy/sell answer "was this ever a
    # good setup" (fixed at the original breakout/pattern entry); a stock
    # that broke out at 100 and has since run to 105 would still show its
    # original great RR there even though entering NOW at 105 is a much
    # worse trade. Scoring off the stale number would make a high score
    # misleadingly trustworthy for exactly the fast-path ("score >= 80,
    # enter directly") workflow this most needs to be honest for.
    if current_rr_buy and current_rr_buy >= settings.get("preferred_rr", 2.5):
        buy_score += W["rr"]
        score_summary.append(f"Current RR {current_rr_buy:.1f}: +{W['rr']}")
    if current_rr_sell and current_rr_sell >= settings.get("preferred_rr", 2.5):
        sell_score += W["rr"]
        score_summary.append(f"Current RR {current_rr_sell:.1f} (SELL): +{W['rr']}")

    # ------------------------------------------------------------------
    # Booster penalty (Task 6, tiered mode only). In tiered mode a condition
    # that was demoted from a hard gate to a booster no longer BLOCKS the
    # trade; instead each failed booster subtracts `booster_penalty` points.
    # This keeps quality enforced through the score threshold (a setup that
    # trips many boosters can't reach IMMEDIATE) while letting a setup that
    # only trips one or two still qualify - the whole point of a+b: more
    # genuine calls without forcing low-quality trades. No effect in strict
    # mode (buy_failed_boosters/sell_failed_boosters are empty there).
    booster_penalty = settings.get("booster_penalty", 7)
    if buy_failed_boosters:
        pen = booster_penalty * len(buy_failed_boosters)
        buy_score -= pen
        score_summary.append(f"Boosters failed ({', '.join(buy_failed_boosters)}): -{pen}")
    if sell_failed_boosters:
        pen = booster_penalty * len(sell_failed_boosters)
        sell_score -= pen
        score_summary.append(f"Boosters failed ({', '.join(sell_failed_boosters)}): -{pen}")

    record["buy_score"] = max(0, min(100, buy_score))
    record["sell_score"] = max(0, min(100, sell_score))
    record["score_breakdown"] = score_breakdown
    record["score_summary"] = score_summary
    record["satisfied"] = list(set(satisfied))
    record["missing"] = list(set(missing))

    # ------------------------------------------------------------------
    # Entry-timing verdict + score cap (new).
    #
    # WHY A CAP AND NOT JUST A POINT DEDUCTION: trend/momentum/breakout/
    # VWAP components alone total 85+ points in the weight table above,
    # and by design these stay positive even when a move is already
    # exhausted - that's precisely the "hard gates are positive near the
    # top/bottom" issue. A flat -10 or -15 point penalty could still
    # leave a badly-stretched setup at 75-85, i.e. still over a "score
    # >= 80, enter directly" threshold. A hard CAP guarantees it can't.
    #
    # This intentionally also protects a workflow that does NOT wait for
    # buy_hard_gate/sell_hard_gate to fully pass and instead acts on the
    # score number alone - the cap makes that number itself trustworthy
    # for that use, not just the hard gate.
    ENTRY_TIMING_SCORE_CAP = settings.get("entry_timing_score_cap", 45)

    buy_timing_bad = entry_too_far_buy or (current_rr_buy is not None and not current_rr_ok_buy)
    sell_timing_bad = entry_too_far_sell or (current_rr_sell is not None and not current_rr_ok_sell)

    if buy_timing_bad or exhausted_buy:
        record["buy_score"] = min(record["buy_score"], ENTRY_TIMING_SCORE_CAP)
    if sell_timing_bad or exhausted_sell:
        record["sell_score"] = min(record["sell_score"], ENTRY_TIMING_SCORE_CAP)

    # The single, clearly-labeled verdict - check THIS, not just the raw
    # score, before entering on a score alone without waiting for the
    # full hard gate.
    entry_timing_verdict = None
    entry_timing_reasons = []
    active_direction = "BUY" if buy_score >= sell_score else "SELL"
    too_far = entry_too_far_buy if active_direction == "BUY" else entry_too_far_sell
    dist_r = entry_distance_buy_r if active_direction == "BUY" else entry_distance_sell_r
    cur_rr = current_rr_buy if active_direction == "BUY" else current_rr_sell
    cur_rr_ok = current_rr_ok_buy if active_direction == "BUY" else current_rr_ok_sell

    if too_far and (cur_rr is not None and not cur_rr_ok):
        entry_timing_verdict = "ALREADY_EXTENDED"
        entry_timing_reasons.append(f"Price has drifted {dist_r}R from the original entry (max allowed {max_entry_drift_r}R)")
        entry_timing_reasons.append(f"Current RR is only {cur_rr} (need {settings.get('min_rr', 2.0)}) - reward left is too small for the risk from here")
    elif too_far:
        entry_timing_verdict = "ALREADY_EXTENDED"
        entry_timing_reasons.append(f"Price has drifted {dist_r}R from the original entry (max allowed {max_entry_drift_r}R) - this is chasing, not a fresh entry")
    elif cur_rr is not None and not cur_rr_ok:
        entry_timing_verdict = "POOR_CURRENT_RR"
        entry_timing_reasons.append(f"Current RR is only {cur_rr} (need {settings.get('min_rr', 2.0)}) - entering now no longer offers enough reward for the risk")
    elif dist_r is not None:
        entry_timing_verdict = "GOOD_ENTRY"
        entry_timing_reasons.append(f"Price is only {dist_r}R from the original entry and current RR ({cur_rr}) still clears the minimum")

    # Exhaustion overrides a "good"/unknown verdict - the move may be late
    # even when price is still close to the original entry.
    exhausted_active = exhausted_buy if active_direction == "BUY" else exhausted_sell
    if exhausted_active and entry_timing_verdict in (None, "GOOD_ENTRY"):
        entry_timing_verdict = "EXHAUSTED"
        entry_timing_reasons.extend(
            exhaustion["exhaustion_reasons_buy"] if active_direction == "BUY"
            else exhaustion["exhaustion_reasons_sell"]
        )

    record["entry_timing_verdict"] = entry_timing_verdict
    record["entry_timing_reasons"] = entry_timing_reasons

    # ------------------------------------------------------------------
    # Structure-obstacle check (new). Targets are pure ATR multiples with
    # no awareness of nearby structure - T1 might sit just beyond a real
    # resistance (BUY) or support (SELL) level that has a real chance of
    # stalling the move before it gets there. Reuses 'levels' (already
    # computed above) rather than re-scanning candles. Checks any level
    # FURTHER than the immediate breakout level itself (that one was
    # already crossed to trigger the signal - it's not a future obstacle).
    obstacle_before_target = False
    obstacle_level = None
    if active_direction == "BUY" and t1_buy is not None and price is not None:
        further = [r for r in (levels.get("near_resistance") or []) if resistance is None or r > resistance + 1e-9]
        for lvl in further:
            if price < lvl <= t1_buy:
                obstacle_before_target = True
                obstacle_level = lvl
                break
    elif active_direction == "SELL" and t1_sell is not None and price is not None:
        further = [s for s in (levels.get("near_support") or []) if support is None or s < support - 1e-9]
        for lvl in further:
            if t1_sell <= lvl < price:
                obstacle_before_target = True
                obstacle_level = lvl
                break

    record["obstacle_before_target"] = obstacle_before_target
    record["obstacle_level"] = obstacle_level
    if obstacle_before_target:
        entry_timing_reasons.append(
            f"A further {'resistance' if active_direction == 'BUY' else 'support'} level at "
            f"{obstacle_level} sits before T1 - the move may stall there before reaching target"
        )
        # Downgrade rather than hard-block, matching the score-cap approach
        # above - this is a real warning, not proof the trade is invalid.
        if entry_timing_verdict == "GOOD_ENTRY":
            entry_timing_verdict = "OBSTACLE_BEFORE_TARGET"
        record["entry_timing_verdict"] = entry_timing_verdict
        record["entry_timing_reasons"] = entry_timing_reasons
        if active_direction == "BUY":
            record["buy_score"] = min(record["buy_score"], ENTRY_TIMING_SCORE_CAP)
        else:
            record["sell_score"] = min(record["sell_score"], ENTRY_TIMING_SCORE_CAP)

    # ------------------------------------------------------------------
    # Manual entry zone (new) - single GOOD/EARLY/WAIT/LATE/DONT_CHASE/
    # NO_TRADE label per direction for entries taken without waiting for
    # IMMEDIATE. Uses the FINAL (capped) scores.
    zone_buy = compute_entry_zone(
        "BUY", record["buy_score"], price, resistance, breakout, atr_5m, vwap,
        exhausted_buy, exhaustion["exhaustion_buy"], early_reversal["signal"],
        current_rr_buy, entry_too_far_buy,
        obstacle_before_target and active_direction == "BUY", m5_candles, now_t,
        index_ok_buy, record["index_name"], idx_dir, day_status, settings)
    zone_sell = compute_entry_zone(
        "SELL", record["sell_score"], price, support, breakdown, atr_5m, vwap,
        exhausted_sell, exhaustion["exhaustion_sell"], early_reversal["signal"],
        current_rr_sell, entry_too_far_sell,
        obstacle_before_target and active_direction == "SELL", m5_candles, now_t,
        index_ok_sell, record["index_name"], idx_dir, day_status, settings)
    record["entry_zone_buy"] = zone_buy
    record["entry_zone_sell"] = zone_sell
    # Headline = the better zone; ties go to the higher final score
    if (ENTRY_ZONE_RANK[zone_buy["zone"]], record["buy_score"]) >= \
            (ENTRY_ZONE_RANK[zone_sell["zone"]], record["sell_score"]):
        record["entry_zone"], record["entry_zone_direction"] = zone_buy, "BUY"
    else:
        record["entry_zone"], record["entry_zone_direction"] = zone_sell, "SELL"

    record["buy_hard_gate"] = buy_hard_gate
    record["sell_hard_gate"] = sell_hard_gate
    
    # ------------------------------------------------------------------
    # Entry / SL / Targets (set from active setup)
    # ------------------------------------------------------------------
    if buy_hard_gate:
        ctx = STATE_STORE.get(symbol)
        record["entry"] = round(entry_buy, 2) if entry_buy else ctx.get("entry_price")
        record["stop_loss"] = round(stop_buy, 2) if stop_buy else None
        record["target_1"] = round(t1_buy, 2) if t1_buy else None
        record["target_2"] = round(t2_buy, 2) if t2_buy else None
        record["target_3"] = round(t3_buy, 2) if t3_buy else None
    elif sell_hard_gate:
        record["entry"] = round(entry_sell, 2) if entry_sell else None
        record["stop_loss"] = round(stop_sell, 2) if stop_sell else None
        record["target_1"] = round(t1_sell, 2) if t1_sell else None
        record["target_2"] = round(t2_sell, 2) if t2_sell else None
        record["target_3"] = round(t3_sell, 2) if t3_sell else None
    
    # ------------------------------------------------------------------
    # Final signal determination (§30 decision priority)
    # ------------------------------------------------------------------
    # Check existing setup state first (state machine)
    prev_state = STATE_STORE.get(symbol)
    prev_signal = prev_state.get("signal_state", "WAIT")
    
    if buy_hard_gate and sell_hard_gate:
        # Both gates TRUE -> CONFLICT, require additional confirmation
        record["signal"] = "CONFLICT"
        record["reason"] = "Both BUY and SELL hard gates passed - need confirmation"
        return record
    
    # Score threshold enforcement (per expert recommendation)
    buy_score_threshold = settings.get("buy_score_threshold", 65)
    sell_score_threshold = settings.get("sell_score_threshold", 65)
    
    if buy_hard_gate:
        # Enforce score >= threshold for IMMEDIATE signals (expert requirement)
        if record["buy_score"] < buy_score_threshold:
            # Hard gate passed but score too low -> downgrade to ALMOST
            record["signal"] = "ALMOST_BUY"
            record["reason"] = f"Buy hard gate passed but score {record['buy_score']} < {buy_score_threshold} threshold"
            STATE_STORE.update(symbol, {
                "setup_id": None,
                "signal_state": "ALMOST_BUY",
                "status": "SETUP"
            })
            return record
        
        # P0-3: HIGH_VOLATILITY -> no IMMEDIATE (expert requirement)
        if regime == "HIGH_VOLATILITY":
            record["signal"] = "ALMOST_BUY"
            record["reason"] = "HIGH_VOLATILITY market - IMMEDIATE blocked, setup ready for breakout"
            STATE_STORE.update(symbol, {
                "setup_id": None,
                "signal_state": "ALMOST_BUY",
                "status": "SETUP"
            })
            return record

        # FIX 2: Require live quote for IMMEDIATE signals (data must be fresh)
        if record.get("data_age_seconds") is None:
            record["signal"] = "ALMOST_BUY"
            record["reason"] = "No live quote timestamp - downgrade to ALMOST (need live data)"
            STATE_STORE.update(symbol, {
                "setup_id": None,
                "signal_state": "ALMOST_BUY",
                "status": "SETUP"
            })
            return record
        
        max_age = settings.get("max_data_age_seconds", 300)
        if record["data_age_seconds"] > max_age:
            record["signal"] = "ALMOST_BUY"
            record["reason"] = f"Stale data (age {record['data_age_seconds']:.0f}s > {max_age}s) - downgrade to ALMOST"
            STATE_STORE.update(symbol, {
                "setup_id": None,
                "signal_state": "ALMOST_BUY",
                "status": "SETUP"
            })
            return record

        # Create new setup_id
        candle_ts = m5_candles[-1]["timestamp"] if m5_candles else now.isoformat()
        setup_id = generate_setup_id(symbol, candle_ts, "BUY")
        record["setup_id"] = setup_id
        record["candle_timestamp"] = candle_ts

        if prev_signal in ("IMMEDIATE_BUY", "IN_TRADE") and prev_state.get("setup_id") == setup_id:
            # Duplicate signal - don't re-trigger
            record["signal"] = "IN_TRADE"
            record["reason"] = "Setup already active (duplicate suppressed)"
        else:
            record["signal"] = "IMMEDIATE_BUY"
            record["reason"] = f"Buy hard gate passed + score {record['buy_score']} >= {buy_score_threshold}"
        
        STATE_STORE.update(symbol, {
            "setup_id": setup_id,
            "signal_state": record["signal"],
            "candle_timestamp": candle_ts,
            "entry_price": record["entry"],
            "status": "ACTIVE",
        })
    elif sell_hard_gate:
        # Enforce score >= threshold for IMMEDIATE signals (expert requirement)
        if record["sell_score"] < sell_score_threshold:
            # Hard gate passed but score too low -> downgrade to ALMOST
            record["signal"] = "ALMOST_SELL"
            record["reason"] = f"Sell hard gate passed but score {record['sell_score']} < {sell_score_threshold} threshold"
            STATE_STORE.update(symbol, {
                "setup_id": None,
                "signal_state": "ALMOST_SELL",
                "status": "SETUP"
            })
            return record
        
        candle_ts = m5_candles[-1]["timestamp"] if m5_candles else now.isoformat()
        setup_id = generate_setup_id(symbol, candle_ts, "SELL")
        record["setup_id"] = setup_id
        record["candle_timestamp"] = candle_ts
        
        if prev_signal in ("IMMEDIATE_SELL", "IN_TRADE") and prev_state.get("setup_id") == setup_id:
            record["signal"] = "IN_TRADE"
            record["reason"] = "Setup already active (duplicate suppressed)"
        else:
            record["signal"] = "IMMEDIATE_SELL"
            record["reason"] = f"Sell hard gate passed + score {record['sell_score']} >= {sell_score_threshold}"
        
        STATE_STORE.update(symbol, {
            "setup_id": setup_id,
            "signal_state": record["signal"],
            "candle_timestamp": candle_ts,
            "entry_price": record["entry"],
            "status": "ACTIVE",
        })
    else:
        # P1-13 + Fix 4: Tighten ALMOST criteria 
        # - Major conditions (liquidity, session, trend, regime, ATR, candle)
        # - RVOL >= 1.5
        # - RR >= 2.0
        # - VWAP confirmation (price on correct side) - FIX 4
        rvol = record.get("rvol", 0) or 0
        rr = record.get("risk_reward", 0) or 0
        rvol_ok = rvol >= settings.get("min_rvol", 1.5)
        rr_ok = rr >= settings.get("min_rr", 2.0)
        vwap_ok_buy = vwap is not None and price > vwap
        vwap_ok_sell = vwap is not None and price < vwap
        
        # Major conditions still required. For the WATCH/ALMOST tier the
        # "session" gate is relaxed to the WATCH window (>= watch_start, e.g.
        # 09:15) so early monitoring is possible; IMMEDIATE still requires the
        # strict ENTRY_WINDOW via the hard-gate block above (review doc #9).
        in_entry_window = record.get("session_status") == "ENTRY_WINDOW"
        watch_window_open = now_t >= watch_start
        session_ok_for_tier = in_entry_window or watch_window_open

        def _major(conds):
            for k in ["liquidity", "market_alignment", "daily_trend",
                      "15m_trend", "5m_structure", "market_regime", "atr",
                      "candle_quality"]:
                if conds.get(k) != "PASS":
                    return False
            # session: strict for IMMEDIATE (handled by hard gate), relaxed to
            # watch window for the WATCH/ALMOST tier.
            return session_ok_for_tier or conds.get("session") == "PASS"

        buy_major = _major(buy_conditions)
        sell_major = _major(sell_conditions)
        
        # P1-13 + Fix 4: Only ALMOST if has major + volume + RR + VWAP
        buy_almost_ready = buy_major and rvol_ok and rr_ok and vwap_ok_buy
        sell_almost_ready = sell_major and rvol_ok and rr_ok and vwap_ok_sell
        
        # Check setup invalidation
        if prev_signal in ("ALMOST_BUY", "IMMEDIATE_BUY", "IN_TRADE") and not buy_major:
            record["signal"] = "CANCEL"
            record["reason"] = "Buy setup invalidated"
            STATE_STORE.update(symbol, {"setup_id": None, "signal_state": "CANCEL", "status": "CANCELLED"})
            return record
        if prev_signal in ("ALMOST_SELL", "IMMEDIATE_SELL", "IN_TRADE") and not sell_major:
            record["signal"] = "CANCEL"
            record["reason"] = "Sell setup invalidated"
            STATE_STORE.update(symbol, {"setup_id": None, "signal_state": "CANCEL", "status": "CANCELLED"})
            return record
        
        # ------------------------------------------------------------------
        # GAP 4: WATCH / ALMOST / WAIT tier logic (per expert + Phase 2)
        # ------------------------------------------------------------------
        # WATCH (score >= 65): Very strong setup, high probability after breakout
        # ALMOST (score 55-64): Decent setup, moderate probability
        # WAIT (score < 55): Weak setup, skip it
        # 
        # All tiers require major conditions to pass
        # Missing only breakout/breakdown confirmation
        
        almost_min_score = 55  # Minimum score for ALMOST
        watch_min_score = 65   # Minimum score for WATCH (Phase 2 enhancement)
        
        # P1-13: Only signal ALMOST if all criteria met (major + volume + RR + score)
        if buy_almost_ready and not record["breakout"]:
            if record["buy_score"] >= watch_min_score:
                record["signal"] = "BUY_WATCH"
                record["reason"] = f"Strong setup (score {record['buy_score']}, RVOL {rvol:.1f}, RR {rr:.1f}) - ready for breakout"
            elif record["buy_score"] >= almost_min_score:
                record["signal"] = "ALMOST_BUY"
                record["reason"] = f"Near-ready (score {record['buy_score']}, RVOL {rvol:.1f}>={settings.get('min_rvol',1.5)}, RR {rr:.1f}>={settings.get('min_rr',2.0)}) - waiting for breakout"
            else:
                record["signal"] = "WAIT"
                record["reason"] = f"Major conditions pass but score {record['buy_score']} < {almost_min_score}"
        elif sell_almost_ready and not record["breakdown"]:
            if record["sell_score"] >= watch_min_score:
                record["signal"] = "SELL_WATCH"
                record["reason"] = f"Strong setup (score {record['sell_score']}, RVOL {rvol:.1f}, RR {rr:.1f}) - ready for breakdown"
            elif record["sell_score"] >= almost_min_score:
                record["signal"] = "ALMOST_SELL"
                record["reason"] = f"Near-ready (score {record['sell_score']}, RVOL {rvol:.1f}>={settings.get('min_rvol',1.5)}, RR {rr:.1f}>={settings.get('min_rr',2.0)}) - waiting for breakdown"
            else:
                record["signal"] = "WAIT"
                record["reason"] = f"Major conditions pass but score {record['sell_score']} < {almost_min_score}"
        elif buy_major or sell_major:
            # Major conditions pass but RVOL or RR insufficient
            record["signal"] = "WAIT"
            missing_parts = []
            if not rvol_ok:
                missing_parts.append(f"RVOL {rvol:.1f}<{settings.get('min_rvol',1.5)}")
            if not rr_ok:
                missing_parts.append(f"RR {rr:.1f}<{settings.get('min_rr',2.0)}")
            record["reason"] = f"WAIT - {', '.join(missing_parts)} (score ok but need volume+RR confirmation)"
        else:
            # WAIT (legitimate result)
            record["signal"] = "WAIT"
            fail_reasons = []
            if not buy_trend_ok and not sell_trend_ok:
                fail_reasons.append("trend conflict")
            if not vol_ok_buy and not vol_ok_sell:
                fail_reasons.append("weak volume")
            if record["liquidity_status"] != "GOOD":
                fail_reasons.append("poor liquidity")
            if rr_ok_buy is False and rr_ok_sell is False:
                fail_reasons.append("bad RR")
            record["reason"] = "WAIT - " + ", ".join(fail_reasons) if fail_reasons else "WAIT - no valid setup"
        
        STATE_STORE.update(symbol, {
            "setup_id": prev_state.get("setup_id"),
            "signal_state": record["signal"],
            "status": "SETUP"
        })
    
    record["failed_conditions"] = list(set(
        record["buy_failed_conditions"] + record["sell_failed_conditions"]
    ))

    # #16 Primary rejection reason (review doc #16): explain WAIT/ALMOST/WATCH
    # rows with the highest-priority FAIL plus ordered secondary blockers.
    primary_blocker, secondary_blockers = find_primary_blocker(
        record, record.get("buy_conditions", {}), record.get("sell_conditions", {}),
        record.get("buy_score", 0), record.get("sell_score", 0), settings,
    )
    record["primary_blocker"] = primary_blocker
    record["secondary_blockers"] = secondary_blockers
    if primary_blocker:
        record["rejection_summary"] = "Primary blocker: " + primary_blocker + \
            (" | Secondary: " + ", ".join(secondary_blockers) if secondary_blockers else "")
    else:
        record["rejection_summary"] = None

    # ------------------------------------------------------------------
    # Entry Decision Layer (spec §11-§15, §19-§22)
    # Computes a structured entry_decision dict summarising:
    #   - confluence, conflict, blocker detail, and a human label.
    # This NEVER overrides the signal/hard-gate — it is a diagnostic
    # overlay that makes the funnel observable.
    # ------------------------------------------------------------------
    signal = record.get("signal", "WAIT")
    _ed_buy_cnt = len(record.get("confirmed_buy_patterns", []))
    _ed_sell_cnt = len(record.get("confirmed_sell_patterns", []))
    _ed_conflict = record.get("pattern_conflict", False)

    def _compute_entry_decision():
        """Derive entry_decision dict from signal + pattern + gate state."""
        # Start building the decision
        ed = {
            "confirmed_buy_count": _ed_buy_cnt,
            "confirmed_sell_count": _ed_sell_cnt,
            "confluence_buy": record.get("confluence_buy", 0),
            "confluence_sell": record.get("confluence_sell", 0),
            "confluence_level_buy": record.get("confluence_level_buy", "NONE"),
            "confluence_level_sell": record.get("confluence_level_sell", "NONE"),
            "pattern_conflict": _ed_conflict,
            "primary_blocker": primary_blocker,
            "secondary_blockers": secondary_blockers,
            "blocker_detail": None,
            "state": "WAIT",
            "direction": None,
        }

        # Conflict override
        if _ed_conflict:
            ed["state"] = "CONFLICT"
            ed["blocker_detail"] = (
                f"BUY ({_ed_buy_cnt} confirmed) + SELL ({_ed_sell_cnt} confirmed) "
                "patterns contradict — no new entry"
            )
            return ed

        # IMMEDIATE signals
        if signal == "IMMEDIATE_BUY":
            ed["state"] = "READY"
            ed["direction"] = "BUY"
            return ed
        if signal == "IMMEDIATE_SELL":
            ed["state"] = "READY"
            ed["direction"] = "SELL"
            return ed

        # Strong confluence but not yet IMMEDIATE
        if signal in ("BUY_WATCH", "ALMOST_BUY"):
            ed["direction"] = "BUY"
            if _ed_buy_cnt >= 2:
                ed["state"] = "STRONG_CANDIDATE"
            elif _ed_buy_cnt == 1:
                ed["state"] = "CANDIDATE"
            else:
                ed["state"] = "WATCH"
        elif signal in ("SELL_WATCH", "ALMOST_SELL"):
            ed["direction"] = "SELL"
            if _ed_sell_cnt >= 2:
                ed["state"] = "STRONG_CANDIDATE"
            elif _ed_sell_cnt == 1:
                ed["state"] = "CANDIDATE"
            else:
                ed["state"] = "WATCH"
        elif signal == "CONFLICT":
            ed["state"] = "CONFLICT"
            ed["blocker_detail"] = "Both BUY and SELL hard gates passed simultaneously"
            return ed
        else:
            # WAIT / CANCEL / IN_TRADE / ERROR
            ed["state"] = "NO_ENTRY" if primary_blocker else "WAIT"

        # Build human-readable blocker detail from primary_blocker
        if primary_blocker:
            _detail_map = {
                "data_quality": "Insufficient data (missing candles / indicators)",
                "liquidity": f"Liquidity POOR (avg_vol={record.get('avg_volume')}, "
                             f"traded_val={record.get('avg_traded_value')})",
                "session": f"Outside entry window "
                           f"{settings.get('entry_start_time', '09:45')}-"
                           f"{settings.get('entry_end_time', '14:45')}",
                "daily_trend": f"Daily trend {record.get('factor_status', {}).get('daily_trend', '?')} "
                               f"(need {'BULLISH' if ed['direction'] == 'BUY' else 'BEARISH'})",
                "15m_trend": f"15m trend {record.get('factor_status', {}).get('15m_trend', '?')} "
                             f"(need {'BULLISH' if ed['direction'] == 'BUY' else 'BEARISH'})",
                "5m_structure": f"5m structure {record.get('factor_status', {}).get('5m_structure', '?')}",
                "market_regime": f"Market regime {record.get('factor_status', {}).get('market_regime', '?')}",
                "vwap": f"Price {'below' if ed.get('direction') == 'BUY' else 'above'} VWAP "
                        f"(VWAP={record.get('vwap')})",
                "volume": f"RVOL {record.get('rvol', 0) or 0:.2f} < "
                          f"{settings.get('min_rvol', 1.5)}",
                "atr": "ATR data unavailable",
                "rr": f"RR {record.get('risk_reward', 0) or 0:.2f} < "
                      f"{settings.get('min_rr', 2.0)}",
                "breakout": "No confirmed 5m breakout or pattern trigger",
                "breakdown": "No confirmed 5m breakdown or pattern trigger",
                "overextension": f"Price overextended beyond "
                                 f"{settings.get('max_entry_extension_atr', 0.5)} ATR",
                "candle_quality": f"Candle body ratio "
                                  f"{record.get('body_ratio', 0) or 0:.2f} < "
                                  f"{settings.get('min_body_ratio', 0.55)}",
                "score": f"Score {max(record.get('buy_score', 0), record.get('sell_score', 0))} "
                         f"< threshold",
            }
            ed["blocker_detail"] = _detail_map.get(
                primary_blocker,
                f"{primary_blocker} gate failed"
            )

        return ed

    entry_decision = _compute_entry_decision()
    record["entry_decision"] = entry_decision

    # #18 Decision log (review doc #18): persist every evaluation for audit.
    # Guarded by settings["decision_log"]; never raises into evaluation.
    if settings.get("decision_log", True):
        try:
            decision_log_module._default.enabled = True
            decision_log_module._default.log(record)
        except Exception as e:  # pragma: no cover - defensive
            import logging
            logging.getLogger(__name__).warning("decision_log failed: %s", e)

    if STATE_STORE and hasattr(STATE_STORE, "clear_old"):
        STATE_STORE.clear_old()
    
    return record


def detect_breakdown(candles: List[dict], level: float, direction: str, settings: Dict[str, Any]) -> bool:
    """Detect breakdown (close below support for SELL)."""
    if not candles or level is None:
        return False
    
    last = candles[-1]  # This MUST be a closed 5m candle
    tolerance = settings.get("breakout_tolerance_percent", 0.05) / 100
    
    # Close below support (wick below is NOT a breakdown)
    return last["close"] < level * (1 - tolerance) and last["close"] < last["open"]


# ---------------------------------------------------------------------------
# Scanner / universe analysis
# ---------------------------------------------------------------------------

def scan_universe(settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Analyze the entire configured stock universe.
    Returns a dict with results + rejection funnel stats (Phase 2 enhancement).
    """
    import json
    with open("intraday_config.json") as f:
        config = json.load(f)
    stocks = config.get("stocks", [])
    
    # Detect market regime ONCE for the whole scan
    market_regime = detect_market_regime(settings)
    
    # ------------------------------------------------------------------
    # REJECTION FUNNEL (Phase 2 enhancement)
    # ------------------------------------------------------------------
    # Track how many stocks pass each gate for diagnostic visibility
    rejection_stats = {
        "total_scanned": 0,
        "data_quality_pass": 0,
        "liquidity_pass": 0,
        "session_pass": 0,
        "trend_alignment_pass": 0,
        "vwap_pass": 0,
        "volume_pass": 0,
        "breakout_pass": 0,
        "rr_pass": 0,
        "score_65plus_pass": 0,
        "immediate_signals": 0,
        "watch_signals": 0,
        "almost_signals": 0,
        # Pattern-level counts (spec §24-§25)
        "pattern_triggered": 0,
        "confirmed_buy_patterns": 0,
        "confirmed_sell_patterns": 0,
        "entry_candidates": 0,
        "strong_candidates": 0,
        "conflicts": 0,
        "pattern_counts": {
            "ORB": 0, "PDH_PDL": 0, "BREAKOUT_RETEST": 0,
            "VWAP_PULLBACK": 0, "EMA20_PULLBACK": 0,
            "INSIDE_BAR": 0, "GAP_AND_GO": 0, "FAILED_ORB": 0,
        },
        "blocker_counts": {},
    }
    
    results = []
    for stock in stocks:
        rejection_stats["total_scanned"] += 1
        
        try:
            record = evaluate_stock(stock, settings, market_regime)
            results.append(record)
            
            # Track gate passage for funnel
            if record.get("signal") not in ("WAIT",) or "data" not in record.get("failed_conditions", []):
                rejection_stats["data_quality_pass"] += 1
            
            buy_cond = record.get("buy_conditions", {})
            sell_cond = record.get("sell_conditions", {})
            
            if buy_cond.get("liquidity") == "PASS" or sell_cond.get("liquidity") == "PASS":
                rejection_stats["liquidity_pass"] += 1
            
            if buy_cond.get("session") == "PASS" or sell_cond.get("session") == "PASS":
                rejection_stats["session_pass"] += 1
            
            # Trend alignment = all trend gates pass
            trend_ok = all(buy_cond.get(k) == "PASS" for k in ["daily_trend", "15m_trend", "5m_structure"])
            trend_ok = trend_ok or all(sell_cond.get(k) == "PASS" for k in ["daily_trend", "15m_trend", "5m_structure"])
            if trend_ok:
                rejection_stats["trend_alignment_pass"] += 1
            
            if buy_cond.get("vwap") == "PASS" or sell_cond.get("vwap") == "PASS":
                rejection_stats["vwap_pass"] += 1
            
            if buy_cond.get("volume") == "PASS" or sell_cond.get("volume") == "PASS":
                rejection_stats["volume_pass"] += 1
            
            if buy_cond.get("breakout") == "PASS" or sell_cond.get("breakdown") == "PASS":
                rejection_stats["breakout_pass"] += 1
            
            if record.get("risk_reward") and record["risk_reward"] >= 2.0:
                rejection_stats["rr_pass"] += 1
            
            max_score = max(record.get("buy_score", 0), record.get("sell_score", 0))
            if max_score >= 65:
                rejection_stats["score_65plus_pass"] += 1
            
            signal = record.get("signal", "WAIT")
            if signal in ("IMMEDIATE_BUY", "IMMEDIATE_SELL"):
                rejection_stats["immediate_signals"] += 1
            elif signal in ("BUY_WATCH", "SELL_WATCH"):
                rejection_stats["watch_signals"] += 1
            elif signal in ("ALMOST_BUY", "ALMOST_SELL"):
                rejection_stats["almost_signals"] += 1

            # Pattern-level diagnostic counts (spec §24-§25)
            _cbp = record.get("confirmed_buy_patterns", [])
            _csp = record.get("confirmed_sell_patterns", [])
            if _cbp or _csp:
                rejection_stats["pattern_triggered"] += 1
            rejection_stats["confirmed_buy_patterns"] += len(_cbp)
            rejection_stats["confirmed_sell_patterns"] += len(_csp)

            # Count by pattern family
            _PMAP = {
                "ORB_BREAKOUT": "ORB", "ORB_BREAKDOWN": "ORB",
                "PDH_BREAKOUT": "PDH_PDL", "PDL_BREAKDOWN": "PDH_PDL",
                "BREAKOUT_RETEST": "BREAKOUT_RETEST",
                "VWAP_PULLBACK": "VWAP_PULLBACK",
                "EMA20_PULLBACK": "EMA20_PULLBACK",
                "INSIDE_BAR_BREAKOUT": "INSIDE_BAR", "INSIDE_BAR_BREAKDOWN": "INSIDE_BAR",
                "GAP_AND_GO": "GAP_AND_GO",
                "FAILED_ORB_REVERSAL": "FAILED_ORB",
            }
            for p in record.get("pattern_report", {}).get("active_patterns", []):
                pn = p.get("pattern_name", "")
                family = _PMAP.get(pn)
                if family and family in rejection_stats["pattern_counts"]:
                    rejection_stats["pattern_counts"][family] += 1

            # Entry decision state counts
            ed = record.get("entry_decision", {})
            ed_state = ed.get("state", "WAIT")
            if ed_state in ("CANDIDATE", "STRONG_CANDIDATE", "READY"):
                rejection_stats["entry_candidates"] += 1
            if ed_state == "STRONG_CANDIDATE":
                rejection_stats["strong_candidates"] += 1
            if ed_state == "CONFLICT":
                rejection_stats["conflicts"] += 1

            # Blocker aggregation
            pb = record.get("primary_blocker")
            if pb:
                rejection_stats["blocker_counts"][pb] = \
                    rejection_stats["blocker_counts"].get(pb, 0) + 1
                
        except Exception as e:
            results.append({
                "symbol": stock.get("symbol", "?"),
                "name": stock.get("name", "?"),
                "signal": "ERROR",
                "reason": str(e),
                "buy_score": 0,
                "sell_score": 0,
                "buy_hard_gate": False,
                "sell_hard_gate": False,
                "price": None,
            })
    
    # Sort per §40 ranking:
    # 1. hard-gate validity
    # 2. BUY/SELL score
    # 3. risk/reward
    # 4. volume confirmation
    # 5. liquidity
    # 6. trend alignment
    def rank_key(r):
        gate_bonus = 1000 if r.get("buy_hard_gate") or r.get("sell_hard_gate") else 0
        score = max(r.get("buy_score", 0), r.get("sell_score", 0))
        rr = r.get("risk_reward") or 0
        vol = r.get("rvol") or 0
        liq = 50 if r.get("liquidity_status") == "GOOD" else 0
        return (gate_bonus + score * 10 + rr * 5 + vol * 2 + liq)
    
    results.sort(key=rank_key, reverse=True)
    
    # Calculate funnel efficiency
    total = rejection_stats["total_scanned"]
    funnel_efficiency = (rejection_stats["immediate_signals"] / total * 100) if total > 0 else 0
    rejection_stats["funnel_efficiency_percent"] = round(funnel_efficiency, 2)
    
    # Requirement (a): best-available candidate so the screen is never blank
    # when nothing clears the gates. Context-only, suppressed when IMMEDIATE
    # signals exist.
    best_available = compute_best_available(results, settings)

    return {
        "results": results,
        "rejection_funnel": rejection_stats,
        "market_regime": market_regime,
        "scan_timestamp": dt.datetime.now().isoformat(),
        "best_available": best_available,
    }


def compute_best_available(results: List[Dict[str, Any]], settings: Dict[str, Any]) -> Dict[str, Any]:
    """Requirement (a): never leave the user with a blank screen.

    When the scan produced NO actionable IMMEDIATE_BUY / IMMEDIATE_SELL signal,
    pick the single highest-ranked BUY-leaning and SELL-leaning candidate so the
    user always has something to look at and decide on themselves. These are
    explicitly labeled BEST_AVAILABLE_* (NOT IMMEDIATE) and are context-only -
    disjoint from the live hard-gate signals, exactly like the pre-market WATCH
    states. They carry the specific blocker(s) keeping them below full-signal
    quality so the user understands why it's not a real signal.

    Returns {"active": bool, "buy": <record-or-None>, "sell": <record-or-None>}.
    `active` is True only when there were no IMMEDIATE signals (i.e. this is
    actually filling an otherwise-empty screen). When IMMEDIATE signals exist,
    best-available is suppressed (active=False) because the real signals are the
    thing to act on.
    """
    tradeable = [r for r in results if r.get("signal") not in ("ERROR", None)]
    has_immediate = any(r.get("signal") in ("IMMEDIATE_BUY", "IMMEDIATE_SELL") for r in tradeable)

    def _blocker_text(r, direction):
        pb = r.get("primary_blocker")
        failed = r.get(f"{direction.lower()}_failed_conditions") or []
        if pb:
            extra = f" (+{len(failed) - 1} more)" if len(failed) > 1 else ""
            return f"{pb}{extra}"
        if failed:
            return f"{failed[0]}" + (f" (+{len(failed) - 1} more)" if len(failed) > 1 else "")
        return "below score threshold"

    def _annotate(r, direction):
        if r is None:
            return None
        out = dict(r)
        out["best_available_direction"] = direction
        out["best_available_label"] = "Below full-signal quality — context only, NOT a trade signal"
        out["best_available_blocker"] = _blocker_text(r, direction)
        out["best_available_score"] = r.get(f"{direction.lower()}_score", 0)
        return out

    # Rank BUY-leaning by buy_score, SELL-leaning by sell_score. A record can
    # lean both ways; we consider it for whichever side scores higher.
    buy_pool = [r for r in tradeable if (r.get("buy_score") or 0) >= (r.get("sell_score") or 0)]
    sell_pool = [r for r in tradeable if (r.get("sell_score") or 0) > (r.get("buy_score") or 0)]
    buy_pool.sort(key=lambda r: (r.get("buy_score") or 0, r.get("risk_reward") or 0), reverse=True)
    sell_pool.sort(key=lambda r: (r.get("sell_score") or 0, r.get("risk_reward") or 0), reverse=True)

    best_buy = _annotate(buy_pool[0], "BUY") if buy_pool else None
    best_sell = _annotate(sell_pool[0], "SELL") if sell_pool else None

    return {
        "active": (not has_immediate),
        "buy": best_buy,
        "sell": best_sell,
    }


def top_opportunities(results: List[Dict[str, Any]], top_n: int = 10) -> Dict[str, Any]:
    """Extract TOP_BUY_CANDIDATES and TOP_SELL_CANDIDATES."""
    # Include WATCH signals in discovery so strong near-ready setups reach the candidate pool (P2: discovery broadening / WATCH semantics).
    buys = [r for r in results if r.get("buy_hard_gate") or r.get("signal") in ("IMMEDIATE_BUY", "ALMOST_BUY", "BUY_WATCH")]
    sells = [r for r in results if r.get("sell_hard_gate") or r.get("signal") in ("IMMEDIATE_SELL", "ALMOST_SELL", "SELL_WATCH")]
    
    buys.sort(key=lambda r: (r.get("buy_hard_gate", False), r.get("buy_score", 0), r.get("risk_reward") or 0), reverse=True)
    sells.sort(key=lambda r: (r.get("sell_hard_gate", False), r.get("sell_score", 0), r.get("risk_reward") or 0), reverse=True)
    
    return {
        "top_buy_candidates": buys[:top_n],
        "top_sell_candidates": sells[:top_n],
    }