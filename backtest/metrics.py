"""
Backtest metrics (Task 4).
==========================

Pure aggregation over a blotter (List[SimTrade] from backtest.replay) into the
numbers that tell you whether the strategy has an edge:

  - trades, wins, losses, win_rate
  - avg_win_r / avg_loss_r (net R, after costs)
  - profit_factor  = gross R won / gross R lost
  - expectancy_r   = mean net R per trade (THE headline edge number)
  - total_r, total_net_pnl
  - max_drawdown_r (peak-to-trough on the cumulative-R equity curve)
  - trades_per_day
  - breakdowns per pattern, per signal, per exit_reason

Plus `blocker_frequencies()`: reads the engine's decision_log JSONL and counts
how often each hard-gate condition was a blocker - the input to Task 8's
decision on which gates to keep as must-haves vs demote to boosters.

All functions are pure and deterministic so they can be unit-tested against a
hand-built blotter with known expected metrics.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional


def _trade_day(t) -> Optional[str]:
    et = getattr(t, "entry_time", None)
    if et is None:
        return None
    if isinstance(et, dt.datetime):
        return et.date().isoformat()
    try:
        return str(et)[:10]
    except Exception:
        return None


def compute_metrics(trades: List[Any]) -> Dict[str, Any]:
    """Core aggregate metrics over a list of SimTrade (or any object with
    .r_multiple and .net_pnl). Trades with r_multiple is None are ignored for
    R stats but still counted in `skipped`."""
    rated = [t for t in trades if getattr(t, "r_multiple", None) is not None]
    skipped = len(trades) - len(rated)
    n = len(rated)
    if n == 0:
        return {"trades": 0, "skipped": skipped, "win_rate": None,
                "profit_factor": None, "expectancy_r": None, "total_r": 0.0,
                "total_net_pnl": 0.0, "max_drawdown_r": 0.0, "wins": 0, "losses": 0,
                "avg_win_r": None, "avg_loss_r": None, "trades_per_day": None}

    rs = [t.r_multiple for t in rated]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_won = sum(wins)
    gross_lost = -sum(losses)  # positive magnitude
    total_r = sum(rs)
    total_net = sum((getattr(t, "net_pnl", 0) or 0) for t in rated)

    # Max drawdown on the cumulative-R equity curve.
    peak = 0.0
    cum = 0.0
    max_dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    days = {d for d in (_trade_day(t) for t in rated) if d}
    trades_per_day = round(n / len(days), 2) if days else None

    return {
        "trades": n,
        "skipped": skipped,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n, 4),
        "avg_win_r": round(gross_won / len(wins), 3) if wins else None,
        "avg_loss_r": round(sum(losses) / len(losses), 3) if losses else None,
        "profit_factor": (round(gross_won / gross_lost, 3) if gross_lost > 0
                          else (float("inf") if gross_won > 0 else 0.0)),
        "expectancy_r": round(total_r / n, 4),
        "total_r": round(total_r, 3),
        "total_net_pnl": round(total_net, 2),
        "max_drawdown_r": round(max_dd, 3),
        "trading_days": len(days),
        "trades_per_day": trades_per_day,
    }


def _breakdown(trades: List[Any], key_attr: str) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[Any]] = defaultdict(list)
    for t in trades:
        key = getattr(t, key_attr, None) or "UNKNOWN"
        groups[str(key)].append(t)
    return {k: compute_metrics(v) for k, v in sorted(groups.items())}


def breakdown_by_pattern(trades: List[Any]) -> Dict[str, Dict[str, Any]]:
    return _breakdown(trades, "pattern")


def breakdown_by_signal(trades: List[Any]) -> Dict[str, Dict[str, Any]]:
    return _breakdown(trades, "signal")


def breakdown_by_exit_reason(trades: List[Any]) -> Dict[str, Dict[str, Any]]:
    return _breakdown(trades, "exit_reason")


def full_report(trades: List[Any]) -> Dict[str, Any]:
    return {
        "overall": compute_metrics(trades),
        "by_pattern": breakdown_by_pattern(trades),
        "by_signal": breakdown_by_signal(trades),
        "by_exit_reason": breakdown_by_exit_reason(trades),
    }


# ---------------------------------------------------------------------------
# Decision-log blocker frequencies (Task 8 input)
# ---------------------------------------------------------------------------

def blocker_frequencies(log_dir: str = "logs/decisions",
                        files: Optional[List[str]] = None) -> Dict[str, Any]:
    """Count how often each hard-gate condition appears as a blocker across the
    engine's decision_log JSONL files.

    Returns:
      {"records": N,
       "primary_blocker": {gate: count, ...},         # the #1 reason only
       "any_failed": {gate: count, ...},              # appears in any failed list
       "sole_blocker": {gate: count, ...}}            # the ONLY failing gate
    The sole-blocker count is the key signal: a gate that is frequently the ONLY
    thing stopping a trade is the strongest candidate to demote to a booster.
    """
    paths = files if files is not None else sorted(glob.glob(os.path.join(log_dir, "*.jsonl")))
    primary = Counter()
    any_failed = Counter()
    sole = Counter()
    records = 0
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    records += 1
                    pb = rec.get("primary_blocker")
                    if pb:
                        primary[pb] += 1
                    # Combine both direction failed-lists; a gate failing on
                    # either side counts as that gate being a blocker.
                    failed = set()
                    for fld in ("buy_failed_conditions", "sell_failed_conditions"):
                        for g in (rec.get(fld) or []):
                            failed.add(g)
                    for g in failed:
                        any_failed[g] += 1
                    if len(failed) == 1:
                        sole[next(iter(failed))] += 1
        except OSError:
            continue
    return {
        "records": records,
        "primary_blocker": dict(primary.most_common()),
        "any_failed": dict(any_failed.most_common()),
        "sole_blocker": dict(sole.most_common()),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _fmt(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        if v == float("inf"):
            return "inf"
        return f"{v:.3f}"
    return str(v)


def render_report(report: Dict[str, Any]) -> str:
    """Human-readable multi-section text table."""
    lines: List[str] = []
    o = report["overall"]
    lines.append("=" * 60)
    lines.append("BACKTEST METRICS - OVERALL")
    lines.append("=" * 60)
    order = ["trades", "wins", "losses", "win_rate", "avg_win_r", "avg_loss_r",
             "profit_factor", "expectancy_r", "total_r", "total_net_pnl",
             "max_drawdown_r", "trading_days", "trades_per_day", "skipped"]
    for k in order:
        if k in o:
            lines.append(f"  {k:<16}: {_fmt(o[k])}")

    def _section(title, bd):
        lines.append("")
        lines.append(f"--- {title} ---")
        lines.append(f"  {'group':<18}{'trades':>7}{'win%':>8}{'PF':>8}{'exp_R':>9}{'totR':>9}")
        for grp, m in bd.items():
            wr = f"{m['win_rate']*100:.0f}%" if m.get("win_rate") is not None else "-"
            lines.append(f"  {grp[:18]:<18}{_fmt(m['trades']):>7}{wr:>8}"
                         f"{_fmt(m['profit_factor']):>8}{_fmt(m['expectancy_r']):>9}{_fmt(m['total_r']):>9}")

    _section("BY SIGNAL", report["by_signal"])
    _section("BY PATTERN", report["by_pattern"])
    _section("BY EXIT REASON", report["by_exit_reason"])
    return "\n".join(lines)


def trades_to_rows(trades: List[Any]) -> List[Dict[str, Any]]:
    """Flatten SimTrade objects into dict rows for CSV/JSON export."""
    rows = []
    for t in trades:
        rows.append({
            "symbol": getattr(t, "symbol", None),
            "direction": getattr(t, "direction", None),
            "signal": getattr(t, "signal", None),
            "pattern": getattr(t, "pattern", None),
            "entry_time": str(getattr(t, "entry_time", "")),
            "entry_price": getattr(t, "entry_price", None),
            "exit_time": str(getattr(t, "exit_time", "")),
            "exit_price": getattr(t, "exit_price", None),
            "exit_reason": getattr(t, "exit_reason", None),
            "quantity": getattr(t, "quantity", None),
            "r_multiple": getattr(t, "r_multiple", None),
            "gross_pnl": getattr(t, "gross_pnl", None),
            "cost": getattr(t, "cost", None),
            "net_pnl": getattr(t, "net_pnl", None),
            "bars_held": getattr(t, "bars_held", None),
        })
    return rows


def write_csv(trades: List[Any], path: str) -> None:
    import csv
    rows = trades_to_rows(trades)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        if not rows:
            f.write("")
            return
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def write_json(report: Dict[str, Any], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
