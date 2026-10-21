"""
trade_forecast.py
==================
Adds two things on top of positions.py's existing tracking:

1. analyze_trade_outlook() - a rule-based "which way is this likely headed
   next" read on an OPEN position: TOWARD_TARGET, TOWARD_STOPLOSS, or
   NEUTRAL, with a 0-100 confidence score and the specific reasons behind
   it. This is a heuristic scorer built from candle quality, momentum,
   and volume - NOT a statistically validated/backtested probability
   model. Treat "confidence" as "how many technical signals agree right
   now", not "% chance of X happening". It's a decision AID, not a
   guarantee - a human still makes the call.

2. send_telegram_alert() - genuinely instant push notifications (unlike
   the browser beep/notification, this reaches your phone even with the
   dashboard tab closed) via a Telegram bot, used for real stop-loss/
   target hits AND meaningful outlook flips (e.g. a BUY position that was
   TOWARD_TARGET flips to TOWARD_STOPLOSS).

Setup for Telegram (optional - everything else works without it):
  1. Message @BotFather on Telegram -> /newbot -> get a bot token.
  2. Message your new bot once (anything), then open:
     https://api.telegram.org/bot<TOKEN>/getUpdates
     and read your chat id from the JSON response.
  3. Set two environment variables before starting the dashboard:
     TELEGRAM_BOT_TOKEN=<token>
     TELEGRAM_CHAT_ID=<chat id>
  If these aren't set, send_telegram_alert() silently no-ops - nothing
  else breaks.
"""
from __future__ import annotations

import os
import urllib.request
import urllib.parse
import json
from typing import Any, Dict, List, Optional

from shared import Indicators

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


# ---------------------------------------------------------------------------
# Outlook prediction
# ---------------------------------------------------------------------------

def _pct(a: float, b: float) -> float:
    """a as a percentage distance from b."""
    return abs(a - b) / b * 100 if b else 0.0


def _candle_quality_score(candles: List[dict], direction: str) -> Dict[str, Any]:
    """Looks at the last few completed 5m candles and scores how much
    they support CONTINUATION in `direction` vs AGAINST it.
    Returns a score from -100 (strongly against) to +100 (strongly for),
    plus the specific observations that drove it."""
    if len(candles) < 4:
        return {"score": 0, "notes": ["Not enough recent candles yet"]}

    recent = candles[-4:]
    score = 0
    notes = []

    # 1. Consecutive candles closing in your favor
    favorable_closes = 0
    for c in recent:
        is_green = c["close"] > c["open"]
        if (direction == "BUY" and is_green) or (direction == "SELL" and not is_green):
            favorable_closes += 1
    if favorable_closes >= 3:
        score += 25
        notes.append(f"{favorable_closes}/4 recent candles closed in your favor")
    elif favorable_closes <= 1:
        score -= 25
        notes.append(f"Only {favorable_closes}/4 recent candles closed in your favor")

    # 2. Body strength of the LAST candle (decisive vs indecisive)
    last = recent[-1]
    body = abs(last["close"] - last["open"])
    rng = max(last["high"] - last["low"], 1e-9)
    body_ratio = body / rng
    last_is_green = last["close"] > last["open"]
    favorable_last = (direction == "BUY" and last_is_green) or (direction == "SELL" and not last_is_green)
    if body_ratio > 0.6:
        if favorable_last:
            score += 20
            notes.append("Latest candle has a strong, decisive body in your favor")
        else:
            score -= 20
            notes.append("Latest candle has a strong, decisive body AGAINST your position")
    elif body_ratio < 0.25:
        notes.append("Latest candle is indecisive (small body / long wicks) - low conviction either way")

    # 3. Volume trend across the recent candles - rising volume on
    #    favorable candles reinforces the move; rising volume on
    #    unfavorable candles is a warning.
    vols = [c.get("volume", 0) for c in recent]
    if vols[-1] > (sum(vols[:-1]) / max(len(vols) - 1, 1)) * 1.2:
        if favorable_last:
            score += 15
            notes.append("Volume picking up on a favorable candle")
        else:
            score -= 15
            notes.append("Volume picking up on an UNfavorable candle")

    return {"score": max(-100, min(100, score)), "notes": notes}


