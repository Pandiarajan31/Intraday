"""
positions.py
=============
Manual intraday position tracking + live "book profit / exit now" alerting.

This does NOT place real orders - it lets you tell the app "I entered
<symbol> at <price>", after which it:
  1. Computes a stop-loss and T1/T2/T3 targets using the SAME ATR-multiple
     risk model engine.py already uses for its own auto-generated signals
     (atr_multiplier_sl / atr_multiplier_targets in intraday_config.json),
     so manually-entered trades get consistent risk math.
  2. Re-checks each OPEN position on a fast, independent cycle (see
     dashboard.py's position-checker thread) for:
       - stop-loss hit
       - T1 / T2 / T3 hit
       - a candle-pattern reversal (shooting star / bearish engulfing for
         a BUY, hammer / bullish engulfing for a SELL) while the position
         is in profit
       - RSI rolling over from overbought/oversold while in profit
       - price losing VWAP against the position while in profit
     and raises an "alert" the dashboard surfaces immediately (banner +
     sound), without auto-closing the position - a human always decides.

Positions persist to POSITIONS_FILE (JSON) so open positions survive a
dashboard restart.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
import datetime as dt
from typing import Any, Dict, List, Optional

from shared import DataProvider, Indicators
import trade_forecast

POSITIONS_FILE = os.environ.get("POSITIONS_FILE", "positions.json")
_LOCK = threading.Lock()

ALERT_MESSAGES = {
    "STOP_LOSS_HIT": "Price hit your stop-loss. Consider exiting now.",
    "BREAKEVEN_STOP_HIT": "Price came back to your entry (breakeven stop). Exit the rest - no loss taken.",
    "TRAIL_STOP_HIT": "Trailing stop hit - exit the rest and keep the profit.",
    "TARGET_1_HIT": "T1 reached - BOOK 50% NOW. Stop moved to entry (breakeven); the rest now trails.",
    "TARGET_2_HIT": "T2 reached - book another part; keep trailing the rest.",
    "TARGET_3_HIT": "Final target T3 reached.",
    "TIME_STOP": "No real progress since entry - exit. Breakouts that stall usually fail.",
    "SQUARE_OFF_TIME": "Square-off time near - close this intraday position now.",
    "REVERSAL_PATTERN": "Reversal candle detected while in profit - consider booking profit now.",
    "MOMENTUM_FADING": "Momentum rolling over while in profit - consider tightening your stop.",
    "VWAP_LOST": "Price moved back across VWAP while in profit - bias may be flipping.",
}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _load() -> Dict[str, Any]:
    if not os.path.exists(POSITIONS_FILE):
        return {"positions": []}
    try:
        with open(POSITIONS_FILE) as f:
            data = json.load(f)
            if "positions" not in data:
                data["positions"] = []
            return data
    except Exception:
        return {"positions": []}


def _save(data: Dict[str, Any]) -> None:
    tmp = POSITIONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, POSITIONS_FILE)


def _last(indicator_obj: Optional[Dict[str, Any]]) -> Optional[float]:
    if not indicator_obj:
        return None
    vals = [v for v in (indicator_obj.get("values") or []) if v is not None]
    return vals[-1] if vals else None


# ---------------------------------------------------------------------------
# Structural stop + R-multiple targets (shared with engine.py's entry zone)
# ---------------------------------------------------------------------------

def structural_stop_plan(candles: List[dict], direction: str, entry_price: float,
                         atr: Optional[float], settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Stop beyond the recent 5m swing (lowest low / highest high of the
    last `structural_stop_lookback` COMPLETED candles) plus a small ATR
    buffer, instead of a fixed 1x ATR from entry. A fixed-distance stop
    sits where normal noise reaches it; a stop beyond structure only gets
    hit if the setup is actually wrong.

    Stop distance is clamped to [structural_stop_min_atr, ...] so it is
    never razor-thin; if it would need more than structural_stop_max_atr
    the entry is too far from structure (`too_wide` = True).
    Targets are R-multiples: T1 = 1R (book 50%, stop -> entry),
    T2 = 2R, T3 = 3R (trail the rest).
    Caller passes only completed candles."""
    if not candles or not atr or atr <= 0 or not entry_price:
        return None
    lookback = settings.get("structural_stop_lookback", 6)
    buffer = settings.get("structural_stop_buffer_atr", 0.2) * atr
    min_dist = settings.get("structural_stop_min_atr", 0.6) * atr
    max_dist = settings.get("structural_stop_max_atr", 2.0) * atr
    window = candles[-lookback:]
    if direction == "BUY":
        stop = min(c["low"] for c in window) - buffer
        dist = max(entry_price - stop, min_dist)
        stop = entry_price - dist
    else:
        stop = max(c["high"] for c in window) + buffer
        dist = max(stop - entry_price, min_dist)
        stop = entry_price + dist
    sign = 1 if direction == "BUY" else -1
    return {
        "stop_loss": round(stop, 2),
        "risk_per_share": round(dist, 2),
        "t1": round(entry_price + sign * dist, 2),
        "t2": round(entry_price + sign * 2 * dist, 2),
        "t3": round(entry_price + sign * 3 * dist, 2),
        "stop_atr": round(dist / atr, 2),
        "too_wide": dist > max_dist,
    }


