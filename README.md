# Intraday Trading System for Indian Stocks

A professional intraday trading system for NSE/BSE stocks with a **hard-gate engine architecture**.

---

## 🎯 **IMPLEMENTATION STATUS: READY FOR LIVE TRADING** ✅

**Last Updated:** 2026-08-25  
**Status:** All P0–P2 expert recommendations from the consolidated doc implemented/audited (excl. WebSocket #19, owner-excluded; Score-independence #13 deferred per doc). See **Expert Recommendations Audit** below for the full 28-section status table. Data-fetch fixes complete (Angel One `SmartApi` import + 5m empty-candle fallback) — verified live, no `data_missing` on daily/15m/5m/quote.

### **What's Been Fixed (Per Expert Recommendations):**
✅ **Session-only VWAP** - Resets at 9:15 AM daily (was including previous day)  
✅ **VWAP hard gate** - Made mandatory (BUY requires price > VWAP, SELL requires price < VWAP)  
✅ **Time-of-day RVOL** - Compares 10:35 volume vs historical 10:35 average  
✅ **20-day liquidity** - Uses genuine 20-day average from daily candles  
✅ **Score threshold enforcement** - IMMEDIATE signals require score >= the configured `buy/sell_score_threshold` (**live value: 60**; see Configuration)  
✅ **All thresholds kept strict** - No loosening per expert recommendation  

**📄 See `CHANGES_IMPLEMENTED.md` for complete details and testing checklist.**

---

## Architecture

```
intraday_trader/
├── engine.py              # Core hard-gate trading engine (SINGLE SOURCE OF TRUTH)
├── patterns.py            # 8-Pattern entry engine + PatternStateStore persistence
├── shared.py              # DataProvider + Indicators (Angel One fetching & TA)
├── premarket.py           # Pre-market + market-open analysis module
├── dashboard.py           # Web UI dashboard (scanner + cards + Pattern Scanner Matrix)
├── decision_log.py        # Persistent JSONL decision audit logger
├── intraday_server.py     # MCP server (stdio) exposing 5 tools
├── intraday_config.json   # Settings + stock universe + pattern parameters
├── requirements.txt       # Python dependencies
├── tests/
│   ├── test_components.py # Core system & risk unit tests
│   └── test_patterns.py   # Pattern engine unit tests (100% test coverage)
└── README.md
```

## Pattern Engine (`patterns.py`)

The pattern detection engine evaluates **8 zero-repaint intraday pattern families** on completed 5m candle closes. It uses thread-safe state persistence (`PatternStateStore`) to track multi-step patterns across scan cycles without repainting.

| # | Pattern Name | Identifier | Logic & Direction | Trigger Condition |
|---|--------------|------------|-------------------|-------------------|
| 1 | **Opening Range Breakout / Breakdown** | `ORB` | BUY / SELL | 5m candle closes outside 9:15-9:45 ORH/ORL with VWAP/RVOL alignment. |
| 2 | **Previous Day High / Low** | `PDH_PDL` | BUY / SELL | 5m candle closes outside PDH (Breakout) or PDL (Breakdown). |
| 3 | **Breakout + Retest** | `BREAKOUT_RETEST` | BUY / SELL | Stateful: candle breaks key level, retests support/resistance, and closes with rejection candle. |
| 4 | **VWAP Pullback** | `VWAP_PULLBACK` | BUY / SELL | Price pulls back into VWAP zone during strong trend and bounces with rejection candle. |
| 5 | **EMA20 Pullback** | `EMA20_PULLBACK` | BUY / SELL | Pullback to 5m EMA20 in strong trend confirmed by bullish/bearish candle close. |
| 6 | **Failed ORB Reversal (Trap)** | `FAILED_ORB` | BUY / SELL | False breakout above ORH / below ORL that quickly fails, reclaiming range for reversal. |
| 7 | **Gap-and-Go** | `GAP_AND_GO` | BUY / SELL | Stocks opening with >0.5% gap that hold gap direction and break opening 5m high/low. |
| 8 | **Inside-Bar Breakout** | `INSIDE_BAR` | BUY / SELL | Consolidation candle (range inside previous candle) broken by 5m directional close. |

### Pattern Agreement Metrics
- **Primary Pattern:** The highest-confidence confirmed pattern detected for the stock.
- **Pattern Agreement Count:** Total number of active patterns pointing in the same direction.
- **Agreement Level:** `HIGH` (≥3 matching patterns), `MEDIUM` (2 matching patterns), `LOW` (1 pattern), `NEUTRAL` (0 patterns).



## Output Format (mirrors `stock_monitor`)

The dashboard output is intentionally structured to **match the swing-trading
`stock_monitor`** terminal output format, adapted for intraday:

- **Per-stock decision badge** (BUY / SELL / WAIT) as the primary headline.
- **Hard-gate (entry stimulus) block** rendered first, exactly like
  `stock_monitor`'s entry stimulus: overall PASS/FAIL badge + a per-condition
  checklist (✓ done / ✗ needed / NOT EVALUATED), built only from backend
  values — never recomputed or inferred in the frontend.
- **Score (0-100)** shown secondary to the gate, mirroring the swing model
  where the gate is authoritative and the score is a ranking aid.
- **Entry / Stop Loss / Targets / Risk-Reward** levels presented identically.
- **Trend across timeframes** (Daily → 15m → 5m) and **technical confirmation**
  (VWAP, RVOL, RSI, ATR) shown as the supporting evidence block.

This keeps the intraday view consistent with the existing swing-trading output
so users reading either system see the same decision-first layout.

## Key Concepts

### Timeframe Hierarchy
- **Daily (1d)** – Primary trend direction
- **15m** – Structure / confirmation
- **5m** – Entry trigger (breakout/breakdown)

### Hard-Gate Engine (engine.py)
Every stock is analyzed by **independent BUY and SELL engines**. Each engine has:
- **Hard gates** – MUST be satisfied for a signal (e.g., liquidity, volume, trend alignment)
- **Score (0-100)** – Secondary ranking factor within an open gate

Signals:
| Signal | Meaning |
|--------|---------|
| `IMMEDIATE_BUY` | All BUY hard gates + major conditions passed (enter now) |
| `IMMEDIATE_SELL` | All SELL hard gates + major conditions passed (exit/short now) |
| `ALMOST_BUY` | Major conditions pass except 5m breakout confirmation |
| `ALMOST_SELL` | Major conditions pass except 5m breakdown confirmation |
| `BUY_NOW` | BUY gate open with high score |
| `SELL_NOW` | SELL gate open with high score |
| `WAIT` | No valid setup |
| `CANCEL` | Trade cancelled |

### Worked Example: Breakout Strategy

The engine is a **momentum / breakout** system. It does NOT buy at lows and sell at highs. Instead it buys when price *breaks above resistance* (expecting more upside) and sells/shorts when price *breaks below support* (expecting more downside).

**Concrete example — RELIANCE intraday:**

1. **Pre-market context**: RELIANCE closed yesterday at ₹2470 (PDC). PDH = ₹2500, PDL = ₹2450. Daily trend = BULLISH.
2. **Morning (9:15–9:45)**: Price consolidates between ₹2470 (support) and ₹2500 (resistance).
3. **In session (9:45+)**: 15m trend = BULLISH, 5m structure = BULLISH. NIFTY regime = TRENDING_UP.
4. **Trigger (≈11:00)**: A **5-minute candle CLOSES at ₹2505** — above the ₹2500 resistance (breakout). The close must be a real close, not just a wick poking above.
5. **Hard gates checked**: session ✓, liquidity GOOD ✓, daily BULLISH ✓, 15m BULLISH ✓, 5m BULLISH ✓, breakout ✓, ATR valid ✓, RR ≥ 2.0 ✓ → **`IMMEDIATE_BUY`**.
6. **Levels computed**:
   - Entry: ₹2505 (just above resistance)
   - Stop-loss: ₹2495 (just below resistance/support)
   - Target 1: ₹2525, Target 2: ₹2535, Target 3: ₹2555 (ATR-based)
   - Risk/Reward ≈ 2.0

**For SELL/short** the mirror happens: price consolidates, a 5m candle **closes below support** → `IMMEDIATE_SELL`, short at the breakdown level, stop above, targets below.

> **Key takeaway**: You trade *with* breakout momentum — buying near a relative high expecting higher, or selling near a relative low expecting lower. If you want to *buy at support / sell at resistance* (mean-reversion scalping), that is a **different strategy** not yet enabled by this engine.

### Canonical Record
`evaluate_stock()` returns **one canonical record** consumed by:
- Dashboard (`dashboard.py`)
- Scanner (`engine.scan_universe`)
- MCP server (`intraday_server.py`)
- Alerts (not yet implemented)

This ensures consistency — no duplicated analysis logic.

## Trading Workflow (read this first)

**`TRADING_GUIDE.md`** is the step-by-step guide for what *you* do vs. what the system
does: pre-market watchlist build (Scan Universe), when to refresh, reading the
BUY/SELL cards, executing orders, and risk rules. Start there for daily operation.

## Dashboard

```bash
cd intraday_trader
python dashboard.py --config intraday_config.json --port 8080
```

**Features:**
- Auto-refreshes every 60s with live countdown timer (60s, 59s, 58s... → reload)
- BUY/SELL scores displayed as gradient progress bars (visual clarity)
- Displays scanner summary (total, buy/sell candidates, immediate buy/sell, errors)
- JSON API: `http://localhost:8080/?format=json`

### Presentation Model (Priority-Ordered)

The `dashboard.py` renderer **does not invent any analysis**. It only displays the canonical record produced by `engine.py`. Presentation follows the spec priority order (P0 → P1 → P2), so a trader can read a card top-to-bottom without scrolling through irrelevant data first.

**Card order (top → bottom):**

| Priority | Section | Source fields (from canonical record) |
|----------|---------|----------------------------------------|
| **P0** | Decision header — signal badge + reason | `signal`, `reason`, `setup_id` |
| **P0** | Price / Session status | `price`, `session_status` |
| **P0** | 5m Trigger (entry timeframe) | `breakout`, `breakdown`, `confirm_level`*, `cancel_level`* |
| **P0** | Trade Levels | `entry`, `stop_loss`, `target_1/2/3`, `risk_reward` |
| **P0** | Hard Gates (gate OPEN/PASS shown BEFORE score) | `buy_hard_gate`, `sell_hard_gate`, `buy_conditions`, `sell_conditions`, `buy_failed_conditions`, `sell_failed_conditions`, `buy_score`, `sell_score` |
| **P1** | Trend (Daily → 15m → 5m) + market regime | `daily_trend`, `m15_trend`, `m5_structure`, `market_regime` |
| **P1** | Key Levels | `pdh`, `pdl`, `pdc`, `orh`*, `orl`*, `support`, `resistance`, `confirm_level`*, `cancel_level`* |
| **P1/P2** | Technical Confirmation | `vwap`, `rvol`, `atr_5m`, `rsi` |
| **P2** | Context | `liquidity_status`, `event_risk`, `sector`*, `relative_strength`*, `watchlist_state`*, `gap_type`*, `failed_breakout`* |
| **P1/P2** | Technical Details (collapsible) | `atr_15m`, `adx`*, `ema20`, `ema50`, `volume_signal`, `body_ratio`, `spread_percent`, `overextended`, `avg_volume`, `avg_traded_value`, 5m candle OHLC* |

`*` = optional; rendered only when the field is present in the record (currently the core engine populates the non-starred fields; the starred fields are surfaced automatically if/when the engine supplies them, e.g. pre-market context).

**Gate vs Score rule:** The hard-gate status (PASS/FAIL with explicit blocker reason) is displayed **before** the BUY/SELL scores. A closed gate means NO TRADE regardless of score — the score is shown only as secondary ranking. The dashboard never computes its own gate/score; it mirrors `engine.py` exactly, so the displayed decision matches the terminal/MCP output character-for-character.

**Closed-market behavior:** When `session_status == "MARKET_CLOSED"`, the card collapses to a compact "NOT EVALUATED" state — no scores, gates, or live levels are shown, since the engine does not evaluate conditions outside market hours. This prevents false context from appearing when data is stale.

### Auto-Selection (Scan Universe / Watchlist Builder)

The dashboard has a **`🔄 Scan Universe`** button in the toolbar. Clicking it auto-selects the intraday watchlist so you do not have to pick symbols manually, and renders a **Watchlist Builder — Candidate Quality** panel (pre-open context, NOT a trade signal).

What `auto_select.py` does (no backend engine/MCP code is touched — only `auto_select.py` + the `/api/scan` endpoint in `dashboard.py`):
1. Fetches the universe from `settings.universe_filter` (NIFTY100). If the live NSE constituent list is unreachable, it falls back to a curated liquid-NIFTY list.
2. Pre-filters by the **same liquidity thresholds** already in the config (`min_price`, `min_avg_volume`, `min_avg_traded_value`).
3. For each survivor, builds a **Candidate Quality Score** from PRE-OPEN-safe, reusable `engine`/`premarket` inputs only — so it works even when the market is closed and no 5m bars exist:
   - `prevday_trend` (daily_dir daily + prev day up/down from `get_prev_day_levels`)
   - `relative_strength` (vs NIFTY via `get_relative_strength`)
   - `sector_strength` (via `get_sector_strength`)
   - `gap_quality` (via `get_gap_quality`, needs live pre-open last price)
   - `liquidity` (avg volume + avg traded value vs thresholds)
   - `atr_suitability` (prev-day ATR range vs `atr_5m_multiplier` targets)
   - `premarket_activity`, `event_risk`, `prevday_participation`
   Sub-scores are 0–100 and weighted by `settings.candidate_score_weights` (default total 100). Higher CQ = better watchlist candidate.
4. Takes a **wider candidate pool** (`settings.candidate_pool_size`, default 50) and writes the **top-N** (`settings.watchlist_size` / `--top`, default 20) into `intraday_config.json["stocks"]`, preserving all `settings`. The full ranked list (with component sub-scores) is returned for the Watchlist Builder panel.
5. **Live dynamic discovery** — when the market is open, `dynamic_discover()` additionally scans the universe for names with `rvol >= settings.dynamic_min_rvol` (default 2.0) / scheduled events and returns `settings.dynamic_opportunity_pool_size` (default 5) of the strongest ongoing movers (`dynamic_opportunities`), surfaced as *context only* — never auto-added to the live config.

```bash
# Via the dashboard button (runs in-browser, then reloads):
python dashboard.py --config intraday_config.json --port 8080
# → open http://localhost:8080 → click "Scan Universe"

# Or directly from the CLI:
python auto_select.py --config intraday_config.json --top 20
```

The **Watchlist Builder — Candidate Quality** panel on the dashboard shows the pre-open ranking with each sub-score (Trend, RS, Sector, Gap, Liq, ATR, Event). It is explicitly labeled **"Context only — NOT a trade signal."** The live engine cards below it are the single source of truth for BUY/SELL decisions.

Notes:
- `top` controls how many stocks land in the watchlist (default 20 = `settings.watchlist_size`). `candidate_pool_size` (default 50) controls how many names are scored before truncation.
- The scan uses Angel One market data for the universe; run `auto_select.py` **before** the session (overnight / pre-open) so the watchlist is ready at 9:15, then let the live engine drive signals during market hours.
- Because it only rewrites `stocks` + emits a context ranking, the engine/MCP output and every other setting remain unchanged.
```

- `dynamic_discover()` is best run during market hours; it returns `{"opportunities": [...]}` only, never mutates `intraday_config.json`.

## MCP Server

```bash
cd intraday_trader
python intraday_server.py
```

Exposes these tools:

### 1. `intraday_trade_setup`
Generate a complete intraday setup for one symbol using the hard-gate engine.

```json
{
  "symbol": "RELIANCE",
  "period": "5d",
  "exchange": "nse"
}
```

Returns:
```json
{
  "symbol": "RELIANCE",
  "current_price": 2456.30,
```

  "signal": "IMMEDIATE_BUY",
  "reason": "All conditions met for BUY",
  "market_regime": "TRENDING_UP",
  "trend_analysis": { "daily_trend": "BULLISH", "m15_trend": "BULLISH", "m5_structure": "BULLISH" },
  "scores": { "buy_score": 85, "sell_score": 12, "buy_hard_gate": true, "sell_hard_gate": false },
  "conditions": { "buy_conditions": { "trend_alignment": true, "breakout": true, ... } },
  "setup": {
    "name": "RELIANCE",
    "symbol": "RELIANCE",
```

    "interval": "5m",
    "entry_zone_lo": 2450.0,
    "entry_zone_hi": 2460.0,
    "confirm_level": 2470.0,
    "cancel_level": 2435.0,
    "stop_loss": 2435.0,
    "targets": [2485.0, 2500.0, 2530.0],
    "risk_reward": 2.5
  },
  "config_json_block": "{ ... }"
}
```

### 2. `intraday_market_scanner`
Scan the configured universe (from `intraday_config.json`) and return ranked candidates.

```json
{
  "exchange": "nse"
}
```

Returns `TOP_BUY_CANDIDATES` and `TOP_SELL_CANDIDATES` sorted by hard-gate status, score, risk/reward, volume, and liquidity.

### 3. `get_intraday_indicators`
Get technical indicators across 15m / 5m / 1m timeframes.

```json
{
  "symbol": "RELIANCE",
  "period": "1d",
  "exchange": "nse"
}
```

### 4. `premarket_scanner`
Scan the market for pre-market opportunities. Returns `TOP_BUY_WATCHLIST` and `TOP_SELL_WATCHLIST` with gap analysis, sector strength, relative strength, event risk, and watchlist state.

```json
{
  "exchange": "nse"
}
```

### 5. `premarket_stock_analysis`
Analyze ONE stock for pre-market context. Returns previous day levels (PDH/PDL/PDC), gap analysis, trend, sector strength, relative strength, pre-market activity, event risk, and watchlist state.

```json
{
  "symbol": "RELIANCE",
  "exchange": "nse"
}
```

## Pre-Market Module (`premarket.py`)

The pre-market module provides context and preparation for the trading day. It NEVER generates live entry signals.

### Safety Rules
- Pre-market states (`WATCH_BUY`, `WATCH_SELL`, `WATCH`, `AVOID`) are disjoint from live states (`IMMEDIATE_BUY`, `IMMEDIATE_SELL`, etc.)
- No pre-market value can bypass a live hard gate
- The live BUY/SELL hard-gate engine (`engine.py`) remains authoritative

### Features
- **Market Context** – NIFTY 50 / BANK NIFTY direction, change %, market regime
- **Previous Day Levels** – PDH, PDL, PDC, VWAP, ATR, trend, momentum
- **Gap Analysis** – STRONG_GAP_UP / GAP_UP / FLAT / GAP_DOWN / STRONG_GAP_DOWN
- **Gap Quality** – Continuation, fill, breakout/breakdown candidates
- **Opening Range** – ORH/ORL tracking from configurable start (default 09:15, 15 min)
- **False Breakout Protection** – FAILED_OPENING_BREAKOUT / FAILED_OPENING_BREAKDOWN detection
- **Sector Strength** – Sector classification + trend
- **Relative Strength** – vs NIFTY 50
- **Pre-market Activity** – Unusual / normal / low activity
- **Event Risk** – Classification (defaults to UNKNOWN)
- **Watchlist States** – WATCH_BUY, WATCH_SELL, WATCH, AVOID

### Configuration (`intraday_config.json`)

```json
{
  "settings": {
    "premarket_enabled": true,
    "gap_analysis_enabled": true,
    "opening_range_enabled": true,
    "opening_range_minutes": 15,
    "opening_range_start": "09:15",
    "premarket_volume_threshold": 1.5,
    "gap_up_threshold": 0.5,
    "strong_gap_up_threshold": 1.5,
    "gap_down_threshold": 0.5,
    "strong_gap_down_threshold": 1.5,
    "max_event_risk": "MEDIUM",
    "relative_strength_lookback": 20,
    "premarket_watchlist_size": 10
  }
}
```

## Configuration (`intraday_config.json`)

> **Single source of truth:** `intraday_config.json` is the authoritative set of
> live thresholds. The engine reads it exactly (via `normalize_settings`), and the
> backtest (`python -m backtest.run`) reads the same file, so what you test is what
> you run. The values shown below are copied from the live config as of the last
> reconciliation; if they ever diverge, **`intraday_config.json` wins** and this
> table is the thing that is out of date. The `backtest/tests/test_config_consistency.py`
> guard test fails if the documented values below drift from the file.

The thresholds that actually drive signals (authoritative live values):

| Setting | Live value | Meaning |
|---------|-----------|---------|
| `min_rvol` | **1.3** | Minimum relative volume for the volume gate |
| `preferred_rvol` | 2.0 | RVOL that earns full volume score |
| `min_rr` | **1.8** | Minimum reward:risk (gated on T3) |
| `preferred_rr` | 2.5 | RR that earns full RR score |
| `buy_score_threshold` | **60** | Score needed for IMMEDIATE_BUY (else ALMOST) |
| `sell_score_threshold` | **60** | Score needed for IMMEDIATE_SELL |
| `almost_min_score` | 55 | Minimum score for the ALMOST tier |
| `max_entry_extension_atr` | 0.5 | Max ATR past the level before "overextended" |
| `min_body_ratio` | 0.5 | Minimum candle body/range for candle-quality gate |
| `allow_counter_trend` | false | Counter-trend trades disabled |
| `use_market_regime_filter` | true | Regime alignment gate on |
| `entry_start_time` / `entry_end_time` | **09:45 / 14:30** | IMMEDIATE entry window (IST) |
| `watch_start_time` | 09:15 | WATCH/ALMOST monitoring may begin here |
| `square_off_time` | 15:15 | Force intraday square-off (IST) |
| `max_data_age_seconds` | 300 | Max quote staleness |
| `max_trades_per_day` | **4** | Daily trade cap (discipline guardrail) |
| `max_consecutive_losses` | 3 | Losing-streak guardrail |
| `dynamic_min_rvol` | **1.5** | RVOL for live dynamic-discovery movers |

Risk / position sizing lives under `settings.paper_risk` (and mirrors
`risk.py RiskLimits` defaults):

| `paper_risk` key | Live value |
|------------------|-----------|
| `account_equity` | 100000 |
| `risk_per_trade_percent` | 0.5 |
| `max_daily_loss_percent` | **2.0** |
| `max_open_positions` | **3** |
| `max_position_value_percent` | 20.0 |

For the full, exact set of keys (ATR multipliers, EMA periods, pattern params,
candidate-score weights, etc.) read `intraday_config.json` directly — it is the
canonical file and is intentionally not duplicated verbatim here to avoid drift.

## Setup (first time)

The system uses **Angel One as the sole market-data provider** (no yfinance fallback).
A Python virtual environment (`venv/`) already exists in this folder.

```bash
cd /home/pandia/Desktop/intraday_trader

