"""Persistent decision logging (review doc #18).

Writes one JSON line per engine.evaluate_stock() call to
logs/decisions/<YYYY-MM-DD>.jsonl so every signal/rejection can be
reconstructed later. Fully defensive: any failure to log is swallowed into a
logger.warning so evaluation is never blocked by the logging sink.

Off by default via settings["decision_log"]; enable in intraday_config.json.
"""
import datetime as dt
import json
import logging
import os

logger = logging.getLogger(__name__)


class DecisionLogger:
    """Append-only JSONL logger for per-symbol evaluation records."""

    def __init__(self, base_dir: str = "logs/decisions", enabled: bool = True):
        self.base_dir = base_dir
        self.enabled = enabled
        self._fh = None
        self._path = None
        self._today = None

    def _ensure_file(self):
        today = dt.date.today().isoformat()
        if not self.enabled:
            return False
        if self._today == today and self._fh is not None:
            return True
        # New day (or first open): rotate.
        try:
            os.makedirs(self.base_dir, exist_ok=True)
        except OSError as e:
            logger.warning("decision_log mkdir failed: %s", e)
            self.enabled = False
            return False
        path = os.path.join(self.base_dir, f"{today}.jsonl")
        try:
            self._fh = open(path, "a", encoding="utf-8")
            self._path = path
            self._today = today
            return True
        except OSError as e:
            logger.warning("decision_log open failed: %s", e)
            self._fh = None
            self.enabled = False
            return False

    def log(self, record: dict):
        if not self.enabled:
            return
        if not self._ensure_file():
            return
        try:
            entry = {
                "ts": dt.datetime.now().isoformat(timespec="seconds"),
                "symbol": record.get("symbol"),
                "setup_id": record.get("setup_id"),
                "signal": record.get("signal"),
                "reason": record.get("reason"),
                "primary_blocker": record.get("primary_blocker"),
                "secondary_blockers": record.get("secondary_blockers"),
                "price": record.get("price"),
                "quote_ts": record.get("quote_timestamp"),
                "quote_age_s": record.get("data_age_seconds"),
                "daily_trend": record.get("daily_trend"),
                "m15_trend": record.get("m15_trend"),
                "m5_structure": record.get("m5_structure"),
                "vwap": record.get("vwap"),
                "rvol": record.get("rvol"),
                "atr_5m": record.get("atr_5m"),
                "risk_reward": record.get("risk_reward"),
                "liquidity_status": record.get("liquidity_status"),
                "market_regime": record.get("market_regime"),
                "market_regime_raw": record.get("market_regime_raw"),
                "market_regime_failed_open": record.get("market_regime_failed_open"),
                "overextended_buy": record.get("overextended_buy"),
                "overextended_sell": record.get("overextended_sell"),
                "reversal_risk_buy": record.get("reversal_risk_buy"),
                "reversal_risk_sell": record.get("reversal_risk_sell"),
                "reversal_risk_reasons_buy": record.get("reversal_risk_reasons_buy"),
                "reversal_risk_reasons_sell": record.get("reversal_risk_reasons_sell"),
                "early_reversal_signal": record.get("early_reversal_signal"),
                "early_reversal_confidence": record.get("early_reversal_confidence"),
                "early_reversal_reasons": record.get("early_reversal_reasons"),
                "exhaustion_buy": record.get("exhaustion_buy"),
                "exhaustion_sell": record.get("exhaustion_sell"),
                "exhaustion_reasons_buy": record.get("exhaustion_reasons_buy"),
                "exhaustion_reasons_sell": record.get("exhaustion_reasons_sell"),
                "vwap_stretch_atr": record.get("vwap_stretch_atr"),
                "day_range_used": record.get("day_range_used"),
                "entry_zone": record.get("entry_zone"),
                "entry_zone_direction": record.get("entry_zone_direction"),
                "entry_zone_buy": record.get("entry_zone_buy"),
                "entry_zone_sell": record.get("entry_zone_sell"),
                "index_name": record.get("index_name"),
                "index_direction": record.get("index_direction"),
                "rs_vs_index_today": record.get("rs_vs_index_today"),
                "trading_day_status": record.get("trading_day_status"),
                "entry_distance_buy_r": record.get("entry_distance_buy_r"),
                "entry_distance_sell_r": record.get("entry_distance_sell_r"),
                "entry_too_far_buy": record.get("entry_too_far_buy"),
                "entry_too_far_sell": record.get("entry_too_far_sell"),
                "current_rr_buy": record.get("current_rr_buy"),
                "current_rr_sell": record.get("current_rr_sell"),
                "obstacle_before_target": record.get("obstacle_before_target"),
                "obstacle_level": record.get("obstacle_level"),
                "entry_timing_verdict": record.get("entry_timing_verdict"),
                "entry_timing_reasons": record.get("entry_timing_reasons"),
                "breakout": record.get("breakout"),
                "breakdown": record.get("breakdown"),
                "extension_overextended": record.get("overextended"),
                "buy_score": record.get("buy_score"),
                "sell_score": record.get("sell_score"),
                "buy_hard_gate": record.get("buy_hard_gate"),
                "sell_hard_gate": record.get("sell_hard_gate"),
                "buy_failed_conditions": record.get("buy_failed_conditions"),
                "sell_failed_conditions": record.get("sell_failed_conditions"),
                "distance_to_trigger": record.get("distance_to_trigger"),
                "trigger_level": record.get("trigger_level"),
                "primary_pattern": record.get("primary_pattern"),
                "pattern_agreement_count": record.get("pattern_agreement_count"),
                "agreement_level": record.get("agreement_level"),
            }
            self._fh.write(json.dumps(entry, default=str) + "\n")
            self._fh.flush()
        except Exception as e:  # never break evaluation
            logger.warning("decision_log write failed: %s", e)


# Module-level default logger (enabled flag set from settings at call site).
_default = DecisionLogger()