def _momentum_score(candles: List[dict], direction: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    """RSI + short EMA slope, scored the same -100..+100 way."""
    score = 0
    notes = []
    if len(candles) < 16:
        return {"score": 0, "notes": ["Not enough history yet for momentum read"]}

    rsi_vals = [v for v in (Indicators.rsi(candles, settings.get("rsi_period", 14)).get("values") or []) if v is not None]
    if len(rsi_vals) >= 2:
        rsi_now, rsi_prev = rsi_vals[-1], rsi_vals[-2]
        rising = rsi_now > rsi_prev
        falling = rsi_now < rsi_prev
        if direction == "BUY":
            if rsi_now >= 70 and falling:
                score -= 20
                notes.append(f"RSI overbought and turning down ({rsi_prev:.0f} -> {rsi_now:.0f})")
            elif 40 <= rsi_now <= 65 and rising:
                score += 20
                notes.append(f"RSI rising with room left to run ({rsi_prev:.0f} -> {rsi_now:.0f})")
            elif rsi_now <= 35:
                score -= 15
                notes.append(f"RSI weak ({rsi_now:.0f}) - momentum fading")
        else:  # SELL
            if rsi_now <= 30 and rising:
                score -= 20
                notes.append(f"RSI oversold and turning up ({rsi_prev:.0f} -> {rsi_now:.0f})")
            elif 35 <= rsi_now <= 60 and falling:
                score += 20
                notes.append(f"RSI falling with room left to run ({rsi_prev:.0f} -> {rsi_now:.0f})")
            elif rsi_now >= 65:
                score -= 15
                notes.append(f"RSI strong against you ({rsi_now:.0f})")

    vwap_last = None
    vwap_obj = Indicators.vwap(candles)
    vwap_vals = vwap_obj.get("values") or []
    if vwap_vals:
        vwap_last = vwap_vals[-1]
    price = candles[-1]["close"]
    if vwap_last:
        dist_pct = _pct(price, vwap_last)
        away_favorably = (direction == "BUY" and price > vwap_last) or (direction == "SELL" and price < vwap_last)
        if away_favorably and dist_pct > 0.15:
            score += 15
            notes.append(f"Price extending away from VWAP in your favor ({dist_pct:.2f}%)")
        elif not away_favorably:
            score -= 20
            notes.append("Price is on the wrong side of VWAP")

    return {"score": max(-100, min(100, score)), "notes": notes}


def analyze_trade_outlook(position: Dict[str, Any], candles: List[dict], settings: Dict[str, Any]) -> Dict[str, Any]:
    """Combine candle-quality + momentum into one outlook for an OPEN
    position. Does NOT fetch data itself - pass in the same 5m candles
    you already fetched for evaluate_exit(), so this doesn't cost an
    extra API call."""
    direction = position["direction"]
    entry = position["entry_price"]
    stop = position["stop_loss"]
    t1 = position["t1"]
    price = candles[-1]["close"] if candles else position.get("last_price", entry)

    dist_to_target_pct = _pct(price, t1)
    dist_to_stop_pct = _pct(price, stop)

    cq = _candle_quality_score(candles, direction)
    mom = _momentum_score(candles, direction, settings)
    combined = cq["score"] * 0.55 + mom["score"] * 0.45

    # A large, real bias toward whichever level is objectively much
    # closer right now (e.g. price already sitting right on the stop)
    # matters more than pattern reading - blend it in, don't let a
    # decent-looking candle mask "you are one tick from your stop".
    if dist_to_stop_pct > 0 and dist_to_target_pct > 0:
        proximity_bias = (dist_to_stop_pct - dist_to_target_pct) / (dist_to_stop_pct + dist_to_target_pct) * 100
        combined = combined * 0.7 + proximity_bias * 0.3

    if combined >= 20:
        verdict = "TOWARD_TARGET"
    elif combined <= -20:
        verdict = "TOWARD_STOPLOSS"
    else:
        verdict = "NEUTRAL"

    confidence = min(100, round(abs(combined)))
    reasons = cq["notes"] + mom["notes"]

    return {
        "verdict": verdict,
        "confidence": confidence,
        "reasons": reasons,
        "distance_to_target_percent": round(dist_to_target_pct, 2),
        "distance_to_stop_percent": round(dist_to_stop_pct, 2),
        "raw_score": round(combined, 1),
    }


# ---------------------------------------------------------------------------
# Telegram notifications (optional - no-ops if not configured)
# ---------------------------------------------------------------------------

def telegram_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def send_telegram_alert(text: str) -> bool:
    """Fire-and-forget push notification to your phone via Telegram.
    Returns False silently (no exception) if not configured or the
    request fails - this must never crash the position-checker loop."""
    if not telegram_configured():
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def format_alert_message(position: Dict[str, Any]) -> str:
    sym = position["symbol"]
    direction = position["direction"]
    alert = position.get("alert")
    msg = position.get("alert_message", "")
    price = position.get("last_price")
    pnl = position.get("unrealized_pnl_percent")
    return (
        f"<b>{sym}</b> ({direction})\n"
        f"{alert.replace('_', ' ') if alert else ''}: {msg}\n"
        f"Price: {price} | P&amp;L: {pnl}%"
    )


def format_outlook_flip_message(position: Dict[str, Any], outlook: Dict[str, Any]) -> str:
    sym = position["symbol"]
    direction = position["direction"]
    verdict = outlook["verdict"].replace("_", " ")
    return (
        f"<b>{sym}</b> ({direction}) outlook update\n"
        f"Now leaning: {verdict} ({outlook['confidence']}% confidence)\n"
        f"Distance to target: {outlook['distance_to_target_percent']}% | "
        f"Distance to stop: {outlook['distance_to_stop_percent']}%"
    )