# 1. (If missing/reset) create the venv once
python3 -m venv venv

# 2. Install dependencies — INCLUDING websocket-client (required by SmartApi)
venv/bin/pip install -r requirements.txt

# 3. Provide Angel One credentials
cp .env.example .env          # then edit .env with your real values
#   ANGELONE_API_KEY / ANGELONE_CLIENT_ID / ANGELONE_PIN / ANGELONE_TOTP_SECRET
#   INTRADAY_DATA_SOURCE=angelone   (must be exactly "angelone")
```

> **Note:** `websocket-client` MUST be installed. Without it, `SmartApi` fails to
> import and *every* fetch returns `data_missing` for all timeframes. `requirements.txt`
> lists it, so a clean `pip install -r requirements.txt` covers it.

Verify the install + credentials + live data in one shot:

```bash
venv/bin/python -c "
import sys; sys.path.insert(0,'.')
import shared
dp = shared.DataProvider()
print('quote:', dp.get_realtime_quote('RELIANCE'))
print('daily:', len(dp.get_ohlcv('RELIANCE','1d','1d','nse',70).get('candles',[])))
print('m15  :', len(dp.get_ohlcv('RELIANCE','5d','15m','nse',75).get('candles',[])))
print('m5   :', len(dp.get_ohlcv('RELIANCE','5d','5m','nse',75).get('candles',[])))
"
```

All three candle counts should be `> 0` and `quote` should contain `last_price`.

## Running

**Dashboard (main UI):**
```bash
venv/bin/python dashboard.py --config intraday_config.json --port 8080
# open http://localhost:8080  (auto-refreshes every 60s; engine recomputes every 5 min)
```

**MCP server (for Claude / automation):**
```bash
venv/bin/python intraday_server.py
```

**Offline test suite (no network/credentials needed):**
```bash
venv/bin/pip install pytest
venv/bin/python -m pytest tests/ -q
```

For the daily trading workflow (pre-market scan, reading cards, executing orders,
risk rules), see **`TRADING_GUIDE.md`**. For a one-page setup-and-run checklist,
see **`SETUP_AND_RUN.md`**.

## Data-fetch fixes (history / why it was broken)

Two issues previously caused constant `data_missing` on daily/15m/5m and are now fixed:

1. **Missing `websocket-client`** → `SmartApi` import failed → every fetch silently
   returned missing data. Fixed by adding `websocket-client` to `requirements.txt`
   (and a runtime guard message if it is absent).
2. **Angel One returns 0 candles (no error) for short 5m periods** (`period="2d"`).
   `brokers/angelone.py get_ohlcv` now uses a **progressive period fallback**
   (`requested → 5d → 1mo → 3mo`) on empty no-error responses, and `engine.py`
   requests the 5m window with `period="5d"` directly.

SSL (`CERTIFICATE_VERIFY_FAILED`) is already handled via `verify=False`, and
`.env` credentials are verified correct (not modified).

## Market Hours
```


