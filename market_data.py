"""Bounded shared quote state and deterministic local five-minute bars."""
from __future__ import annotations
import datetime as dt
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

class TickCandleBuilder:
    def __init__(self, interval_minutes=5, max_bars=600): self.interval, self.bars, self.last_volume = interval_minutes, defaultdict(lambda: deque(maxlen=max_bars)), {}
    def add(self, symbol: str, price: float, timestamp: str, cumulative_volume: Optional[int] = None):
        at = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00")); at = at.replace(tzinfo=IST) if at.tzinfo is None else at.astimezone(IST); bucket = at.replace(minute=at.minute // self.interval * self.interval, second=0, microsecond=0).isoformat(); key = symbol.upper(); volume = 0
        if cumulative_volume is not None: volume = max(0, int(cumulative_volume) - self.last_volume.get(key, int(cumulative_volume))); self.last_volume[key] = int(cumulative_volume)
        if self.bars[key] and self.bars[key][-1]["timestamp"] == bucket:
            bar = self.bars[key][-1]; bar["high"], bar["low"], bar["close"], bar["volume"] = max(bar["high"], price), min(bar["low"], price), price, bar["volume"] + volume
        else: self.bars[key].append({"timestamp": bucket, "open": price, "high": price, "low": price, "close": price, "volume": volume})
    def candles(self, symbol: str, closed_only=False) -> List[Dict[str, Any]]:
        bars = list(self.bars[symbol.upper()])
        if closed_only and bars and bars[-1]["timestamp"][:16] == dt.datetime.now(IST).replace(minute=dt.datetime.now(IST).minute // self.interval * self.interval, second=0, microsecond=0).isoformat()[:16]: bars.pop()
        return bars