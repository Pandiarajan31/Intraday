"""
As-of time-travel clock (backtest Task 2).
==========================================

The live engine reads the wall clock in several places:
  - engine.evaluate_stock():            now = dt.datetime.now()  (session checks)
  - engine.get_completed_candles():     now default = dt.datetime.now(IST)
  - shared.Indicators.vwap / .compute_summary:  "today" = dt.datetime.now(IST).date()
                                        (session-only VWAP filter)

For a historical backtest we must make the engine believe "now" is a chosen
PAST instant, so it only ever sees data at/before that instant (no look-ahead).

This module provides a thread-local override of "now". When an as-of time is
set (via the `as_of` context manager), the engine/indicator helpers read it
through `now_ist()` / `today_ist()`. When nothing is set, they fall back to the
real wall clock, so live behavior is completely unchanged.

Why thread-local + context manager (not a global param threaded everywhere):
rewiring every call site to pass an explicit timestamp would touch a large
amount of verified live code. A scoped override keeps the change tiny and
reversible, and the look-ahead guard (candle truncation) is enforced in the
backtest's DataProvider shim, not left to each call site to remember.
"""
from __future__ import annotations

import datetime as dt
import threading
import zoneinfo
from contextlib import contextmanager
from typing import Optional

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

_state = threading.local()


def _get_as_of() -> Optional[dt.datetime]:
    return getattr(_state, "as_of", None)


def set_as_of(instant: Optional[dt.datetime]) -> None:
    """Set (or clear with None) the thread-local as-of instant. Prefer the
    `as_of` context manager; this is the low-level setter."""
    if instant is not None and instant.tzinfo is None:
        instant = instant.replace(tzinfo=IST)
    _state.as_of = instant


@contextmanager
def as_of(instant: Optional[dt.datetime]):
    """Scope a simulated 'now'. Inside the block, now_ist()/today_ist() return
    `instant`; outside, they return the real wall clock. Restores the previous
    value on exit (supports nesting)."""
    prev = _get_as_of()
    set_as_of(instant)
    try:
        yield
    finally:
        set_as_of(prev)


def is_active() -> bool:
    return _get_as_of() is not None


def now_ist() -> dt.datetime:
    """Simulated now (tz-aware IST) if an as-of is set, else real IST now."""
    aso = _get_as_of()
    if aso is not None:
        return aso.astimezone(IST)
    return dt.datetime.now(zoneinfo.ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)


def now_naive() -> dt.datetime:
    """Simulated now as a NAIVE datetime (IST wall time), matching the live
    engine's `dt.datetime.now()` (which is naive local == IST on the trading
    box). Used by evaluate_stock's session-time comparisons."""
    return now_ist().replace(tzinfo=None)


def today_ist() -> dt.date:
    """Simulated today's date (IST), for the session-only VWAP filter."""
    return now_ist().date()
