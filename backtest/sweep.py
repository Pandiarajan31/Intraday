"""
Parameter sweep runner (backtest Task 8).
=========================================

Evidence-based tuning: run the backtest under several config VARIANTS and
compare their metrics side by side, so the must-have/booster split and the key
thresholds are chosen from measured expectancy + trade frequency, not guesses.

Core idea (one-factor-at-a-time + held-out validation):
  - Start from a baseline config (your intraday_config.json settings).
  - Each VARIANT changes ONE thing (gate_mode, booster_penalty, a must-have set,
    min_rvol, min_rr, score threshold, ...).
  - For every variant, replay the SAME cached history and compute metrics.
  - Split the window into TRAIN and VALIDATION halves (by date). A variant that
    looks good on train but falls apart on validation is overfit - the compare
    table shows both so you don't get fooled.

The headline tradeoff for the user's goal (a+b): "trades_per_day" vs
"expectancy_r" / "profit_factor". We want roughly >= 1 actionable call/day WHILE
keeping expectancy positive. This tool surfaces exactly that tradeoff per variant.

IMPORTANT - DATA: this reads the LOCAL cache (backtest/data/), which is populated
by `python -m backtest.history`. With NO cached data the sweep runs but every
variant yields 0 trades (nothing to measure). The actual numeric recommendation
REQUIRES the user to fetch real history first with their Angel One credentials:

    python -m backtest.history --config intraday_config.json --months 6
    python -m backtest.sweep   --config intraday_config.json

Reproducibility: given the same cached data + same variants, metrics are
identical on every run (replay + metrics are deterministic).
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
from typing import Any, Dict, List, Optional, Tuple

from backtest import metrics as metrics_module
from backtest import replay as replay_module


# ---------------------------------------------------------------------------
# Variant definitions
# ---------------------------------------------------------------------------

def default_variants(baseline: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """One-factor-at-a-time variants around the baseline settings. Each returns
    (name, settings_override_applied_to_a_copy_of_baseline). The baseline itself
    is always included as the reference."""
    variants: List[Tuple[str, Dict[str, Any]]] = []

    def mk(name, **overrides):
        s = copy.deepcopy(baseline)
        s.update(overrides)
        variants.append((name, s))

    # Reference.
    mk("baseline")

    # Gate model: the big lever for signal frequency (requirement b).
    mk("tiered", gate_mode="tiered")

    # Booster penalty sweep (only meaningful in tiered mode).
    for bp in (3, 5, 7, 10, 15):
        s = copy.deepcopy(baseline)
        s["gate_mode"] = "tiered"
        s["booster_penalty"] = bp
        variants.append((f"tiered_boost{bp}", s))

    # Score-threshold sweep (lower -> more IMMEDIATE calls, maybe worse quality).
    for th in (55, 60, 65):
        s = copy.deepcopy(baseline)
        s["gate_mode"] = "tiered"
        s["buy_score_threshold"] = th
        s["sell_score_threshold"] = th
        variants.append((f"tiered_score{th}", s))

    # RVOL sweep.
    for rv in (1.2, 1.5, 2.0):
        s = copy.deepcopy(baseline)
        s["gate_mode"] = "tiered"
        s["min_rvol"] = rv
        variants.append((f"tiered_rvol{rv}", s))

    # RR sweep.
    for rr in (1.5, 1.8, 2.0):
        s = copy.deepcopy(baseline)
        s["gate_mode"] = "tiered"
        s["min_rr"] = rr
        variants.append((f"tiered_rr{rr}", s))

    # Must-have-set variants: progressively looser (more boosters).
    s = copy.deepcopy(baseline)
    s["gate_mode"] = "tiered"
    s["hard_gate_must_haves"] = ["session", "liquidity", "atr", "daily_trend", "vwap"]
    variants.append(("tiered_loose_musthaves", s))

    s = copy.deepcopy(baseline)
    s["gate_mode"] = "tiered"
    s["hard_gate_must_haves"] = ["session", "liquidity", "atr", "daily_trend",
                                 "15m_trend", "vwap", "current_rr", "rr", "volume"]
    variants.append(("tiered_tight_musthaves", s))

    return variants


# ---------------------------------------------------------------------------
# Train / validation split
# ---------------------------------------------------------------------------

def _date_span(store, symbols: List[str]) -> Tuple[Optional[dt.date], Optional[dt.date]]:
    import pandas as pd
    lo = hi = None
    for sym in symbols:
        candles = store._store.get((sym.upper(), "5m"), [])
        if not candles:
            continue
        d0 = pd.Timestamp(candles[0]["timestamp"]).date()
        d1 = pd.Timestamp(candles[-1]["timestamp"]).date()
        lo = d0 if lo is None else min(lo, d0)
        hi = d1 if hi is None else max(hi, d1)
    return lo, hi


def split_dates(lo: dt.date, hi: dt.date) -> Tuple[dt.date, dt.date]:
    """Return (train_end, valid_start) splitting [lo, hi] in half by calendar."""
    mid = lo + (hi - lo) / 2
    train_end = mid
    valid_start = mid + dt.timedelta(days=1)
    return train_end, valid_start


# ---------------------------------------------------------------------------
# Running a sweep
# ---------------------------------------------------------------------------

def run_variant(symbols: List[str], settings: Dict[str, Any], store,
                start_date=None, end_date=None) -> Dict[str, Any]:
    trades = []
    for sym in symbols:
        trades.extend(replay_module.replay_symbol(sym, settings, store,
                                                  start_date=start_date, end_date=end_date))
    return metrics_module.compute_metrics(trades)


def run_sweep(symbols: List[str], baseline: Dict[str, Any],
              variants: Optional[List[Tuple[str, Dict[str, Any]]]] = None,
              split: bool = True, store=None) -> Dict[str, Any]:
    """Run every variant over cached history for `symbols`. Returns a dict of
    {variant_name: {"full":metrics, "train":metrics|None, "valid":metrics|None}}.
    """
    from backtest.data_shim import HistoryStore
    if store is None:
        store = HistoryStore()
        missing = []
        for sym in symbols:
            if not store.load_symbol(sym):
                missing.append(sym)
        if missing:
            print(f"[sweep] WARNING: no cached history for {len(missing)} symbol(s): "
                  f"{', '.join(missing[:10])}{'...' if len(missing) > 10 else ''}")
            print("[sweep] Run `python -m backtest.history --config intraday_config.json "
                  "--months 6` first (needs Angel One creds). Variants over these symbols "
                  "will report 0 trades.")

    variants = variants or default_variants(baseline)
    lo, hi = _date_span(store, symbols)
    train_end = valid_start = None
    if split and lo and hi and hi > lo:
        train_end, valid_start = split_dates(lo, hi)

    out: Dict[str, Any] = {"_window": {"start": str(lo), "end": str(hi),
                                       "train_end": str(train_end), "valid_start": str(valid_start)},
                           "variants": {}}
    for name, settings in variants:
        full = run_variant(symbols, settings, store)
        train = valid = None
        if train_end and valid_start:
            train = run_variant(symbols, settings, store, start_date=lo, end_date=train_end)
            valid = run_variant(symbols, settings, store, start_date=valid_start, end_date=hi)
        out["variants"][name] = {"full": full, "train": train, "valid": valid}
    return out


# ---------------------------------------------------------------------------
# Rendering + recommendation
# ---------------------------------------------------------------------------

def _row(name, m):
    if not m:
        return f"  {name:<24} (no data)"
    wr = f"{m['win_rate']*100:.0f}%" if m.get("win_rate") is not None else "-"
    pf = m.get("profit_factor")
    pf = "inf" if pf == float("inf") else (f"{pf:.2f}" if pf is not None else "-")
    exp = m.get("expectancy_r")
    exp = f"{exp:+.3f}" if exp is not None else "-"
    tpd = m.get("trades_per_day")
    tpd = f"{tpd:.2f}" if tpd is not None else "-"
    return f"  {name:<24}{m.get('trades',0):>6}{tpd:>8}{wr:>7}{pf:>7}{exp:>9}"


def render_sweep(result: Dict[str, Any]) -> str:
    lines = []
    w = result.get("_window", {})
    lines.append("=" * 70)
    lines.append("PARAMETER SWEEP")
    lines.append(f"window: {w.get('start')} .. {w.get('end')}  "
                 f"(train<= {w.get('train_end')}, valid>= {w.get('valid_start')})")
    lines.append("=" * 70)
    header = f"  {'variant':<24}{'trades':>6}{'t/day':>8}{'win%':>7}{'PF':>7}{'exp_R':>9}"
    lines.append("[FULL WINDOW]")
    lines.append(header)
    for name, m in result["variants"].items():
        lines.append(_row(name, m["full"]))
    # Validation view (overfit check), only if split ran.
    if any(v.get("valid") for v in result["variants"].values()):
        lines.append("")
        lines.append("[VALIDATION HALF] (a variant strong here is more trustworthy)")
        lines.append(header)
        for name, m in result["variants"].items():
            lines.append(_row(name, m["valid"]))
    lines.append("")
    lines.append(recommend(result))
    return "\n".join(lines)


def recommend(result: Dict[str, Any], min_trades_per_day: float = 1.0) -> str:
    """Pick the variant with the best expectancy among those that (a) have
    positive expectancy on BOTH train and validation (or full if no split) and
    (b) produce >= min_trades_per_day. Purely advisory; prints the rationale."""
    best = None
    for name, m in result["variants"].items():
        full = m["full"]
        valid = m.get("valid")
        train = m.get("train")
        if not full or full.get("expectancy_r") is None:
            continue
        exp_full = full["expectancy_r"]
        tpd = full.get("trades_per_day") or 0
        # Robustness: require positive on validation too when available.
        robust = exp_full > 0
        if valid and valid.get("expectancy_r") is not None:
            robust = robust and valid["expectancy_r"] > 0
        if train and train.get("expectancy_r") is not None:
            robust = robust and train["expectancy_r"] > 0
        if robust and tpd >= min_trades_per_day:
            if best is None or exp_full > best[1]:
                best = (name, exp_full, tpd)

    if best is None:
        return ("RECOMMENDATION: no variant met BOTH positive (robust) expectancy "
                f"AND >= {min_trades_per_day} trades/day on this data. Either the "
                "edge isn't there on this window, or more history is needed. Do NOT "
                "loosen gates further just to force trades - that destroys expectancy. "
                "Re-run with more months of cached data before deciding.")
    name, exp, tpd = best
    return (f"RECOMMENDATION: '{name}' - best robust expectancy ({exp:+.3f}R/trade) "
            f"while producing {tpd:.2f} trades/day (>= {min_trades_per_day}). "
            "Apply its settings to intraday_config.json, then forward-test on paper "
            "before trusting it live. Backtest != live; treat this as a prior, not a promise.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Sweep engine config variants over cached backtest history.")
    ap.add_argument("--config", required=True, help="intraday_config.json (baseline settings + universe)")
    ap.add_argument("--symbol", action="append", help="Override universe with explicit symbol(s)")
    ap.add_argument("--no-split", action="store_true", help="Disable train/validation split")
    ap.add_argument("--min-trades-per-day", type=float, default=1.0)
    ap.add_argument("--out", help="Write the full sweep result JSON here")
    args = ap.parse_args(argv)

    with open(args.config) as f:
        cfg = json.load(f)
    baseline = cfg.get("settings", {})
    try:
        from engine import normalize_settings
        baseline = normalize_settings(baseline)
    except Exception:
        pass
    symbols = args.symbol or [s["symbol"] for s in cfg.get("stocks", []) if s.get("symbol")]
    if not symbols:
        ap.error("no symbols (config has no stocks and no --symbol)")

    result = run_sweep(symbols, baseline, split=not args.no_split)
    print(render_sweep(result))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"\n[sweep] wrote {args.out}")
    return result


if __name__ == "__main__":
    main()