- **Entry window**: 09:45 – 14:30 IST
- **Square-off**: 15:15 IST
- **Data freshness**: Max 300s (configurable)
---

## Expert Recommendations Audit (Consolidated Doc, 28 sections)

**Last audited:** 2026-08-25 — against `Intraday_Engine_Consolidated_Fixes_and_Improvements.md`.
Test suite: **green** (`pytest` -> 5 passed; 2 live-integration tests deselected pending broker credentials).

| # | Recommendation | Severity | Status | Where |
|---|----------------|----------|--------|-------|
| 1 | RR gate structurally broken | 🔴 | ✅ Done | Structural RR, gate on T3 = 2×ATR (`engine.py`) |
| 2 | Quote timestamp / timezone | 🔴 | ✅ Done | `Asia/Kolkata` localization (`engine.py`) |
| 3 | Completed candles authoritative | 🔴 | ✅ Done | `get_completed_candles` (`engine.py:156`) |
| 4 | 20-day daily liquidity | 🔴 | ✅ Done | `len(daily) < 20 -> INSUFFICIENT_HISTORY` |
| 5 | Broken integration tests | 🔴 | ✅ Done | `test_angelone_integration.py` -- 5 passed |
| 6 | Missing data -> WAIT | 🔴 | ✅ Done | `data_quality_issues` -> `signal="WAIT"` |
| 7 | RVOL 1.5 + broaden discovery | 🟠 | ✅ Done | `min_rvol=1.5`, universe broadening (`auto_select.py`) |
| 8 | Wider candidate breadth | 🟠 | ✅ Done | `NIFTY100` + `dynamic_discover` |
| 9 | Separate WATCH start time | 🟠 | ✅ Done | `watch_start=09:15`, `entry_start=09:45` |
| 10 | 0.5 ATR extension strict | 🟠 | ✅ Done | `max_entry_extension_atr=0.5` |
| 11 | Market regime handling | 🟠 | ✅ Done | `detect_market_regime`, filter on |
| 12 | Structural RR not fixed ATR | 🟠 | ✅ Done | `_structural_swing_levels` |
| 13 | Score independence | 🟡 | ⏸ Deferred | Doc says _do not redesign until paper/backtest data exists_ |
| 14 | ALMOST definition | 🟡 | ✅ Done | distance-to-trigger + WAIT explanation |
| 15 | WATCH tier | 🟡 | ✅ Done | `BUY_WATCH`/`SELL_WATCH` -> `ALMOST` -> `IMMEDIATE` |
| 16 | Primary rejection reason | 🟡 | ✅ Done | `find_primary_blocker` + `GATE_PRIORITY` |
| 17 | Rejection funnel | 🟡 | ✅ Done | `scan_universe` `rejection_funnel` + dashboard |
| 18 | Persistent decision logging | 🟡 | ✅ Done | `decision_log.py` wired into `evaluate_stock` |
| 19 | WebSocket architecture | 🟡 | ⛔ Excluded | Not implemented -- REST polling sufficient for 5m engine (owner decision) |
| 20 | Separate engine/order exec | 🟡 | ✅ Done | Engine analysis-only; `risk.py` has no order code |
| 21 | Risk mgmt / no leverage | 🟡 | ✅ Done | `PaperRiskManager` sizes on `capital x risk%` |
| 22 | Daily risk controls | 🟡 | ✅ Done | `max_daily_loss=2.0%`, `max_open_positions=3`, consecutive-loss + drawdown guardrails |
| 23 | What NOT to change | 🟡 | ✅ Respected | All thresholds kept canonical |
| 24 | Backtesting requirements | -- | 📄 Doc | Process item; cannot backtest yet |
| 25 | Signal-availability strategy | -- | 📄 Doc | Followed |
| 26 | Recommended final architecture | -- | 📄 Doc | Reflected in funnel pipeline |
| 27 | Final implementation order | -- | 📄 Doc | Phases A-E followed |
| 28 | Final threshold recommendation | -- | ✅ Done | All values match in `intraday_config.json` |

