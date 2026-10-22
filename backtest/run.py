"""
Backtest runner CLI (Task 4 deliverable).
=========================================

Chains the whole measurement loop into one command:
    config -> (cached history) -> replay -> metrics -> report

Usage:
    # Backtest the watchlist in intraday_config.json over cached history:
    python -m backtest.run --config intraday_config.json

    # Single symbol, write a trade blotter CSV + metrics JSON:
    python -m backtest.run --symbol RELIANCE --out-dir backtest/out

    # Restrict the window:
    python -m backtest.run --config intraday_config.json --start 2026-04-01 --end 2026-06-30

Prereq: run `python -m backtest.history --config intraday_config.json --months 6`
first to populate backtest/data/. The runner reads from that cache (it does NOT
fetch) so it is fully offline/deterministic.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from typing import List, Optional

from backtest import metrics as metrics_module
from backtest import replay as replay_module


def _load_config(path: str):
    with open(path) as f:
        return json.load(f)


def _parse_date(s: Optional[str]) -> Optional[dt.date]:
    return dt.datetime.strptime(s, "%Y-%m-%d").date() if s else None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the intraday engine backtest over cached history.")
    ap.add_argument("--config", help="intraday_config.json (universe + settings)")
    ap.add_argument("--symbol", action="append", help="Symbol to backtest (repeatable). Overrides config stocks if given.")
    ap.add_argument("--start", help="Start date YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", help="End date YYYY-MM-DD (inclusive)")
    ap.add_argument("--out-dir", help="If set, write blotter.csv + report.json here")
    ap.add_argument("--progress", action="store_true", help="Print each trade as it's simulated")
    args = ap.parse_args(argv)

    if not args.config and not args.symbol:
        ap.error("provide --config and/or --symbol")

    settings = {}
    symbols: List[str] = []
    if args.config:
        cfg = _load_config(args.config)
        settings = cfg.get("settings", {})
        symbols = [s["symbol"] for s in cfg.get("stocks", []) if s.get("symbol")]
    if args.symbol:
        symbols = args.symbol  # explicit symbols override config universe

    if not symbols:
        ap.error("no symbols to backtest (config has no stocks and no --symbol given)")

    # Normalize settings exactly as the engine does, so the backtest uses the
    # same effective thresholds the live engine would.
    try:
        from engine import normalize_settings
        settings = normalize_settings(settings)
    except Exception:
        pass

    print(f"[backtest] symbols={len(symbols)} window={args.start or 'all'}..{args.end or 'all'}")
    trades = replay_module.replay_universe(
        symbols, settings,
        start_date=_parse_date(args.start), end_date=_parse_date(args.end),
        progress=args.progress,
    )

    report = metrics_module.full_report(trades)
    # Attach decision-log blocker frequencies if logs exist.
    try:
        report["blockers"] = metrics_module.blocker_frequencies()
    except Exception:
        report["blockers"] = {}

    print("\n" + metrics_module.render_report(report))

    blk = report.get("blockers", {})
    if blk.get("records"):
        print("\n--- DECISION-LOG BLOCKERS (sole-blocker counts) ---")
        for gate, cnt in list(blk.get("sole_blocker", {}).items())[:15]:
            print(f"  {gate:<22}{cnt}")

    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        metrics_module.write_csv(trades, os.path.join(args.out_dir, "blotter.csv"))
        metrics_module.write_json(report, os.path.join(args.out_dir, "report.json"))
        print(f"\n[backtest] wrote {args.out_dir}/blotter.csv and report.json")

    return report


if __name__ == "__main__":
    main()
