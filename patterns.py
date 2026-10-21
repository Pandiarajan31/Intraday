"""
Intraday Entry Pattern Engine.
Supports 8 high-priority intraday pattern families while maintaining strict
risk controls, zero-repaint closed-candle confirmation, and state persistence.
"""

import threading
import datetime as dt
from typing import Dict, List, Any, Optional
import pandas as pd


STATUS_WATCHING = "WATCHING"
STATUS_APPROACHING = "APPROACHING"
STATUS_TRIGGERED = "TRIGGERED"
STATUS_CONFIRMED = "CONFIRMED"
STATUS_REJECTED = "REJECTED"
STATUS_INVALIDATED = "INVALIDATED"
STATUS_CONFLICT = "CONFLICT"

PATTERN_FAMILIES = {
    "ORB": "BREAKOUT",
    "PDH_PDL": "LEVEL",
    "BREAKOUT_RETEST": "PULLBACK",
    "VWAP_PULLBACK": "PULLBACK",
    "EMA20_PULLBACK": "PULLBACK",
    "FAILED_ORB": "REVERSAL",
    "GAP_AND_GO": "MOMENTUM",
    "INSIDE_BAR": "BREAKOUT",
}


class PatternStateStore:
    """Thread-safe state persistence for stateful patterns."""

    def __init__(self):
        self._lock = threading.Lock()
        self._store: Dict[str, Dict[str, Any]] = {}

    def get_state(self, symbol: str, pattern_id: str) -> Dict[str, Any]:
        with self._lock:
            key = f"{symbol.upper()}:{pattern_id.upper()}"
            return dict(self._store.get(key, {}))

    def update_state(self, symbol: str, pattern_id: str, updates: Dict[str, Any]):
        with self._lock:
            key = f"{symbol.upper()}:{pattern_id.upper()}"
            current = self._store.setdefault(key, {})
            current.update(updates)
            current["updated_at"] = dt.datetime.now().isoformat()

    def clear(self, symbol: Optional[str] = None):
        with self._lock:
            if symbol:
                keys = [k for k in self._store if k.startswith(f"{symbol.upper()}:")]
                for k in keys:
                    del self._store[k]
            else:
                self._store.clear()


PATTERN_STATE_STORE = PatternStateStore()