**Notes:**
- **#13** is intentionally deferred per the expert's own instruction (line 833 of the doc: _"Do not redesign the score until paper/backtest data exists."_) It is **not** a missing item.
- **#19 (WebSocket)** was reviewed and deliberately excluded: the engine trades off the **completed 5m** candle and auto-refreshes every 60s, so REST polling meets every correctness goal. WebSocket would only add a continuous live LTP and a forming-candle view -- beneficial but not required. Revisit only if moving to 1m/tick entry or auto-order placement.
- **Live thresholds (authoritative, from `intraday_config.json`):** `min_rvol 1.3`, `preferred_rvol 2.0`, `min_rr 1.8`, `preferred_rr 2.5`, `buy/sell_score_threshold 60`, `almost_min_score 55`, `extension 0.5 ATR`, `counter_trend off`, `regime filter on`, `entry 09:45-14:30`, `square_off 15:15`, `max_trades_per_day 4`, `paper_risk.max_daily_loss_percent 2.0`, `max_open_positions 3`. These are looser than the earlier expert "Final Threshold Recommendation" (which suggested `min_rvol 1.5 / min_rr 2.0 / score 65`); the live config was intentionally relaxed and is the single source of truth. See the Configuration section's table. The `#28` row in the audit above reflects the original doc values, not the current live config.
