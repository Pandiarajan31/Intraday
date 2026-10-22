"""
Trade simulator (backtest Task 3).
==================================

Simulates the lifecycle of ONE trade over subsequent 5m candles using the SAME
exit rules positions.py encodes for live manual trades, so backtested outcomes
match how the system actually tells you to manage a position:

  - Stop / T1 / T2 / T3 from positions.structural_stop_plan (structural swing
    stop + R-multiple targets; T1=1R, T2=2R, T3=3R).
  - At T1: book 50% of the position and move the stop to breakeven (entry).
  - After T1: trail the stop behind the last `trail_lookback_candles` COMPLETED
    candles (min low for BUY / max high for SELL), only ever tightening.
  - Time-stop: before T1, if held >= time_stop_minutes with max-favorable move
    < time_stop_min_progress_r * initial_risk, exit.
  - Square-off: force-close at square_off_time.
  - Costs: positions.round_trip_cost per share (brokerage/STT/slippage proxy),
    applied to the whole position on exit.

Intrabar fill convention (conservative): within a single 5m bar we assume the
STOP is reached before any target if the bar's range spans both - the pessimistic
assumption, so the backtest never flatters itself. Targets/stop are detected
using each bar's high/low; the fill price is the level itself (no favorable
slippage beyond round_trip_cost).

The simulator is pure/deterministic over the candle list it is given - no file
I/O, no wall clock, no threading (unlike positions.evaluate_exit, which is
stateful and lives on positions.json).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

import positions as positions_module

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _ts(c) -> dt.datetime:
    t = pd.Timestamp(c["timestamp"])
    if t.tzinfo is None:
        t = t.tz_localize(IST)
    return t.to_pydatetime()


@dataclass
class SimTrade:
    symbol: str
    direction: str                 # BUY / SELL
    entry_time: dt.datetime
    entry_price: float
    initial_stop: float
    t1: float
    t2: float
    t3: float
    risk_per_share: float
    quantity: int
    # filled in on close:
    exit_time: Optional[dt.datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    r_multiple: Optional[float] = None       # net, after costs, in R units
    net_pnl: Optional[float] = None          # currency, after costs
    gross_pnl: Optional[float] = None
    cost: Optional[float] = None
    bars_held: int = 0
    booked_half: bool = False
    setup_id: Optional[str] = None
    pattern: Optional[str] = None
    signal: Optional[str] = None


def _favorable(direction: str, price: float, entry: float) -> float:
    return (price - entry) if direction == "BUY" else (entry - price)


def simulate_trade(symbol: str, direction: str, entry_time: dt.datetime,
                   entry_price: float, plan: Dict[str, Any], quantity: int,
                   future_candles: List[dict], settings: Dict[str, Any],
                   square_off_time: str = "15:15",
                   setup_id: Optional[str] = None, pattern: Optional[str] = None,
                   signal: Optional[str] = None) -> SimTrade:
    """Walk `future_candles` (completed 5m bars AFTER entry, ascending) and
    resolve the trade's exit. `plan` is a positions.structural_stop_plan dict."""
    is_buy = direction == "BUY"
    sign = 1 if is_buy else -1
    stop = plan["stop_loss"]
    t1, t2, t3 = plan["t1"], plan["t2"], plan["t3"]
    risk = plan["risk_per_share"]
    initial_risk = abs(entry_price - stop)

    trade = SimTrade(
        symbol=symbol, direction=direction, entry_time=entry_time,
        entry_price=round(entry_price, 2), initial_stop=round(stop, 2),
        t1=t1, t2=t2, t3=t3, risk_per_share=risk, quantity=quantity,
        setup_id=setup_id, pattern=pattern, signal=signal,
    )

    sq = dt.datetime.strptime(square_off_time, "%H:%M").time()
    trail_n = settings.get("trail_lookback_candles", 3)
    time_stop_min = settings.get("time_stop_minutes", 30)
    time_stop_prog = settings.get("time_stop_min_progress_r", 0.5)

    cur_stop = stop
    booked_half = False
    max_fav = 0.0
    # Fraction of position still open (1.0, or 0.5 after booking at T1).
    # Realized R is accumulated as parts are booked.
    realized_r_parts: List[tuple] = []  # (fraction, exit_price)

    def _beyond(a, b):
        return a >= b if is_buy else a <= b

    exit_price = None
    exit_reason = None
    bars = 0
    booked_price = None

    for i, c in enumerate(future_candles):
        bars = i + 1
        hi, lo = c["high"], c["low"]
        ct = _ts(c)
        max_fav = max(max_fav, _favorable(direction, hi if is_buy else lo, entry_price))

        # --- Square-off: force exit at/after square-off time (bar close). ---
        if ct.time() >= sq:
            exit_price = c["close"]
            exit_reason = "SQUARE_OFF"
            break

        # --- Stop first (conservative intrabar assumption). ---
        stop_hit = (lo <= cur_stop) if is_buy else (hi >= cur_stop)
        if stop_hit:
            exit_price = cur_stop
            if booked_half:
                exit_reason = "TRAIL_OR_BREAKEVEN_STOP"
            else:
                exit_reason = "STOP_LOSS"
            break

        # --- T1: book half, move stop to breakeven. ---
        if not booked_half and _beyond(hi if is_buy else lo, t1):
            booked_half = True
            booked_price = t1
            realized_r_parts.append((0.5, t1))
            cur_stop = entry_price  # breakeven

        # --- Final target T3 closes the remainder. ---
        if _beyond(hi if is_buy else lo, t3):
            exit_price = t3
            exit_reason = "TARGET_3"
            break

        # --- Trailing stop after T1 (tighten only), behind last N bars. ---
        if booked_half and i >= trail_n:
            window = future_candles[i - trail_n:i]  # completed bars before current
            if window:
                swing = min(w["low"] for w in window) if is_buy else max(w["high"] for w in window)
                if _beyond(swing, cur_stop) and swing != cur_stop:
                    cur_stop = swing

        # --- Time stop before T1. ---
        if not booked_half and initial_risk > 0:
            held_min = (ct - entry_time).total_seconds() / 60
            if held_min >= time_stop_min and max_fav < time_stop_prog * initial_risk:
                exit_price = c["close"]
                exit_reason = "TIME_STOP"
                break
    else:
        # Ran out of candles without an explicit exit: close at last close.
        if future_candles:
            exit_price = future_candles[-1]["close"]
            exit_reason = "DATA_END"

    if exit_price is None:
        # No future candles at all.
        exit_price = entry_price
        exit_reason = "NO_DATA"

    # ------------------------------------------------------------------
    # P&L: if we booked half at T1, that half exits at t1 and the rest at
    # exit_price. Otherwise the whole position exits at exit_price.
    # Costs: round_trip_cost per share on the full quantity (entry+exit).
    # ------------------------------------------------------------------
    cost_per_share = positions_module.round_trip_cost(entry_price, settings)
    if booked_half and booked_price is not None:
        half = quantity / 2.0
        gross = (sign * (booked_price - entry_price) * half
                 + sign * (exit_price - entry_price) * (quantity - half))
    else:
        gross = sign * (exit_price - entry_price) * quantity
    cost = cost_per_share * quantity
    net = gross - cost

    trade.exit_time = future_candles[bars - 1]["timestamp"] if (future_candles and bars) else None
    trade.exit_price = round(exit_price, 2)
    trade.exit_reason = exit_reason
    trade.booked_half = booked_half
    trade.gross_pnl = round(gross, 2)
    trade.cost = round(cost, 2)
    trade.net_pnl = round(net, 2)
    trade.bars_held = bars
    trade.r_multiple = round(net / (initial_risk * quantity), 3) if (initial_risk > 0 and quantity > 0) else None
    return trade