def round_trip_cost(price: float, settings: Dict[str, Any]) -> float:
    """Estimated brokerage + STT + exchange/SEBI + GST + stamp + slippage
    for one intraday round trip, per share (settings round_trip_cost_pct,
    default 0.08% of price)."""
    return price * settings.get("round_trip_cost_pct", 0.08) / 100


# ---------------------------------------------------------------------------
# Daily discipline limits (max trades, losing streak, daily loss)
# ---------------------------------------------------------------------------

def _pnl_amount(p: Dict[str, Any]) -> Optional[float]:
    if p.get("exit_price") is None or not p.get("quantity"):
        return None
    diff = p["exit_price"] - p["entry_price"]
    return diff * p["quantity"] if p["direction"] == "BUY" else -diff * p["quantity"]


def trading_day_status(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Today's discipline state from the logged positions. Most intraday
    accounts are lost to over-trading and revenge trades after losses,
    not to the signals themselves - so once a limit is hit, new entries
    are blocked for the day (when settings['risk_guardrails'] is on)."""
    today = dt.date.today().isoformat()
    all_pos = _load().get("positions", [])
    entered_today = [p for p in all_pos if str(p.get("entry_time", "")).startswith(today)]
    closed_today = sorted(
        [p for p in all_pos if p.get("status") == "CLOSED" and str(p.get("exit_time", "")).startswith(today)],
        key=lambda p: p.get("exit_time", ""))
    open_now = [p for p in all_pos if p.get("status") == "OPEN"]

    realized = 0.0
    losing_streak = 0
    for p in closed_today:
        amt = _pnl_amount(p)
        if amt is not None:
            realized += amt
        exit_p = p.get("exit_price")
        if exit_p is None:
            continue
        won = (exit_p > p["entry_price"]) if p["direction"] == "BUY" else (exit_p < p["entry_price"])
        losing_streak = 0 if won else losing_streak + 1

    pr = settings.get("paper_risk", {}) or {}
    equity = pr.get("account_equity", 100000)
    max_trades = settings.get("max_trades_per_day", 4)
    max_streak = settings.get("max_consecutive_losses", 3)
    max_loss_pct = pr.get("max_daily_loss_percent", 2.0)
    max_open = pr.get("max_open_positions", 3)

    reasons = []
    if len(entered_today) >= max_trades:
        reasons.append(f"{len(entered_today)} trades today (max {max_trades})")
    if losing_streak >= max_streak:
        reasons.append(f"{losing_streak} losses in a row (max {max_streak}) - stop for today")
    if realized <= -(equity * max_loss_pct / 100):
        reasons.append(f"Daily loss ₹{abs(realized):,.0f} reached limit ({max_loss_pct}% of ₹{equity:,.0f})")
    if len(open_now) >= max_open:
        reasons.append(f"{len(open_now)} positions already open (max {max_open})")

    enabled = settings.get("risk_guardrails", False)
    return {
        "enabled": enabled,
        "blocked": enabled and bool(reasons),
        "reasons": reasons,
        "trades_today": len(entered_today),
        "max_trades": max_trades,
        "losing_streak": losing_streak,
        "realized_pnl": round(realized, 2),
        "open_positions": len(open_now),
    }


# ---------------------------------------------------------------------------
# Opening a position: compute SL / T1 / T2 / T3
# ---------------------------------------------------------------------------

def _compute_plan(symbol: str, direction: str, entry_price: float, settings: Dict[str, Any],
                   exchange: str = "nse") -> Dict[str, Any]:
    """Structural stop + R-multiple targets (structural_stop_plan) - the
    same plan the dashboard's Entry Zone shows. Falls back to the
    ATR-multiple model (atr_multiplier_sl / atr_multiplier_targets) when
    there isn't enough candle data for a structural stop."""
    m5 = DataProvider.get_ohlcv(symbol, period="10d", interval="5m", exchange=exchange)
    candles = m5.get("candles", []) if m5 else []

    atr = _last(Indicators.atr(candles, settings.get("atr_period", 14))) if candles else None
    if not atr or atr <= 0:
        # Thin/no data fallback: 0.5% of entry price, clearly flagged.
        atr = entry_price * 0.005
        atr_is_fallback = True
    else:
        atr_is_fallback = False

    atr_mult_sl = settings.get("atr_multiplier_sl", 1.0)
    atr_mult_tgts = settings.get("atr_multiplier_targets", [1.0, 1.5, 2.0])

    # Last candle may still be forming - exclude it from the swing.
    struct = structural_stop_plan(candles[:-1], direction, entry_price, atr, settings) \
        if not atr_is_fallback and len(candles) > 2 else None
    if struct:
        stop_loss, t1, t2, t3 = struct["stop_loss"], struct["t1"], struct["t2"], struct["t3"]
    elif direction == "BUY":
        stop_loss = entry_price - atr_mult_sl * atr
        t1 = entry_price + atr_mult_tgts[0] * atr
        t2 = entry_price + atr_mult_tgts[1] * atr
        t3 = entry_price + atr_mult_tgts[2] * atr
    else:
        stop_loss = entry_price + atr_mult_sl * atr
        t1 = entry_price - atr_mult_tgts[0] * atr
        t2 = entry_price - atr_mult_tgts[1] * atr
        t3 = entry_price - atr_mult_tgts[2] * atr

    risk = abs(entry_price - stop_loss)
    rr_t1 = round(abs(t1 - entry_price) / risk, 2) if risk else None
    rr_t3 = round(abs(t3 - entry_price) / risk, 2) if risk else None

    return {
        "atr": round(atr, 2),
        "atr_is_fallback": atr_is_fallback,
        "stop_type": "STRUCTURAL" if struct else "ATR",
        "initial_stop_loss": round(stop_loss, 2),
        "stop_loss": round(stop_loss, 2),
        "t1": round(t1, 2),
        "t2": round(t2, 2),
        "t3": round(t3, 2),
        "rr_t1": rr_t1,
        "rr_t3": rr_t3,
    }


def open_position(symbol: str, direction: str, settings: Dict[str, Any],
                   entry_price: Optional[float] = None,
                   entry_price_low: Optional[float] = None,
                   entry_price_high: Optional[float] = None,
                   quantity: Optional[float] = None,
                   amount_invested: Optional[float] = None,
                   account_size: Optional[float] = None,
                   risk_percent: Optional[float] = None,
                   notify_enabled: bool = True,
                   exchange: str = "nse",
                   notes: Optional[str] = None) -> Dict[str, Any]:
    """Register a manually-entered intraday trade. Accepts either a single
    entry_price OR a (entry_price_low, entry_price_high) range - if a range
    is given, the midpoint is used for P&L/target math and the range is kept
    for display (this mirrors how price-range trade calls are usually
    communicated - "buy between 100-102").

    Position sizing - three options, in this precedence order:
      1. quantity (number of shares) - if given, used directly.
      2. amount_invested (total capital, e.g. 50000) - implied quantity is
         entry-price based: amount_invested / entry_price.
      3. account_size + risk_percent (e.g. account_size=100000,
         risk_percent=1.0 -> risk 1% of a 1 lakh account = 1000 on this
         trade) - implied quantity is STOP-DISTANCE based:
         (account_size * risk_percent/100) / abs(entry_price - stop_loss).
         This is the standard "risk-based sizing" approach: the same %
         risk per trade regardless of how far away the stop happens to be,
         rather than a fixed rupee amount that could be way oversized
         relative to a wide stop or way undersized relative to a tight one.
    If none of the three are given, quantity is left as None (P&L is still
    shown in %, just not in currency)."""
    direction = (direction or "").upper()
    if direction not in ("BUY", "SELL"):
        raise ValueError("direction must be BUY or SELL")

    if entry_price is None:
        if entry_price_low is None or entry_price_high is None:
            raise ValueError("provide entry_price, or both entry_price_low and entry_price_high")
        entry_price = (float(entry_price_low) + float(entry_price_high)) / 2.0
    entry_price = float(entry_price)

    symbol = symbol.upper().strip()
    # Computed BEFORE sizing resolution below - risk-percent sizing needs
    # the stop-loss distance to derive quantity.
    plan = _compute_plan(symbol, direction, entry_price, settings, exchange)

    sizing_method = None
    risk_amount = None
    if quantity is not None:
        sizing_method = "quantity"
    elif amount_invested is not None and entry_price > 0:
        quantity = round(float(amount_invested) / entry_price, 4)
        sizing_method = "amount_invested"
    elif account_size is not None and risk_percent is not None:
        stop_distance = abs(entry_price - plan["stop_loss"])
        if stop_distance > 0:
            risk_amount = round(float(account_size) * (float(risk_percent) / 100.0), 2)
            quantity = round(risk_amount / stop_distance, 4)
            amount_invested = round(quantity * entry_price, 2)
            sizing_method = "risk_percent"
            if plan.get("atr_is_fallback"):
                # ATR came from the 0.5%-of-price fallback (thin/no candle
                # data), not real volatility - the stop distance (and
                # therefore this sizing) is a rougher estimate than usual.
                sizing_method = "risk_percent_atr_fallback"

    position = {
        "id": uuid.uuid4().hex[:10],
        "symbol": symbol,
        "exchange": exchange,
        "direction": direction,
        "entry_price": round(entry_price, 2),
        "entry_price_low": round(float(entry_price_low), 2) if entry_price_low is not None else None,
        "entry_price_high": round(float(entry_price_high), 2) if entry_price_high is not None else None,
        "quantity": quantity,
        "amount_invested": round(float(amount_invested), 2) if amount_invested is not None else None,
        "sizing_method": sizing_method,
        "risk_amount": risk_amount,   # actual rupees at risk if stop-loss is hit, when risk-percent sizing was used
        "notify_enabled": bool(notify_enabled),
        "entry_time": dt.datetime.now().isoformat(timespec="seconds"),
        "status": "OPEN",
        "notes": notes,
        **plan,
        "alert": None,
        "alert_message": None,
        "alert_time": None,
        "last_checked": None,
        "last_price": None,
        "unrealized_pnl_percent": None,
        "unrealized_pnl_amount": None,
        "outlook": None,          # filled in by trade_forecast.analyze_trade_outlook
        "exit_price": None,
        "exit_time": None,
        "exit_reason": None,
        "last_error": None,
    }
    with _LOCK:
        data = _load()
        data["positions"].append(position)
        _save(data)
    return position


def delete_position(position_id: str) -> bool:
    """Fully remove a position from the tracker (any status) - distinct
    from close_position(), which keeps a CLOSED record for history. Use
    this for "I don't want to track this anymore", e.g. added by mistake
    or just want it off the list."""
    with _LOCK:
        data = _load()
        before = len(data["positions"])
        data["positions"] = [p for p in data["positions"] if p["id"] != position_id]
        changed = len(data["positions"]) != before
        if changed:
            _save(data)
        return changed


def close_position(position_id: str, exit_price: Optional[float] = None,
                    reason: str = "MANUAL_CLOSE") -> Optional[Dict[str, Any]]:
    with _LOCK:
        data = _load()
        for p in data["positions"]:
            if p["id"] == position_id and p["status"] == "OPEN":
                p["status"] = "CLOSED"
                p["exit_price"] = round(float(exit_price), 2) if exit_price is not None else p.get("last_price")
                p["exit_time"] = dt.datetime.now().isoformat(timespec="seconds")
                p["exit_reason"] = reason
                _save(data)
                return p
    return None


def acknowledge_alert(position_id: str) -> Optional[Dict[str, Any]]:
    """Clear the alert flag without closing the position (user saw it and
    chose to hold)."""
    with _LOCK:
        data = _load()
        for p in data["positions"]:
            if p["id"] == position_id:
                p["alert"] = None
                p["alert_message"] = None
                _save(data)
                return p
    return None


def list_positions(status: Optional[str] = None) -> List[Dict[str, Any]]:
    data = _load()
    positions = data.get("positions", [])
    if status:
        positions = [p for p in positions if p["status"] == status]
    return positions


# ---------------------------------------------------------------------------
# Exit-signal evaluation
# ---------------------------------------------------------------------------

def _is_reversal_candle(candles: List[dict], direction: str):
    """Well-known single/two-candle reversal patterns, used ONLY as an exit
    nudge (deliberately looser than the entry hard-gate - a false-positive
    "consider booking" hint is far cheaper than a missed exit)."""
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


def evaluate_exit(position: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    """Re-check ONE open position against fresh market data and update it
    in place. Sets position['alert'] whenever something worth acting on
    fires, and position['outlook'] with a candle-quality-based read on
    whether it's currently leaning toward target or stop-loss. Sends a
    Telegram push (if configured, and notify_enabled for this position)
    on any NEW alert or a meaningful outlook flip. Never auto-closes -
    only flags."""
    symbol = position["symbol"]
    exchange = position.get("exchange", "nse")
    direction = position["direction"]
    entry = position["entry_price"]
    notify_on = position.get("notify_enabled", True)

    # Capture prior state BEFORE this cycle overwrites it, so we can tell
    # a genuinely NEW alert / outlook flip apart from "still the same as
    # last check" - otherwise we'd re-notify every cycle forever.
    prev_alert = position.get("alert")
    prev_verdict = (position.get("outlook") or {}).get("verdict")

    m5 = DataProvider.get_ohlcv(symbol, period="10d", interval="5m", exchange=exchange)
    candles = m5.get("candles", []) if m5 else []
    quote = DataProvider.get_realtime_quote(symbol, exchange=exchange) or {}
    price = quote.get("last_price") or (candles[-1]["close"] if candles else None)

    position["last_checked"] = dt.datetime.now().isoformat(timespec="seconds")
    position["last_error"] = None
    if price is None or not candles:
        position["last_error"] = "No fresh price/candle data this cycle"
        return position

    price = float(price)
    position["last_price"] = round(price, 2)
    pnl_pct = ((price - entry) / entry * 100) if direction == "BUY" else ((entry - price) / entry * 100)
    position["unrealized_pnl_percent"] = round(pnl_pct, 2)
    if position.get("quantity"):
        pnl_amount = (price - entry) * position["quantity"] if direction == "BUY" else (entry - price) * position["quantity"]
        position["unrealized_pnl_amount"] = round(pnl_amount, 2)

    alert = None
    is_buy = direction == "BUY"
    initial_risk = abs(entry - position.get("initial_stop_loss", position["stop_loss"]))
    favorable = (price - entry) if is_buy else (entry - price)
    position["max_favorable"] = round(max(position.get("max_favorable") or 0.0, favorable), 2)

    def _beyond(a, b):
        """a is at/through b in the trade's favour."""
        return a >= b if is_buy else a <= b

    # 1. Hard levels - unambiguous, checked first.
    stop = position["stop_loss"]
    if not _beyond(price, stop) or price == stop:
        if position.get("trailing") and _beyond(stop, entry) and stop != entry:
            alert = "TRAIL_STOP_HIT"
        elif position.get("t1_done"):
            alert = "BREAKEVEN_STOP_HIT"
        else:
            alert = "STOP_LOSS_HIT"
    elif _beyond(price, position["t3"]):
        alert = "TARGET_3_HIT"
    elif _beyond(price, position["t2"]):
        alert = "TARGET_2_HIT"
    elif _beyond(price, position["t1"]):
        alert = "TARGET_1_HIT"

    # 1b. Trade management. At T1: book 50% and move the stop to entry
    #     (the rest can no longer lose). After T1: trail the stop behind
    #     the last few COMPLETED 5m candles - only ever tightening it.
    if alert in ("TARGET_1_HIT", "TARGET_2_HIT", "TARGET_3_HIT") and not position.get("t1_done"):
        position["t1_done"] = True
        if not _beyond(stop, entry):
            position["stop_loss"] = round(entry, 2)
    if position.get("t1_done") and len(candles) >= 4:
        trail_n = settings.get("trail_lookback_candles", 3)
        done = candles[-(trail_n + 1):-1]
        swing = min(c["low"] for c in done) if is_buy else max(c["high"] for c in done)
        if _beyond(swing, position["stop_loss"]) and swing != position["stop_loss"] and _beyond(price, swing):
            position["stop_loss"] = round(swing, 2)
            position["trailing"] = True

    # 1c. Time stop - no meaningful progress (0.5R) within N minutes.
    if alert is None and not position.get("t1_done") and initial_risk > 0:
        try:
            held_min = (dt.datetime.now() - dt.datetime.fromisoformat(position["entry_time"])).total_seconds() / 60
        except Exception:
            held_min = 0
        if held_min >= settings.get("time_stop_minutes", 30) and \
                position["max_favorable"] < settings.get("time_stop_min_progress_r", 0.5) * initial_risk:
            alert = "TIME_STOP"

    # 1d. Square-off reminder (intraday positions must be closed).
    if alert is None:
        sq = dt.datetime.strptime(settings.get("square_off_time", "15:15"), "%H:%M")
        sq_warn = (sq - dt.timedelta(minutes=5)).time()
        if dt.datetime.now().time() >= sq_warn:
            alert = "SQUARE_OFF_TIME"

    # 2. Candle-pattern reversal - only meaningful while in profit; a
    #    reversal candle while still underwater vs. entry is just noise.
    if alert is None and pnl_pct > 0:
        is_reversal, pattern_name = _is_reversal_candle(candles, direction)
        if is_reversal:
            alert = "REVERSAL_PATTERN"
            position["alert_detail"] = pattern_name

    # 3. Momentum fading (RSI rollover from overbought/oversold) - an
    #    earlier warning than waiting for a full reversal candle.
    if alert is None and pnl_pct > 0 and len(candles) >= 16:
        rsi_vals = [v for v in (Indicators.rsi(candles, settings.get("rsi_period", 14)).get("values") or []) if v is not None]
        if len(rsi_vals) >= 2:
            rsi_now, rsi_prev = rsi_vals[-1], rsi_vals[-2]
            if direction == "BUY" and rsi_prev >= 70 and rsi_now < rsi_prev:
                alert = "MOMENTUM_FADING"
            elif direction == "SELL" and rsi_prev <= 30 and rsi_now > rsi_prev:
                alert = "MOMENTUM_FADING"

    # 4. VWAP lost against the position, while in profit.
    if alert is None and pnl_pct > 0:
        vwap_last = _last(Indicators.vwap(candles))
        if vwap_last:
            if direction == "BUY" and price < vwap_last:
                alert = "VWAP_LOST"
            elif direction == "SELL" and price > vwap_last:
                alert = "VWAP_LOST"

    if alert:
        position["alert"] = alert
        position["alert_message"] = ALERT_MESSAGES.get(alert, alert)
        position["alert_time"] = dt.datetime.now().isoformat(timespec="seconds")
    # NOTE: an existing alert is intentionally left in place (not cleared)
    # once conditions normalize, so it doesn't flap on/off - it stays
    # visible until the user acknowledges it or closes the position.

    # 5. Predictive outlook - reuses the SAME candles already fetched
    #    above, so this costs zero extra API calls.
    try:
        outlook = trade_forecast.analyze_trade_outlook(position, candles, settings)
        position["outlook"] = outlook
    except Exception as exc:
        outlook = None
        position["last_error"] = f"outlook error: {exc}"

    # 6. Push notifications - only for genuinely NEW events this cycle,
    #    and only if this position has notifications turned on.
    if notify_on and trade_forecast.telegram_configured():
        if alert and alert != prev_alert:
            trade_forecast.send_telegram_alert(trade_forecast.format_alert_message(position))
        elif outlook and outlook["verdict"] != prev_verdict and outlook["verdict"] != "NEUTRAL":
            trade_forecast.send_telegram_alert(trade_forecast.format_outlook_flip_message(position, outlook))

    return position


def check_all_open_positions(settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Re-evaluate every OPEN position and persist results. Returns the
    positions that currently have an active alert (for the dashboard to
    surface immediately)."""
    with _LOCK:
        data = _load()
        alerts = []
        for p in data["positions"]:
            if p["status"] != "OPEN":
                continue
            try:
                evaluate_exit(p, settings)
            except Exception as exc:
                p["last_error"] = str(exc)
            if p.get("alert"):
                alerts.append(p)
        _save(data)
        return alerts