def _build_pattern_result(
    pattern_id: str,
    pattern_name: str,
    direction: str,
    status: str,
    confidence: int,
    trigger_price: Optional[float],
    trigger_level: Optional[float],
    current_price: Optional[float],
    conditions: Dict[str, str],
    reasons: List[str],
    missing_conditions: List[str],
    invalidation: str,
    entry_ready: bool = False,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Construct a standard pattern result dictionary."""
    dist_pct = None
    if current_price and trigger_price and current_price > 0:
        dist_pct = round(abs(trigger_price - current_price) / current_price * 100, 2)

    res = {
        "pattern_id": pattern_id,
        "pattern_name": pattern_name,
        "direction": direction,
        "status": status,
        "confidence": confidence,
        "trigger_price": round(trigger_price, 2) if trigger_price else None,
        "trigger_level": round(trigger_level, 2) if trigger_level else None,
        "current_price": round(current_price, 2) if current_price else None,
        "distance_to_trigger_percent": dist_pct,
        "conditions": conditions or {},
        "reasons": reasons or [],
        "missing_conditions": missing_conditions or [],
        "invalidation": invalidation,
        "entry_ready": entry_ready,
    }
    return res


def _pattern_entry_setup(pattern: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Construct an authoritative entry setup from a confirmed pattern.
    """
    direction = pattern["direction"]
    trigger_price = pattern["trigger_price"]
    trigger_level = pattern["trigger_level"]
    confidence = pattern["confidence"]

    # Typical stop distance based on pattern context (fallbacks)
    # Use 0.5% or trigger distance as a baseline if not provided
    stop_dist = abs(trigger_price - trigger_level) if trigger_level else (trigger_price * 0.005)
    
    # Minimal stop loss floor (e.g. 0.2%) to avoid tiny SL
    min_stop = trigger_price * 0.002
    stop_dist = max(stop_dist, min_stop)

    if direction == "BUY":
        entry_price = trigger_price
        stop_loss = entry_price - stop_dist
        risk = entry_price - stop_loss
        t1 = entry_price + (risk * 1.5)
        t2 = entry_price + (risk * 2.5)
        t3 = entry_price + (risk * 4.0)
        invalidation = stop_loss
    else:
        entry_price = trigger_price
        stop_loss = entry_price + stop_dist
        risk = stop_loss - entry_price
        t1 = entry_price - (risk * 1.5)
        t2 = entry_price - (risk * 2.5)
        t3 = entry_price - (risk * 4.0)
        invalidation = stop_loss

    rr_t1 = round((t1 - entry_price) / risk, 2) if risk > 0 else 0
    rr_t3 = round((t3 - entry_price) / risk, 2) if risk > 0 else 0

    return {
        "pattern_name": pattern["pattern_name"],
        "pattern_id": pattern["pattern_id"],
        "family": PATTERN_FAMILIES.get(pattern["pattern_id"], "UNKNOWN"),
        "direction": direction,
        "trigger_price": round(trigger_price, 2),
        "entry_price": round(entry_price, 2),
        "stop_loss": round(stop_loss, 2),
        "t1": round(t1, 2),
        "t2": round(t2, 2),
        "t3": round(t3, 2),
        "rr_t1": abs(rr_t1),
        "rr_t3": abs(rr_t3),
        "invalidation_level": round(invalidation, 2),
        "confidence": confidence,
    }


def _select_pattern_setup(evaluated_patterns: List[Dict[str, Any]], settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Select the best confirmed pattern and return its setup."""
    confirmed = [p for p in evaluated_patterns if p["status"] == STATUS_CONFIRMED]
    if not confirmed:
        return None

    # Sort by confidence, then by pattern priority if needed
    confirmed.sort(key=lambda x: x["confidence"], reverse=True)
    best_pattern = confirmed[0]
    return _pattern_entry_setup(best_pattern, settings)

def eval_orb(
    symbol: str,
    candles_5m: List[dict],
    price: Optional[float],
    or_levels: Dict[str, Any],
    common_ctx: Dict[str, Any],
    settings: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Pattern #1: Opening Range Breakout / Breakdown (ORB)."""
    orh = or_levels.get("orh")
    orl = or_levels.get("orl")
    if not candles_5m or not price or not (orh or orl):
        return None

    tol = settings.get("orb_tolerance_percent", 0.05) / 100
    last_closed = candles_5m[-1]
    is_closed_bullish = last_closed["close"] > orh * (1 + tol) if orh else False
    is_closed_bearish = last_closed["close"] < orl * (1 - tol) if orl else False

    if orh and (price >= orh * 0.995 or is_closed_bullish):
        conds = {
            "trend": common_ctx.get("buy_trend", "FAIL"),
            "vwap": "PASS" if common_ctx.get("above_vwap") else "FAIL",
            "rvol": "PASS" if common_ctx.get("rvol_ok") else "FAIL",
            "atr": "PASS" if common_ctx.get("atr_ok") else "FAIL",
            "level_break": "PASS" if is_closed_bullish else "WAITING",
        }
        reasons, missing = [], []
        if is_closed_bullish:
            reasons.append(f"Completed 5m candle closed above ORH ({last_closed['close']:.2f} > {orh:.2f})")
        else:
            missing.append("Waiting for completed 5m candle close above ORH")

        if common_ctx.get("above_vwap"):
            reasons.append("Price above VWAP")
        else:
            missing.append("Price below VWAP")

        status = STATUS_CONFIRMED if (is_closed_bullish and not missing) else (STATUS_APPROACHING if price >= orh * 0.998 else STATUS_WATCHING)
        confidence = 85 if status == STATUS_CONFIRMED else (65 if status == STATUS_APPROACHING else 50)

        return _build_pattern_result(
            pattern_id="ORB",
            pattern_name="Opening Range Breakout",
            direction="BUY",
            status=status,
            confidence=confidence,
            trigger_price=orh * 1.001,
            trigger_level=orh,
            current_price=price,
            conditions=conds,
            reasons=reasons,
            missing_conditions=missing,
            invalidation=f"Price falls back below ORH ({orh:.2f})",
            entry_ready=(status == STATUS_CONFIRMED),
        )

    if orl and (price <= orl * 1.005 or is_closed_bearish):
        conds = {
            "trend": common_ctx.get("sell_trend", "FAIL"),
            "vwap": "PASS" if not common_ctx.get("above_vwap") else "FAIL",
            "rvol": "PASS" if common_ctx.get("rvol_ok") else "FAIL",
            "atr": "PASS" if common_ctx.get("atr_ok") else "FAIL",
            "level_break": "PASS" if is_closed_bearish else "WAITING",
        }
        reasons, missing = [], []
        if is_closed_bearish:
            reasons.append(f"Completed 5m candle closed below ORL ({last_closed['close']:.2f} < {orl:.2f})")
        else:
            missing.append("Waiting for completed 5m candle close below ORL")

        status = STATUS_CONFIRMED if (is_closed_bearish and not missing) else (STATUS_APPROACHING if price <= orl * 1.002 else STATUS_WATCHING)
        confidence = 85 if status == STATUS_CONFIRMED else (65 if status == STATUS_APPROACHING else 50)

        return _build_pattern_result(
            pattern_id="ORB",
            pattern_name="Opening Range Breakdown",
            direction="SELL",
            status=status,
            confidence=confidence,
            trigger_price=orl * 0.999,
            trigger_level=orl,
            current_price=price,
            conditions=conds,
            reasons=reasons,
            missing_conditions=missing,
            invalidation=f"Price reclaims ORL ({orl:.2f})",
            entry_ready=(status == STATUS_CONFIRMED),
        )

    return None



def eval_pdh_pdl(
    symbol: str,
    candles_5m: List[dict],
    price: Optional[float],
    pd_levels: Dict[str, Any],
    common_ctx: Dict[str, Any],
    settings: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Pattern #2: Previous Day High / Previous Day Low Breakout."""
    pdh = pd_levels.get("pdh")
    pdl = pd_levels.get("pdl")
    if not candles_5m or not price or not (pdh or pdl):
        return None

    tol = settings.get("breakout_tolerance_percent", 0.05) / 100
    last_closed = candles_5m[-1]
    is_closed_bullish = last_closed["close"] > pdh * (1 + tol) if pdh else False
    is_closed_bearish = last_closed["close"] < pdl * (1 - tol) if pdl else False

    if pdh and (price >= pdh * 0.995 or is_closed_bullish):
        conds = {
            "trend": common_ctx.get("buy_trend", "FAIL"),
            "vwap": "PASS" if common_ctx.get("above_vwap") else "FAIL",
            "rvol": "PASS" if common_ctx.get("rvol_ok") else "FAIL",
            "level_break": "PASS" if is_closed_bullish else "WAITING",
        }
        reasons, missing = [], []
        if is_closed_bullish:
            reasons.append(f"Completed 5m candle closed above PDH ({last_closed['close']:.2f} > {pdh:.2f})")
        else:
            missing.append("Waiting for 5m close above PDH")

        status = STATUS_CONFIRMED if (is_closed_bullish and not missing) else (STATUS_APPROACHING if price >= pdh * 0.998 else STATUS_WATCHING)
        confidence = 84 if status == STATUS_CONFIRMED else (64 if status == STATUS_APPROACHING else 50)

        return _build_pattern_result(
            pattern_id="PDH_PDL",
            pattern_name="Previous Day High Breakout",
            direction="BUY",
            status=status,
            confidence=confidence,
            trigger_price=pdh * 1.001,
            trigger_level=pdh,
            current_price=price,
            conditions=conds,
            reasons=reasons,
            missing_conditions=missing,
            invalidation=f"Price falls below PDH ({pdh:.2f})",
            entry_ready=(status == STATUS_CONFIRMED),
        )

    if pdl and (price <= pdl * 1.005 or is_closed_bearish):
        conds = {
            "trend": common_ctx.get("sell_trend", "FAIL"),
            "vwap": "PASS" if not common_ctx.get("above_vwap") else "FAIL",
            "rvol": "PASS" if common_ctx.get("rvol_ok") else "FAIL",
            "level_break": "PASS" if is_closed_bearish else "WAITING",
        }
        reasons, missing = [], []
        if is_closed_bearish:
            reasons.append(f"Completed 5m candle closed below PDL ({last_closed['close']:.2f} < {pdl:.2f})")
        else:
            missing.append("Waiting for 5m close below PDL")

        status = STATUS_CONFIRMED if (is_closed_bearish and not missing) else (STATUS_APPROACHING if price <= pdl * 1.002 else STATUS_WATCHING)
        confidence = 84 if status == STATUS_CONFIRMED else (64 if status == STATUS_APPROACHING else 50)

        return _build_pattern_result(
            pattern_id="PDH_PDL",
            pattern_name="Previous Day Low Breakdown",
            direction="SELL",
            status=status,
            confidence=confidence,
            trigger_price=pdl * 0.999,
            trigger_level=pdl,
            current_price=price,
            conditions=conds,
            reasons=reasons,
            missing_conditions=missing,
            invalidation=f"Price reclaims PDL ({pdl:.2f})",
            entry_ready=(status == STATUS_CONFIRMED),
        )

    return None


def eval_breakout_retest(
    symbol: str,
    candles_5m: List[dict],
    price: Optional[float],
    resistance: Optional[float],
    support: Optional[float],
    common_ctx: Dict[str, Any],
    settings: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Pattern #3: Breakout + Retest (Handles both BUY and SELL)."""
    if not candles_5m or not price or len(candles_5m) < 5:
        return None

    tol = settings.get("breakout_retest_tolerance_percent", 0.15) / 100
    last_closed = candles_5m[-1]

    # BUY Logic (Breakout of resistance)
    if resistance:
        level = resistance
        recent_closes = [c["close"] for c in candles_5m[-10:]]
        recent_lows = [c["low"] for c in candles_5m[-5:]]
        has_broken_out = any(cl > level * (1 + tol) for cl in recent_closes)
        has_retested = has_broken_out and any(abs(l - level) / level <= tol or (l <= level and cl >= level) for l, cl in zip(recent_lows, recent_closes[-5:]))
        rejection_candle = last_closed["close"] > last_closed["open"] and last_closed["close"] > level

        if has_broken_out:
            status = STATUS_CONFIRMED if (has_retested and rejection_candle) else (STATUS_APPROACHING if has_retested else STATUS_WATCHING)
            confidence = 90 if status == STATUS_CONFIRMED else (75 if status == STATUS_APPROACHING else 60)
            reasons, missing = [], []
            if has_broken_out: reasons.append(f"Price broke resistance ₹{level:.2f}")
            if has_retested: reasons.append(f"Price retested level as support")
            else: missing.append("Waiting for retest of broken resistance")
            if rejection_candle: reasons.append("Bullish rejection candle confirmed hold")
            else: missing.append("Waiting for bullish rejection")

            return _build_pattern_result(
                pattern_id="BREAKOUT_RETEST", pattern_name="Breakout + Retest", direction="BUY",
                status=status, confidence=confidence, trigger_price=last_closed["high"] * 1.001,
                trigger_level=level, current_price=price, conditions={"breakout": "PASS", "retest": "PASS" if has_retested else "WAITING", "rejection": "PASS" if rejection_candle else "WAITING"},
                reasons=reasons, missing_conditions=missing, invalidation=f"Price drops below {level:.2f}", entry_ready=(status == STATUS_CONFIRMED)
            )

    # SELL Logic (Breakdown of support)
    if support:
        level = support
        recent_closes = [c["close"] for c in candles_5m[-10:]]
        recent_highs = [c["high"] for c in candles_5m[-5:]]
        has_broken_down = any(cl < level * (1 - tol) for cl in recent_closes)
        has_retested = has_broken_down and any(abs(h - level) / level <= tol or (h >= level and cl <= level) for h, cl in zip(recent_highs, recent_closes[-5:]))
        rejection_candle = last_closed["close"] < last_closed["open"] and last_closed["close"] < level

        if has_broken_down:
            status = STATUS_CONFIRMED if (has_retested and rejection_candle) else (STATUS_APPROACHING if has_retested else STATUS_WATCHING)
            confidence = 90 if status == STATUS_CONFIRMED else (75 if status == STATUS_APPROACHING else 60)
            reasons, missing = [], []
            if has_broken_down: reasons.append(f"Price broke support ₹{level:.2f}")
            if has_retested: reasons.append(f"Price retested level as resistance")
            else: missing.append("Waiting for retest of broken support")
            if rejection_candle: reasons.append("Bearish rejection candle confirmed hold")
            else: missing.append("Waiting for bearish rejection")

            return _build_pattern_result(
                pattern_id="BREAKOUT_RETEST", pattern_name="Breakdown + Retest", direction="SELL",
                status=status, confidence=confidence, trigger_price=last_closed["low"] * 0.999,
                trigger_level=level, current_price=price, conditions={"breakout": "PASS", "retest": "PASS" if has_retested else "WAITING", "rejection": "PASS" if rejection_candle else "WAITING"},
                reasons=reasons, missing_conditions=missing, invalidation=f"Price reclaims {level:.2f}", entry_ready=(status == STATUS_CONFIRMED)
            )

    return None


def eval_vwap_pullback(
    symbol: str,
    candles_5m: List[dict],
    price: Optional[float],
    vwap: Optional[float],
    common_ctx: Dict[str, Any],
    settings: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Pattern #4: VWAP Pullback (Handles both BUY and SELL)."""
    if not candles_5m or not price or not vwap or len(candles_5m) < 5:
        return None

    tol = settings.get("vwap_pullback_tolerance_percent", 0.20) / 100
    last_c = candles_5m[-1]

    # BUY Logic
    is_bullish_trend = common_ctx.get("buy_trend") == "PASS"
    if is_bullish_trend:
        recent_lows = [c["low"] for c in candles_5m[-5:]]
        recent_closes = [c["close"] for c in candles_5m[-5:]]
        pullback_occurred = any(abs(l - vwap) / vwap <= tol or (l <= vwap and cl >= vwap * (1 - tol/2)) for l, cl in zip(recent_lows, recent_closes))
        rejection_candle = last_c["close"] > last_c["open"] and last_c["close"] > vwap

        if pullback_occurred or abs(price - vwap) / vwap <= tol * 1.5:
            reasons, missing = ["Trend alignment is BULLISH", f"Price near VWAP (₹{vwap:.2f})"], []
            if pullback_occurred: reasons.append("VWAP level held as support")
            else: missing.append("Waiting for pullback to VWAP")
            if rejection_candle: reasons.append("Bullish rejection confirmed")
            else: missing.append("Waiting for bullish rejection")

            status = STATUS_CONFIRMED if (pullback_occurred and rejection_candle) else STATUS_APPROACHING
            return _build_pattern_result(
                pattern_id="VWAP_PULLBACK", pattern_name="VWAP Pullback", direction="BUY",
                status=status, confidence=82 if status == STATUS_CONFIRMED else 68, trigger_price=last_c["high"] * 1.001,
                trigger_level=vwap, current_price=price, conditions={"trend": "PASS", "vwap_hold": "PASS" if pullback_occurred else "WAITING", "rejection": "PASS" if rejection_candle else "WAITING"},
                reasons=reasons, missing_conditions=missing, invalidation=f"Price closes below VWAP ({vwap:.2f})", entry_ready=(status == STATUS_CONFIRMED)
            )

    # SELL Logic
    is_bearish_trend = common_ctx.get("sell_trend") == "PASS"
    if is_bearish_trend:
        recent_highs = [c["high"] for c in candles_5m[-5:]]
        recent_closes = [c["close"] for c in candles_5m[-5:]]
        pullback_occurred = any(abs(h - vwap) / vwap <= tol or (h >= vwap and cl <= vwap * (1 + tol/2)) for h, cl in zip(recent_highs, recent_closes))
        rejection_candle = last_c["close"] < last_c["open"] and last_c["close"] < vwap

        if pullback_occurred or abs(price - vwap) / vwap <= tol * 1.5:
            reasons, missing = ["Trend alignment is BEARISH", f"Price near VWAP (₹{vwap:.2f})"], []
            if pullback_occurred: reasons.append("VWAP level held as resistance")
            else: missing.append("Waiting for pullback to VWAP")
            if rejection_candle: reasons.append("Bearish rejection confirmed")
            else: missing.append("Waiting for bearish rejection")

            status = STATUS_CONFIRMED if (pullback_occurred and rejection_candle) else STATUS_APPROACHING
            return _build_pattern_result(
                pattern_id="VWAP_PULLBACK", pattern_name="VWAP Pullback", direction="SELL",
                status=status, confidence=82 if status == STATUS_CONFIRMED else 68, trigger_price=last_c["low"] * 0.999,
                trigger_level=vwap, current_price=price, conditions={"trend": "PASS", "vwap_hold": "PASS" if pullback_occurred else "WAITING", "rejection": "PASS" if rejection_candle else "WAITING"},
                reasons=reasons, missing_conditions=missing, invalidation=f"Price closes above VWAP ({vwap:.2f})", entry_ready=(status == STATUS_CONFIRMED)
            )

    return None


def eval_ema20_pullback(
    symbol: str,
    candles_5m: List[dict],
    price: Optional[float],
    common_ctx: Dict[str, Any],
    settings: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Pattern #5: EMA20 Pullback."""
    if not candles_5m or not price or len(candles_5m) < 20:
        return None

    closes = pd.Series([c["close"] for c in candles_5m])
    ema20 = closes.ewm(span=settings.get("ema_fast", 20), adjust=False).mean().iloc[-1]
    ema50 = closes.ewm(span=settings.get("ema_slow", 50), adjust=False).mean().iloc[-1]

    if ema20 <= ema50:
        return None

    tol = settings.get("ema20_pullback_tolerance_percent", 0.20) / 100
    recent_lows = [c["low"] for c in candles_5m[-4:]]
    recent_closes = [c["close"] for c in candles_5m[-4:]]
    last_c = candles_5m[-1]

    pullback_occurred = any(abs(l - ema20) / ema20 <= tol or (l <= ema20 and cl >= ema20 * (1 - tol/2)) for l, cl in zip(recent_lows, recent_closes))
    rejection_candle = last_c["close"] > last_c["open"] and last_c["close"] > ema20

    if pullback_occurred or abs(price - ema20) / ema20 <= tol * 1.5:
        reasons, missing = [], []
        reasons.append(f"EMA20 ({ema20:.2f}) is above EMA50 ({ema50:.2f}) [Bullish Structure]")

        if pullback_occurred: reasons.append("Price pulled back to touch EMA20 support")
        else: missing.append("Waiting for pullback to EMA20")

        if rejection_candle: reasons.append("5m bullish rejection candle confirmed EMA20 hold")
        else: missing.append("Waiting for 5m bullish rejection candle close")

        status = STATUS_CONFIRMED if (pullback_occurred and rejection_candle) else STATUS_APPROACHING
        confidence = 80 if status == STATUS_CONFIRMED else 65

        return _build_pattern_result(
            pattern_id="EMA20_PULLBACK",
            pattern_name="EMA20 Pullback",
            direction="BUY",
            status=status,
            confidence=confidence,
            trigger_price=last_c["high"] * 1.001,
            trigger_level=ema20,
            current_price=price,
            conditions={"ema_structure": "PASS", "pullback": "PASS" if pullback_occurred else "WAITING", "rejection": "PASS" if rejection_candle else "WAITING"},
            reasons=reasons,
            missing_conditions=missing,
            invalidation=f"EMA20 crosses below EMA50 or price drops below EMA50 ({ema50:.2f})",
            entry_ready=(status == STATUS_CONFIRMED),
        )

    return None


def eval_failed_orb(
    symbol: str,
    candles_5m: List[dict],
    price: Optional[float],
    or_levels: Dict[str, Any],
    common_ctx: Dict[str, Any],
    settings: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Pattern #6: Failed Opening Breakout / Breakdown (Trap Reversal - BUY and SELL)."""
    orh = or_levels.get("orh")
    orl = or_levels.get("orl")
    if not candles_5m or not price or not (orh and orl) or len(candles_5m) < 4:
        return None

    recent_lows = [c["low"] for c in candles_5m[-6:]]
    recent_highs = [c["high"] for c in candles_5m[-6:]]
    last_c = candles_5m[-1]

    # Bullish Trap (Failed Breakdown below ORL -> BUY)
    broke_orl = any(l < orl for l in recent_lows)
    reclaimed_orl = broke_orl and last_c["close"] > orl and last_c["close"] > last_c["open"]

    if broke_orl and reclaimed_orl:
        reasons = [
            f"Price broke below ORL ({orl:.2f}) trapping sellers",
            f"Breakdown failed and price reclaimed ORL (5m Close: {last_c['close']:.2f})",
            "Bullish reversal candle confirmed reclaim",
        ]
        return _build_pattern_result(
            pattern_id="FAILED_ORB",
            pattern_name="Failed Opening Breakdown (Reversal)",
            direction="BUY",
            status=STATUS_CONFIRMED,
            confidence=85,
            trigger_price=last_c["high"] * 1.001,
            trigger_level=orl,
            current_price=price,
            conditions={"trap_detected": "PASS", "reclaim": "PASS", "reversal_candle": "PASS"},
            reasons=reasons,
            missing_conditions=[],
            invalidation=f"Price drops below recent low ({min(recent_lows):.2f})",
            entry_ready=True,
        )

    # Bearish Trap (Failed Breakout above ORH -> SELL)
    broke_orh = any(h > orh for h in recent_highs)
    rejected_orh = broke_orh and last_c["close"] < orh and last_c["close"] < last_c["open"]

    if broke_orh and rejected_orh:
        reasons = [
            f"Price broke above ORH ({orh:.2f}) trapping buyers",
            f"Breakout failed and price rejected below ORH (5m Close: {last_c['close']:.2f})",
            "Bearish reversal candle confirmed rejection",
        ]
        return _build_pattern_result(
            pattern_id="FAILED_ORB",
            pattern_name="Failed Opening Breakout (Reversal)",
            direction="SELL",
            status=STATUS_CONFIRMED,
            confidence=85,
            trigger_price=last_c["low"] * 0.999,
            trigger_level=orh,
            current_price=price,
            conditions={"trap_detected": "PASS", "rejection": "PASS", "reversal_candle": "PASS"},
            reasons=reasons,
            missing_conditions=[],
            invalidation=f"Price rises above recent high ({max(recent_highs):.2f})",
            entry_ready=True,
        )

    return None



def eval_gap_and_go(
    symbol: str,
    candles_5m: List[dict],
    price: Optional[float],
    gap_info: Dict[str, Any],
    common_ctx: Dict[str, Any],
    settings: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Pattern #7: Gap-and-Go (BUY and SELL)."""
    if not candles_5m or not price or not gap_info:
        return None

    gap_pct = gap_info.get("gap_percent", 0.0)
    min_gap = settings.get("min_gap_percent", 1.0)
    if abs(gap_pct) < min_gap:
        return None

    last_c = candles_5m[-1]
    is_gap_up = gap_pct >= min_gap
    is_gap_down = gap_pct <= -min_gap
    vwap = common_ctx.get("vwap")
    above_vwap = common_ctx.get("above_vwap", False)
    rvol_ok = common_ctx.get("rvol_ok", False)

    if is_gap_up:
        holding_vwap = above_vwap
        breakout_continuation = last_c["close"] > candles_5m[0]["high"]
        status = STATUS_CONFIRMED if (holding_vwap and breakout_continuation and rvol_ok) else STATUS_APPROACHING
        confidence = 83 if status == STATUS_CONFIRMED else 67

        reasons, missing = [f"Meaningful gap-up detected (+{gap_pct:.2f}%)"], []
        if holding_vwap: reasons.append("Price holding above VWAP")
        else: missing.append("Price below VWAP")
        if breakout_continuation: reasons.append("Price broke above opening high")
        else: missing.append("Waiting for continuation above opening high")
        if rvol_ok: reasons.append("RVOL expansion confirmed")
        else: missing.append("RVOL expansion missing")

        return _build_pattern_result(
            pattern_id="GAP_AND_GO", pattern_name="Gap-and-Go", direction="BUY",
            status=status, confidence=confidence, trigger_price=candles_5m[0]["high"] * 1.001,
            trigger_level=candles_5m[0]["high"], current_price=price,
            conditions={"gap_quality": "PASS", "vwap_hold": "PASS" if holding_vwap else "FAIL", "continuation": "PASS" if breakout_continuation else "WAITING"},
            reasons=reasons, missing_conditions=missing, invalidation="Gap filled or VWAP lost", entry_ready=(status == STATUS_CONFIRMED)
        )

    if is_gap_down:
        holding_vwap = not above_vwap if vwap else True
        breakout_continuation = last_c["close"] < candles_5m[0]["low"]
        status = STATUS_CONFIRMED if (holding_vwap and breakout_continuation and rvol_ok) else STATUS_APPROACHING
        confidence = 83 if status == STATUS_CONFIRMED else 67

        reasons, missing = [f"Meaningful gap-down detected ({gap_pct:.2f}%)"], []
        if holding_vwap: reasons.append("Price holding below VWAP")
        else: missing.append("Price above VWAP")
        if breakout_continuation: reasons.append("Price broke below opening low")
        else: missing.append("Waiting for continuation below opening low")
        if rvol_ok: reasons.append("RVOL expansion confirmed")
        else: missing.append("RVOL expansion missing")

        return _build_pattern_result(
            pattern_id="GAP_AND_GO", pattern_name="Gap-and-Go", direction="SELL",
            status=status, confidence=confidence, trigger_price=candles_5m[0]["low"] * 0.999,
            trigger_level=candles_5m[0]["low"], current_price=price,
            conditions={"gap_quality": "PASS", "vwap_hold": "PASS" if holding_vwap else "FAIL", "continuation": "PASS" if breakout_continuation else "WAITING"},
            reasons=reasons, missing_conditions=missing, invalidation="Gap filled or VWAP reclaimed", entry_ready=(status == STATUS_CONFIRMED)
        )

    return None


def eval_inside_bar(
    symbol: str,
    candles_5m: List[dict],
    price: Optional[float],
    common_ctx: Dict[str, Any],
    settings: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Pattern #8: Inside-Bar Breakout / Breakdown (BUY and SELL)."""
    if not candles_5m or not price or len(candles_5m) < 3:
        return None

    mother_bar = candles_5m[-3]
    inside_bar = candles_5m[-2]
    trigger_bar = candles_5m[-1]

    is_inside = (inside_bar["high"] < mother_bar["high"]) and (inside_bar["low"] > mother_bar["low"])
    if not is_inside:
        return None

    inside_high = inside_bar["high"]
    inside_low = inside_bar["low"]

    # Bullish breakout
    if trigger_bar["close"] > inside_high and common_ctx.get("buy_trend") == "PASS":
        reasons = [f"Inside bar detected (Range: ₹{inside_low:.2f} - ₹{inside_high:.2f})", f"5m close above inside-bar high"]
        return _build_pattern_result(
            pattern_id="INSIDE_BAR", pattern_name="Inside-Bar Breakout", direction="BUY",
            status=STATUS_CONFIRMED, confidence=80, trigger_price=inside_high * 1.001,
            trigger_level=inside_high, current_price=price,
            conditions={"inside_bar": "PASS", "trend": "PASS", "breakout_close": "PASS"},
            reasons=reasons, missing_conditions=[], invalidation=f"Price drops below inside-bar low ({inside_low:.2f})", entry_ready=True
        )

    # Bearish breakdown
    if trigger_bar["close"] < inside_low and common_ctx.get("sell_trend") == "PASS":
        reasons = [f"Inside bar detected (Range: ₹{inside_low:.2f} - ₹{inside_high:.2f})", f"5m close below inside-bar low"]
        return _build_pattern_result(
            pattern_id="INSIDE_BAR", pattern_name="Inside-Bar Breakdown", direction="SELL",
            status=STATUS_CONFIRMED, confidence=80, trigger_price=inside_low * 0.999,
            trigger_level=inside_low, current_price=price,
            conditions={"inside_bar": "PASS", "trend": "PASS", "breakout_close": "PASS"},
            reasons=reasons, missing_conditions=[], invalidation=f"Price rises above inside-bar high ({inside_high:.2f})", entry_ready=True
        )

    return None



def evaluate_all_patterns(
    symbol: str,
    candles_5m: List[dict],
    candles_15m: List[dict],
    candles_daily: List[dict],
    quote: Optional[Dict[str, Any]],
    indicators: Dict[str, Any],
    levels: Dict[str, Any],
    pd_levels: Dict[str, Any],
    or_levels: Dict[str, Any],
    gap_info: Dict[str, Any],
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    """Run all enabled patterns for a symbol and return pattern state summary."""
    price = float(quote["last_price"]) if (quote and quote.get("last_price") is not None) else (candles_5m[-1]["close"] if candles_5m else None)
    vwap = indicators.get("vwap")
    rvol = indicators.get("rvol")
    atr_5m = indicators.get("atr_5m")

    common_ctx = {
        "price": price,
        "vwap": vwap,
        "above_vwap": (price > vwap) if (price and vwap) else False,
        "rvol": rvol,
        "rvol_ok": (rvol is not None and rvol >= settings.get("min_rvol", 1.5)),
        "atr_ok": (atr_5m is not None and atr_5m > 0),
        "buy_trend": "PASS" if indicators.get("daily_trend") == "BULLISH" and indicators.get("m15_trend") == "BULLISH" else "FAIL",
        "sell_trend": "PASS" if indicators.get("daily_trend") == "BEARISH" and indicators.get("m15_trend") == "BEARISH" else "FAIL",
    }

    pattern_cfg = settings.get("patterns", {}).get("enabled", {})
    evaluated_patterns: List[Dict[str, Any]] = []

    evaluators = [
        ("orb", lambda: eval_orb(symbol, candles_5m, price, or_levels, common_ctx, settings)),
        ("pdh_pdl", lambda: eval_pdh_pdl(symbol, candles_5m, price, pd_levels, common_ctx, settings)),
        ("breakout_retest", lambda: eval_breakout_retest(symbol, candles_5m, price, levels.get("resistance"), levels.get("support"), common_ctx, settings)),
        ("vwap_pullback", lambda: eval_vwap_pullback(symbol, candles_5m, price, vwap, common_ctx, settings)),
        ("ema20_pullback", lambda: eval_ema20_pullback(symbol, candles_5m, price, common_ctx, settings)),
        ("failed_orb", lambda: eval_failed_orb(symbol, candles_5m, price, or_levels, common_ctx, settings)),
        ("gap_and_go", lambda: eval_gap_and_go(symbol, candles_5m, price, gap_info, common_ctx, settings)),
        ("inside_bar", lambda: eval_inside_bar(symbol, candles_5m, price, common_ctx, settings)),
    ]

    for cfg_key, eval_fn in evaluators:
        if pattern_cfg.get(cfg_key, True):
            try:
                res = eval_fn()
                if res:
                    evaluated_patterns.append(res)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning(f"Pattern {cfg_key} evaluation failed for {symbol}: {exc}")

    confirmed_buys = [p for p in evaluated_patterns if p["direction"] == "BUY" and p["status"] == STATUS_CONFIRMED]
    confirmed_sells = [p for p in evaluated_patterns if p["direction"] == "SELL" and p["status"] == STATUS_CONFIRMED]

    # Count independent families
    buy_families = {PATTERN_FAMILIES.get(p["pattern_id"]) for p in confirmed_buys}
    sell_families = {PATTERN_FAMILIES.get(p["pattern_id"]) for p in confirmed_sells}
    pattern_agreement_count = max(len(buy_families), len(sell_families))
    agreement_level = "HIGH" if pattern_agreement_count >= 2 else ("MEDIUM" if pattern_agreement_count == 1 else "LOW")

    evaluated_patterns.sort(key=lambda p: (1 if p["status"] == STATUS_CONFIRMED else (0.5 if p["status"] == STATUS_APPROACHING else 0), p["confidence"]), reverse=True)
    primary_pattern = evaluated_patterns[0] if evaluated_patterns else None
    authoritative_setup = _select_pattern_setup(evaluated_patterns, settings)

    return {
        "symbol": symbol,
        "primary_pattern": primary_pattern,
        "authoritative_setup": authoritative_setup,
        "pattern_count": len(evaluated_patterns),
        "pattern_agreement_count": pattern_agreement_count,
        "agreement_level": agreement_level,
        "active_patterns": evaluated_patterns,
    }

