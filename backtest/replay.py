"""
Replay harness (backtest Task 3).
=================================

Steps a symbol's cached 5m history bar-by-bar. At each COMPLETED 5m close it
sets the as-of clock to that bar's close time and runs the REAL
engine.evaluate_stock() through the data shim (so the engine sees only data up
to that instant - no look-ahead). When an actionable signal fires
(IMMEDIATE_BUY / IMMEDIATE_SELL) and no trade is currently open for the symbol,
it opens a simulated trade and resolves it with backtest.simulator (which
mirrors positions.py exit rules + round_trip_cost).

Rules enforced:
  - One open trade per symbol at a time (a new signal while in a trade is
    ignored until the current trade closes).
  - Daily trade cap (settings.max_trades_per_day) per symbol per day.
  - Entries only inside the engine's own entry window (the engine already
    returns WAIT outside 09:45-14:30 by default, so we simply act on whatever
    IMMEDIATE signal it emits).
  - All positions are intraday: the simulator force-squares-off at
    square_off_time, so no trade is carried overnight.

Output: a list of SimTrade (the "blotter"), consumed by backtest.metrics.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

import pandas as pd

import engine as engine_module
from backtest import clock, data_shim
from backtest import history as history_module
from backtest.data_shim import HistoryStore
from backtest.simulator import simulate_trade, SimTrade

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

ACTIONABLE = {"IMMEDIATE_BUY": "BUY", "IMMEDIATE_SELL": "SELL"}


def _ts(c) -> dt.datetime:
    t = pd.Timestamp(c["timestamp"])
    if t.tzinfo is None:
        t = t.tz_localize(IST)
    return t.to_pydatetime()


def _completed_5m_closes(candles: List[dict]) -> List[dt.datetime]:
    """Return the CLOSE time of each 5m candle (open + 5 min)."""
    return [_ts(c) + dt.timedelta(minutes=5) for c in candles]


def replay_symbol(symbol: str, settings: Dict[str, Any], store: HistoryStore,
                  regime: Optional[Dict[str, Any]] = None,
                  start_date: Optional[dt.date] = None,
                  end_date: Optional[dt.date] = None,
                  progress: bool = False) -> List[SimTrade]:
    """Replay one symbol over its cached 5m history. Returns the trade blotter."""
    all_5m = store._store.get((symbol.upper(), "5m"), [])
    if not all_5m:
        return []
    square_off = settings.get("square_off_time", "15:15")
    max_trades = settings.get("max_trades_per_day", 4)
    regime = regime or {"regime": "RANGE", "reason": "backtest-neutral"}

    trades: List[SimTrade] = []
    open_until_idx = -1          # index up to which we're inside an open trade
    trades_today: Dict[dt.date, int] = {}

    # Precompute close times aligned to all_5m indices.
    closes = _completed_5m_closes(all_5m)

    with data_shim.use_history(store):
        for i, candle in enumerate(all_5m):
            close_time = closes[i]
            day = close_time.date()
            if start_date and day < start_date:
                continue
            if end_date and day > end_date:
                break
            if i <= open_until_idx:
                continue  # still inside a previously-opened trade
            if trades_today.get(day, 0) >= max_trades:
                continue

            # Fresh engine state each bar so STATE_STORE's duplicate-suppression
            # (IN_TRADE) doesn't mask a genuine fresh IMMEDIATE in replay.
            engine_module.STATE_STORE._states.clear()

            with clock.as_of(close_time):
                rec = engine_module.evaluate_stock({"symbol": symbol}, settings, regime)

            direction = ACTIONABLE.get(rec.get("signal"))
            if not direction:
                continue

            entry_price = rec.get("entry") or rec.get("price")
            if not entry_price:
                continue

            # Build the structural stop plan from completed candles up to & incl.
            # the signal bar (same inputs the engine/positions use).
            import positions as positions_module
            from shared import Indicators
            hist = all_5m[max(0, i - 60):i + 1]
            atr_vals = Indicators.atr(hist, settings.get("atr_period", 14)).get("values") or []
            atr = atr_vals[-1] if atr_vals else None
            plan = positions_module.structural_stop_plan(hist, direction, float(entry_price), atr, settings)
            if not plan:
                continue

            # Risk-based position sizing (same model as positions.py risk_percent).
            pr = settings.get("paper_risk", {}) or {}
            equity = pr.get("account_equity", 100000)
            risk_pct = pr.get("risk_per_trade_percent", 0.5)
            risk_amt = equity * risk_pct / 100
            risk_ps = plan["risk_per_share"]
            qty = int(risk_amt // risk_ps) if risk_ps > 0 else 0
            max_val = equity * pr.get("max_position_value_percent", 20.0) / 100
            qty = min(qty, int(max_val // entry_price)) if entry_price else qty
            if qty < 1:
                continue

            future = all_5m[i + 1:]
            trade = simulate_trade(
                symbol=symbol, direction=direction, entry_time=close_time,
                entry_price=float(entry_price), plan=plan, quantity=qty,
                future_candles=future, settings=settings, square_off_time=square_off,
                setup_id=rec.get("setup_id"), pattern=rec.get("primary_pattern"),
                signal=rec.get("signal"),
            )
            trades.append(trade)
            trades_today[day] = trades_today.get(day, 0) + 1

            # Advance past the bars this trade consumed so we don't re-enter mid-trade.
            open_until_idx = i + max(1, trade.bars_held)

            if progress:
                print(f"  {symbol} {close_time:%Y-%m-%d %H:%M} {direction} -> "
                      f"{trade.exit_reason} R={trade.r_multiple} net={trade.net_pnl}")

    return trades


def replay_universe(symbols: List[str], settings: Dict[str, Any],
                    months: int = 6, intervals=None,
                    start_date: Optional[dt.date] = None,
                    end_date: Optional[dt.date] = None,
                    progress: bool = False) -> List[SimTrade]:
    """Load cache for each symbol and replay it. Returns the combined blotter."""
    store = HistoryStore()
    all_trades: List[SimTrade] = []
    for sym in symbols:
        if progress:
            print(f"[replay] {sym}")
        if not store.load_symbol(sym, intervals=intervals):
            if progress:
                print(f"[replay] {sym}: no cached history - skipping "
                      f"(run `python -m backtest.history` first)")
            continue
        all_trades.extend(
            replay_symbol(sym, settings, store, start_date=start_date,
                          end_date=end_date, progress=progress)
        )
    return all_trades
