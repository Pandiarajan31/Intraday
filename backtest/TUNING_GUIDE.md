# Backtest & Tuning Guide

This is the measurement loop that turns "I think this is profitable" into
"the data says this is profitable (or not)". It also drives the decision on
how aggressively to relax the gates so you get roughly one actionable call a
day without forcing bad trades.

**Honest caveat up front:** a REST 5-minute backtest with modeled charges is an
approximation, not a promise of live results. Slippage, partial fills, and news
gaps are modeled conservatively or not at all. Treat every number here as a
*prior* to forward-test on paper, not a guarantee.

---

## The pieces

| Module | What it does |
|--------|--------------|
| `backtest/history.py` | Fetches + caches historical candles from Angel One (`backtest/data/<SYM>/<interval>.csv`). |
| `backtest/clock.py` + `backtest/data_shim.py` | "As-of" time-travel so the real engine evaluates each past bar seeing only data up to that instant (no look-ahead). |
| `backtest/replay.py` | Steps 5m bars, runs the real `engine.evaluate_stock`, opens a simulated trade on each IMMEDIATE signal. |
| `backtest/simulator.py` | Resolves each trade with the SAME exit rules `positions.py` uses (T1 book 50% + breakeven, trail, time-stop, square-off) and `round_trip_cost`. |
| `backtest/metrics.py` | Win rate, avg win/loss R, profit factor, expectancy/trade, max drawdown, trades/day, per-pattern/signal breakdowns. |
| `backtest/run.py` | One-shot backtest of a config over cached data. |
| `backtest/sweep.py` | Runs many config variants and compares them (train vs validation). |

---

## Step 1 — Fetch real history (needs YOUR Angel One credentials)

The repo ships with **no cached data** (`backtest/data/` is gitignored). You must
fetch it once with your own `.env` credentials and a working network:

```bash
venv/Scripts/python -m backtest.history --config intraday_config.json --months 6
```

This writes `backtest/data/<SYMBOL>/{5m,15m,1d}.csv`. Six months across the
15-symbol watchlist is a reasonable starting window. More is better for
statistical confidence; fewer than ~2 months is too thin to trust.

> Until you do this, every backtest/sweep below runs but reports **0 trades**,
> because there is nothing to replay. The numbers only become real with your data.

## Step 2 — Baseline backtest

```bash
venv/Scripts/python -m backtest.run --config intraday_config.json --out-dir backtest/out
```

Read the `OVERALL` block first:
- **expectancy_r** is the headline edge. Positive = the strategy made money per
  trade (in R units, after costs). Negative = it lost money; no amount of
  frequency tuning fixes a negative edge.
- **profit_factor** > 1 means gross wins outweigh gross losses. ~1.3+ is
  workable, 1.5+ is good for intraday.
- **trades_per_day** is your frequency. This is the number you want near/above 1.
- **max_drawdown_r** is the worst peak-to-trough in R — your pain tolerance check.

Then read `BY PATTERN` and `BY SIGNAL` to see which setups actually carry the
edge and which bleed. A pattern with negative expectancy and many trades is a
candidate to disable.

## Step 3 — Sweep to choose the gate relaxation + thresholds

```bash
venv/Scripts/python -m backtest.sweep --config intraday_config.json --out backtest/out/sweep.json
```

This compares `strict` vs `tiered`, several `booster_penalty` values, score
thresholds, `min_rvol`, `min_rr`, and loose/tight must-have sets. For each
variant it shows **trades/day vs expectancy/profit-factor** on the full window
AND on a held-out validation half.

How to read it:
- Prefer variants that are positive on **both** the training and validation
  halves. A variant that only shines on the full window is likely overfit.
- Among the robust ones, pick the highest expectancy that also clears your
  frequency goal (`--min-trades-per-day`, default 1.0).
- The tool prints a `RECOMMENDATION` line applying exactly that rule.

**The frequency/quality tradeoff (your a+b goal):** moving `strict -> tiered`
and lowering `booster_penalty`/score threshold increases trades/day but usually
lowers expectancy. The sweep makes that curve explicit so you choose the point
where you still have a positive, robust edge. If NO variant clears both bars,
the honest answer is "don't force it" — the recommendation says so, and you
should gather more data or accept fewer trades rather than trade a negative edge.

## Step 4 — Apply + forward-test

1. Copy the winning variant's settings into `intraday_config.json`
   (e.g. `gate_mode: "tiered"`, a `booster_penalty`, a `hard_gate_must_haves`
   set). The config-consistency guard test will remind you to keep the README
   table in sync if you document new live values.
2. Run the dashboard in paper mode for a few weeks and compare live results to
   the backtest. Only trust the change if forward results roughly match.
3. Re-run the whole loop periodically as you accumulate more history.

---

## Reproducibility

Given the same cached data and the same variant list, `backtest.sweep` and
`backtest.run` produce identical metrics every time (replay + metrics are pure
and deterministic). This is covered by `backtest/tests/test_sweep.py`.

## What this loop deliberately will NOT do

- It won't place orders. Everything is analysis/paper only.
- It won't tell you a config is profitable live — only that it was profitable on
  past cached bars under modeled costs. Forward-testing is non-negotiable.
- It won't force a daily trade at the expense of expectancy. "No qualifying
  trade today" is a valid, often profitable outcome; the best-available panel
  (context-only) is there so you still have something to look at.
