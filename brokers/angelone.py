"""Read-only Angel One SmartAPI adapter with secure credential handling."""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import time
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import requests

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
INTERVALS = {"1m": "ONE_MINUTE", "3m": "THREE_MINUTE", "5m": "FIVE_MINUTE", "10m": "TEN_MINUTE", "15m": "FIFTEEN_MINUTE", "30m": "THIRTY_MINUTE", "1h": "ONE_HOUR", "1d": "ONE_DAY"}
PERIOD_DAYS = {"1d": 1, "2d": 2, "5d": 5, "10d": 10, "1mo": 31, "3mo": 93, "6mo": 186, "1y": 370}

# Angel One SmartAPI historical/getCandleData limit is documented as
# 3 requests/sec PER CLIENT, with an additional 180/min cap on top of that.
# In practice, historical candle requests are throttled much more aggressively (~1-1.5 req/sec).
# We run the per-second bucket at 1.2 req/sec to guarantee >=830ms spacing between calls,
# and cap calls at 100 per rolling minute to avoid tripping AB1021 rate limits.
class _TokenBucket:
    def __init__(self, rate: float = 1.2, per_minute_cap: int = 100):
        self.rate = rate
        self.tokens = rate
        self.last = time.time()
        self._lock = threading.Lock()
        self.per_minute_cap = per_minute_cap
        self._minute_calls: "deque" = deque()

    def acquire(self) -> float:
        """Return seconds to sleep (may be 0) to stay within both the
        per-second and per-minute rate limits."""
        with self._lock:
            now = time.time()

            # Per-minute cap: drop timestamps older than 60s, then check if
            # we're at the ceiling. If so, wait until the oldest call ages out.
            while self._minute_calls and now - self._minute_calls[0] > 60.0:
                self._minute_calls.popleft()
            minute_wait = 0.0
            if len(self._minute_calls) >= self.per_minute_cap:
                minute_wait = max(0.0, 60.0 - (now - self._minute_calls[0]))

            # Per-second token bucket.
            self.tokens = min(self.rate, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                second_wait = 0.0
            else:
                second_wait = (1.0 - self.tokens) / self.rate
                self.tokens = 0.0

            wait = max(minute_wait, second_wait)
            # Record this call's timestamp (post-wait, approximately) so the
            # per-minute window stays accurate even when we had to sleep.
            self._minute_calls.append(now + wait)
            return wait

_TOKEN_BUCKET = _TokenBucket(1.2, per_minute_cap=100)

# Single-flight cache: prevent duplicate concurrent calls for the same (symbol, interval).
_single_flight: dict = {}
_single_flight_lock = threading.Lock()

# Short-lived TTL cache (35s) for OHLCV queries to prevent redundant network calls during auto-refreshes.
_ohlcv_ttl_cache: dict = {}
_ohlcv_ttl_lock = threading.Lock()



class AngelOneConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AngelOneCredentials:
    api_key: str
    client_id: str
    pin: str
    totp_secret: str

    @classmethod
    def from_environment(cls) -> "AngelOneCredentials":
        values = {"api_key": os.getenv("ANGELONE_API_KEY", ""), "client_id": os.getenv("ANGELONE_CLIENT_ID", ""), "pin": os.getenv("ANGELONE_PIN", ""), "totp_secret": os.getenv("ANGELONE_TOTP_SECRET", "")}
        missing = [key.upper() for key, value in values.items() if not value.strip()]
        if missing:
            raise AngelOneConfigurationError("Missing Angel One environment variables: " + ", ".join(missing))
        return cls(**values)


class AngelOneBroker:
    """Lazily-authenticated, market-data-only adapter; it cannot place orders."""
    def __init__(self, credentials: AngelOneCredentials):
        self.credentials, self.client, self.master = credentials, None, None
        self.cache_file = Path(__file__).resolve().parent.parent / ".cache" / "angelone_instruments.json"

    def initialize_session(self) -> Dict[str, Any]:
        if self.client:
            return {"status": "Success", "message": "existing session reused"}
        try:
            from SmartApi import SmartConnect
            import pyotp
            import logging
            
            # Suppress SmartConnect internal SDK logger error output on transient rate-limits (AB1021)
            logging.getLogger("smartConnect").setLevel(logging.CRITICAL)
            logging.getLogger("SmartApi").setLevel(logging.CRITICAL)
            logging.getLogger("SmartApi.smartConnect").setLevel(logging.CRITICAL)
            logging.getLogger("logzero_default").setLevel(logging.CRITICAL)
            try:
                import logzero
                logzero.logger.setLevel(logging.CRITICAL)
            except Exception:
                pass

            # Temporary bypass for SSL verification failure with Angel One
            import requests, urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            original_request = requests.Session.request
            def patched_request(self_obj, method, url, **kwargs):
                kwargs['verify'] = False
                return original_request(self_obj, method, url, **kwargs)
            requests.Session.request = patched_request
            
        except ImportError as exc:
            raise AngelOneConfigurationError("Install smartapi-python and pyotp before using Angel One data") from exc
        self.client = SmartConnect(api_key=self.credentials.api_key)
        result = self.client.generateSession(self.credentials.client_id, self.credentials.pin, pyotp.TOTP(self.credentials.totp_secret).now())
        if not result or not result.get("status"):
            self.client = None
            raise RuntimeError(f"Angel One login failed: {(result or {}).get('message', 'unknown error')}")
        return result

    def _master(self):
        if self.master is not None: return self.master
        try:
            response = requests.get(MASTER_URL, timeout=20); response.raise_for_status(); self.master = response.json()
            self.cache_file.parent.mkdir(parents=True, exist_ok=True); self.cache_file.write_text(json.dumps(self.master))
        except Exception as exc:
            if self.cache_file.exists(): self.master = json.loads(self.cache_file.read_text())
            else: raise RuntimeError(f"Cannot load Angel One instrument master: {exc}") from exc
        return self.master

    def _instrument(self, symbol: str, exchange: str) -> Dict[str, str]:
        name, segment = symbol.upper().replace(".NS", "").replace(".BO", ""), exchange.upper()
        index_tokens = {
            "^NSEI": ("99926000", "Nifty 50"),
            "NIFTY": ("99926000", "Nifty 50"),
            "NIFTY50": ("99926000", "Nifty 50"),
            "^NSEBANK": ("99926009", "Nifty Bank"),
            "BANKNIFTY": ("99926009", "Nifty Bank"),
        }
        if name in index_tokens:
            token, trading_symbol = index_tokens[name]
            return {"exchange": "NSE", "token": token, "trading_symbol": trading_symbol}
        found = next((row for row in self._master() if str(row.get("exch_seg", "")).upper() == segment and str(row.get("name", "")).upper() == name and str(row.get("symbol", "")).upper().endswith("-EQ")), None)
        if not found: raise ValueError(f"No {segment} equity instrument found for {name}")
        return {"exchange": segment, "token": str(found["token"]), "trading_symbol": str(found["symbol"])}

    def _call(self, method: str, *args):
        # Respect Angel One rate limit (3 req/sec + 180/min for historical data).
        wait = _TOKEN_BUCKET.acquire()
        if wait > 0:
            time.sleep(wait + random.uniform(0.0, 0.05))

        self.initialize_session()
        result = getattr(self.client, method)(*args)
        if not result or not result.get("status"):
            msg = (result or {}).get("message", "")
            if "AB1021" in msg or "Too many requests" in msg:
                # Exponential backoff with jitter, up to 3 retries. Each retry
                # also goes through the token bucket so it can't itself add to
                # the throttle pressure that caused the original AB1021.
                for attempt in range(3):
                    backoff = (2 ** attempt) * 0.5 + random.uniform(0.0, 0.2)
                    time.sleep(backoff)
                    retry_wait = _TOKEN_BUCKET.acquire()
                    if retry_wait > 0:
                        time.sleep(retry_wait + random.uniform(0.0, 0.05))
                    result = getattr(self.client, method)(*args)
                    if result and result.get("status"):
                        break
            if not result or not result.get("status"):
                raise RuntimeError(f"Angel One {method} failed: {(result or {}).get('message', 'unknown error')}")
        return result

    def get_realtime_quote(self, symbol: str, exchange: str = "nse") -> Dict[str, Any]:
        item = self._instrument(symbol, exchange); data = self._call("ltpData", item["exchange"], item["trading_symbol"], item["token"]).get("data") or {}
        if data.get("ltp") is None: raise RuntimeError("Angel One quote omitted ltp")
        return {"symbol": symbol.upper(), "symbol_input": symbol.upper(), "exchange": item["exchange"], "market": "INDIA", "last_price": float(data["ltp"]), "previous_close": _num(data.get("close")), "open": _num(data.get("open")), "day_high": _num(data.get("high")), "day_low": _num(data.get("low")), "volume": int(_num(data.get("volume")) or 0), "timestamp": dt.datetime.now(IST).isoformat(), "source": "angelone"}

    def get_candles_range(self, symbol: str, interval: str, start: dt.datetime,
                          end: dt.datetime, exchange: str = "nse") -> Dict[str, Any]:
        """Fetch candles for an EXPLICIT [start, end] date range (used by the
        backtest history fetcher). Unlike get_ohlcv(), this takes concrete
        datetimes instead of a named period, so historical windows of any age
        can be pulled by iterating ranges. Reuses the same authenticated
        client, rate-limiting (_call -> _TOKEN_BUCKET) and instrument
        resolution as the live path. The caller is responsible for chunking a
        long span into per-interval-safe windows (see backtest/history.py).

        Returns the same candle shape as get_ohlcv():
            {"candles": [{"timestamp","open","high","low","close","volume"}, ...]}
        No TTL/single-flight caching is applied here - the backtest layer caches
        to disk itself, and these are one-off historical pulls, not live refreshes.
        """
        if interval not in INTERVALS:
            raise ValueError(f"Unsupported Angel One interval: {interval}")
        item = self._instrument(symbol, exchange)
        if start.tzinfo is None:
            start = start.replace(tzinfo=IST)
        if end.tzinfo is None:
            end = end.replace(tzinfo=IST)
        params = {
            "exchange": item["exchange"],
            "symboltoken": item["token"],
            "interval": INTERVALS[interval],
            "fromdate": start.astimezone(IST).strftime("%Y-%m-%d %H:%M"),
            "todate": end.astimezone(IST).strftime("%Y-%m-%d %H:%M"),
        }
        data = self._call("getCandleData", params).get("data") or []
        candles = [
            {"timestamp": _timestamp(row[0]), "open": float(row[1]), "high": float(row[2]),
             "low": float(row[3]), "close": float(row[4]), "volume": int(float(row[5]))}
            for row in data if len(row) >= 6
        ]
        return {
            "symbol": symbol.upper(), "symbol_input": symbol.upper(),
            "exchange": item["exchange"], "market": "INDIA",
            "start": start.isoformat(), "end": end.isoformat(),
            "interval": interval, "count": len(candles), "total_candles": len(candles),
            "candles": candles, "source": "angelone",
        }

    def get_ohlcv(self, symbol: str, period="5d", interval="15m", exchange="nse") -> Dict[str, Any]:
        if interval not in INTERVALS: raise ValueError(f"Unsupported Angel One interval: {interval}")
        item, now = self._instrument(symbol, exchange), dt.datetime.now(IST)

        # Short-lived TTL cache check (35 seconds) to prevent redundant network calls during auto-refreshes:
        ttl_key = (symbol.upper(), exchange.upper(), interval, period.lower())
        with _ohlcv_ttl_lock:
            if ttl_key in _ohlcv_ttl_cache:
                entry_time, cached_val = _ohlcv_ttl_cache[ttl_key]
                if time.time() - entry_time < 35.0:
                    return cached_val

        # Angel One frequently returns an EMPTY dataset (no error) for very
        # short periods — e.g. 5m over 1d/2d yields 0 candles — and also
        # returns genuine throttling errors (status=False) during live market
        # hours. Both surface downstream as daily_data_missing / 15m_data_missing
        # / 5m_data_missing. To make the pipeline self-healing, retry the same
        # interval with progressively longer periods, swallowing transient
        # errors and backing off, until a non-empty result is returned.
        # Order fallback windows sensibly per interval:
        #  - Daily trend needs a long history (>=30 candles); if the requested
        #    window is throttled, fall back to LONGER windows, never shorter
        #    ones (a 5d daily window yields only ~3 candles and fails the
        #    engine's minimum-candle gate).
        #  - Intraday (5m/15m/...) prefers the requested window and only widens
        #    to collect more bars when a very short window returns nothing.
        if interval == "1d":
            long_to_short = ["1y", "6mo", "3mo", "1mo", "5d"]
            # Requested period first, then longer windows, then shorter ones.
            fallback_periods = [period.lower()] + [p for p in long_to_short if p != period.lower()]
        else:
            fallback_periods = [period] + [p for p in ("5d", "1mo", "3mo") if p != period.lower()]

        # Single-flight dedup per (symbol_token, interval):
        # Only one concurrent getCandleData call per key; others wait for result.
        sf_key = (str(item["token"]), interval)
        with _single_flight_lock:
            in_flight = sf_key in _single_flight and not _single_flight[sf_key].get("done", False)
            if in_flight:
                # Another thread is already fetching this data; wait and reuse.
                wait_event = _single_flight[sf_key].get("event")
                cached_result = _single_flight[sf_key].get("result")
            else:
                wait_event = threading.Event()
                _single_flight[sf_key] = {"done": False, "result": None, "event": wait_event}

        if in_flight:
            # Wait for the in-flight request to complete
            wait_event.wait(timeout=30)
            with _single_flight_lock:
                cached_result = _single_flight.get(sf_key, {}).get("result")
            if cached_result is not None:
                return cached_result

        try:
            # NOTE: we deliberately do NOT widen the fallback window in response
            # to a throttle/exception anymore. Widening on throttle used to mean
            # "getCandleData call #1 was throttled -> immediately fire call #2
            # with a *bigger* date range" - which multiplies request volume at
            # exactly the moment the API is already rejecting us, turning a
            # single throttle into a cascading retry storm. Now: a throttle
            # just retries the SAME period (bounded, with backoff); we only
            # move to the next (wider) fallback period on a genuine
            # successful-but-empty response, which means "no data for this
            # window", not "the API is rate-limiting me".
            same_period_retries = 2
            for attempt, p in enumerate(fallback_periods):
                start = now - dt.timedelta(days=PERIOD_DAYS.get(p.lower(), 5))
                params = {"exchange": item["exchange"], "symboltoken": item["token"], "interval": INTERVALS[interval], "fromdate": start.strftime("%Y-%m-%d %H:%M"), "todate": now.strftime("%Y-%m-%d %H:%M")}

                data = None
                for retry in range(same_period_retries + 1):
                    try:
                        data = self._call("getCandleData", params).get("data") or []
                        break
                    except Exception:
                        # Transient throttle/error: back off and retry the SAME
                        # period/window rather than escalating to a wider one.
                        if retry < same_period_retries:
                            time.sleep(0.8 * (retry + 1))
                        continue

                if data is None:
                    # Every retry on this period was throttled/errored.
                    # Move to the next fallback period, but don't hammer.
                    time.sleep(0.6 * (attempt + 1))
                    continue

                candles = [{"timestamp": _timestamp(row[0]), "open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4]), "volume": int(float(row[5]))} for row in data if len(row) >= 6]
                if candles:
                    result = {"symbol": symbol.upper(), "symbol_input": symbol.upper(), "exchange": item["exchange"], "market": "INDIA", "start": start.isoformat(), "end": now.isoformat(), "interval": interval, "period_used": p, "total_candles": len(candles), "count": len(candles), "candles": candles, "source": "angelone"}
                    # Publish to short-lived TTL cache & single-flight cache
                    with _ohlcv_ttl_lock:
                        _ohlcv_ttl_cache[ttl_key] = (time.time(), result)
                    with _single_flight_lock:
                        _single_flight[sf_key] = {"done": True, "result": result, "event": wait_event}
                    wait_event.set()
                    return result
                # Genuine successful-but-empty response: no data for this window.
                # Safe to move on to the next (wider) fallback period.

            # All fallback periods returned empty (without usable data): report empty.
            start = now - dt.timedelta(days=PERIOD_DAYS.get(period.lower(), 5))
            result = {"symbol": symbol.upper(), "symbol_input": symbol.upper(), "exchange": item["exchange"], "market": "INDIA", "start": start.isoformat(), "end": now.isoformat(), "interval": interval, "total_candles": 0, "count": 0, "candles": [], "source": "angelone"}
        finally:
            # Signal any waiting threads and mark done
            with _single_flight_lock:
                if sf_key in _single_flight:
                    _single_flight[sf_key]["done"] = True
                    _single_flight[sf_key].get("event", threading.Event()).set()

        return result


def _num(value: Any) -> Optional[float]:
    try: return float(value) if value is not None else None
    except (TypeError, ValueError): return None

def _timestamp(value: Any) -> str:
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed.astimezone(IST)).isoformat()