# Intraday Trading — Step-by-Step Workflow

This guide explains exactly what **you** do vs. what the **system** does, and when to
click each button. It is written for the `intraday_trader` dashboard (`dashboard.py`)
plus the optional MCP server.

> Everything below is **context/decision support only**. You place the actual buy/sell
> orders in your broker terminal. The system never places trades for you.

---

## 0. Start the dashboard (once, in the morning)

```bash
cd /home/pandia/Desktop/intraday_trader
python3 dashboard.py --config intraday_config.json --port 8080
```

Open **http://localhost:8080** in your browser. Leave it running all day.

The page **auto-refreshes every 60 seconds** and the engine **recomputes signals
every 5 minutes** (cache TTL = 300s). You do not need to babysit it.

---

## 1. Do you have to add stocks manually? (Pre-market prep)

**No — the system can build the watchlist for you from pre-open / pre-market data.**

Use the **`🔄 Scan Universe`** button (top-right of the dashboard) **before market
open** (best between 9:00–9:15, or even the night before). What it does:

1. Pulls the NIFTY100 universe (falls back to a curated liquid list if NSE is unreachable).
2. Pre-filters by the liquidity rules already in `intraday_config.json`
   (`min_price`, `min_avg_volume`, `min_avg_traded_value`).
3. Scores every survivor with a **Candidate Quality** model built **only from
   pre-open-safe inputs** (previous-day trend, relative strength vs NIFTY, sector
   strength, gap quality, liquidity, ATR suitability, event risk, participation).
4. Writes the **top 20** names into `intraday_config.json["stocks"]` automatically.

After it finishes, the page reloads and you will see a **"Watchlist Builder —
Candidate Quality"** table at the top — a **pre-open ranking (context only, NOT a
trade signal)** — followed by the live engine cards for the 20 selected stocks.

**Alternative:** you can also build the watchlist from the CLI:

```bash
python3 auto_select.py --config intraday_config.json --top 20
```

Or you can **manually** edit `intraday_config.json` → `"stocks": [{ "symbol": "RELIANCE" }, ...]`
if you have your own list. The engine evaluates whatever is in that list; you never
*have* to run Scan Universe.

> **Rule of thumb:** Run **Scan Universe once before market open** to let the system
> pick candidates from pre-market data. Do **not** click it repeatedly during the day
> — see §3.

---

## 2. Market open & what you watch

- Market hours: **09:15 – 15:30 IST**.
- Engine **entry window**: **09:45 – 14:45** (config `entry_start_time` / `entry_end_time`).
- **Square-off**: everything closed by **15:15** (config `square_off_time`).

When the market is **closed**, cards show a compact `MARKET CLOSED / NOT EVALUATED`
state — no signals, gates, or levels (the engine computes only during market hours).

During market hours, each stock card is **decision-first**:

```
┌─────────────────────────────────────────────┐
│ RELIANCE              [ IMMEDIATE_BUY ]       │  ← P0: decision badge
│ reason: All conditions met for BUY           │
│ Price ₹2505.00  | Session: IN_SESSION        │  ← P0: price/session
│ 5m Breakout ✓  5m Breakdown ✗                │  ← P0: trigger
│ Pattern: ORB Breakout (HIGH Agreement: 3)    │  ← P0: primary pattern & agreement
│ Entry ₹2505  SL ₹2495  T1/T2/T3 ₹2525/35/55  │  ← P0: levels
│ Pattern: ORB Breakout (HIGH Agreement: 3)    │  ← P0: primary pattern & agreement
│ RR 2.0                                       │
│ BUY Gate  🔓 OPEN   SELL Gate 🔒 CLOSED      │  ← P0: hard gates (authoritative)
│ BUY Score 85/100  SELL Score 12/100          │  ← secondary
└─────────────────────────────────────────────┘
```

---

## 3. Pattern Scanner Matrix & Signal Interpretation

The dashboard renders a **Pattern Scanner Matrix** table above the stock cards. This table scans all 8 pattern families across your watchlist in real time:

1. **Opening Range Breakout / Breakdown (`ORB`)**
2. **Previous Day High / Low (`PDH_PDL`)**
3. **Breakout + Retest (`BREAKOUT_RETEST`)**
4. **VWAP Pullback (`VWAP_PULLBACK`)**
5. **EMA20 Pullback (`EMA20_PULLBACK`)**
6. **Failed ORB Reversal (`FAILED_ORB`)**
7. **Gap-and-Go (`GAP_AND_GO`)**
8. **Inside-Bar Breakout (`INSIDE_BAR`)**

### Understanding Pattern Agreement:
- **`HIGH` Agreement (3+ Patterns):** High-probability setup where multiple distinct technical patterns align in the same direction (e.g. `ORB` + `VWAP_PULLBACK` + `BREAKOUT_RETEST`).
- **`MEDIUM` Agreement (2 Patterns):** Strong setup with dual-pattern alignment.
- **`LOW` / `NEUTRAL` Agreement (0-1 Pattern):** Single-pattern setup; relies strictly on the primary hard gate.

---

## 4. Complete Inventory of All Data You Get

When monitoring stocks through the dashboard, engine, decision log, or MCP API tools, you receive:

| Category | Data Fields Included |
|----------|----------------------|
| **Quote & Session** | LTP, Open, High, Low, Close, `IN_SESSION` / `CLOSED` state, localized timestamp (`Asia/Kolkata`). |
| **Timeframe Trends** | Daily (1d) trend, 15m structure trend, 5m execution trend, timeframe alignment (`ALIGNED` / `CONFLICT`). |
| **Reference Levels** | Opening Range High (`ORH`), Opening Range Low (`ORL`), Previous Day High (`PDH`), Low (`PDL`), Close (`PDC`), Opening Gap %. |
| **Indicators & TA** | Session-only VWAP, time-of-day RVOL, RSI-14, ATR-14, EMA-20/50/200, Bollinger Bands. |
| **8-Pattern Engine** | Pattern ID, name, direction, status (`CONFIRMED`, `APPROACHING`, `WATCHING`), trigger price, distance %, confidence score, invalidation level, conditions, reasons. |
| **Pattern Agreement** | Primary pattern name, agreement count, agreement level (`HIGH` / `MEDIUM` / `LOW` / `NEUTRAL`). |
| **Hard-Gate Engine** | Decision signal (`IMMEDIATE_BUY`, `IMMEDIATE_SELL`, `ALMOST`, `WAIT`), BUY/SELL hard gates (`OPEN`/`CLOSED`), score (0-100), primary rejection reason if gate is closed. |
| **Trade Sizing & Levels** | Recommended Entry price, Stop Loss (SL), Targets (T1, T2, T3), Risk-to-Reward (RR), share quantity (`qty`), total capital required. |
| **Decision Audit Log** | Persistent JSONL record of every scan cycle saved to `decision_log.jsonl`. |aily BULLISH · 15m BULLISH · 5m BULLISH     │  ← trend
│ VWAP / RVOL / ATR / RSI                        │  ← confirmation
└─────────────────────────────────────────────┘
```

**The hard gate is the boss.** If BUY Gate = CLOSED, there is **no trade** no matter
how high the score. Only act when the gate is OPEN *and* the signal is actionable.

### Signal meanings
| Signal | What it means for you |
|--------|-----------------------|
| `IMMEDIATE_BUY` | All BUY gates + breakout confirmed → **enter long now** |
| `IMMEDIATE_SELL` | All SELL gates + breakdown confirmed → **enter short now** |
| `ALMOST_BUY` | Setup ready, waiting for 5m breakout close → **prep a buy order** |
| `ALMOST_SELL` | Setup ready, waiting for 5m breakdown close → **prep a sell order** |
| `BUY_NOW` / `SELL_NOW` | Gate open with high score → momentum bias |
| `WAIT` | No valid setup → do nothing |
| `CANCEL` | Setup invalidated → abandon the idea |

---

## 3. When do you click "Scan Now" / "Refresh Now"?

There is **no button literally named "Scan Now"**. You have two buttons:

- **`Refresh Now`** — reloads the page. If the 5-minute signal cache is fresh it just
  shows what's already computed (instant). If the cache is older than 5 min, the page
  triggers a fresh recompute in the background. Use it when you want to *force a look*
  without waiting for the 60s auto-refresh.
- **`🔄 Scan Universe`** — rebuilds the **watchlist** from pre-market data and
  recomputes. **Use this before market open (prep).** Do **not** spam it during the day,
  because it will *replace* your current 20 stocks with a fresh auto-picked list.

**So, during live trading:**
1. Just **watch the dashboard** — it auto-refreshes every 60s and the engine silently
   recomputes every 5 min. A 5-minute lag is fine because the engine trades off 5-minute
   candles anyway.
2. Click **`Refresh Now`** only if you want to nudge a reload sooner than the next
   auto-refresh.
3. **Do not** click `Scan Universe` mid-session unless you specifically want to
   re-pick the whole watchlist.

The 5-minute recompute is what makes signals "fresh" — you don't need to click anything
for that to happen.

---

## 3. Pattern Scanner Matrix & Signal Interpretation

The dashboard renders a **Pattern Scanner Matrix** table above the stock cards. This table scans all 8 pattern families across your watchlist in real time:

1. **Opening Range Breakout / Breakdown (`ORB`)**
2. **Previous Day High / Low (`PDH_PDL`)**
3. **Breakout + Retest (`BREAKOUT_RETEST`)**
4. **VWAP Pullback (`VWAP_PULLBACK`)**
5. **EMA20 Pullback (`EMA20_PULLBACK`)**
6. **Failed ORB Reversal (`FAILED_ORB`)**
7. **Gap-and-Go (`GAP_AND_GO`)**
8. **Inside-Bar Breakout (`INSIDE_BAR`)**

### Understanding Pattern Agreement:
- **`HIGH` Agreement (3+ Patterns):** High-probability setup where multiple distinct technical patterns align in the same direction (e.g. `ORB` + `VWAP_PULLBACK` + `BREAKOUT_RETEST`).
- **`MEDIUM` Agreement (2 Patterns):** Strong setup with dual-pattern alignment.
- **`LOW` / `NEUTRAL` Agreement (0-1 Pattern):** Single-pattern setup; relies strictly on the primary hard gate.

---

## 4. Complete Inventory of All Data You Get

When monitoring stocks through the dashboard, engine, decision log, or MCP API tools, you receive:

| Category | Data Fields Included |
|----------|----------------------|
| **Quote & Session** | LTP, Open, High, Low, Close, `IN_SESSION` / `CLOSED` state, localized timestamp (`Asia/Kolkata`). |
| **Timeframe Trends** | Daily (1d) trend, 15m structure trend, 5m execution trend, timeframe alignment (`ALIGNED` / `CONFLICT`). |
| **Reference Levels** | Opening Range High (`ORH`), Opening Range Low (`ORL`), Previous Day High (`PDH`), Low (`PDL`), Close (`PDC`), Opening Gap %. |
| **Indicators & TA** | Session-only VWAP, time-of-day RVOL, RSI-14, ATR-14, EMA-20/50/200, Bollinger Bands. |
| **8-Pattern Engine** | Pattern ID, name, direction, status (`CONFIRMED`, `APPROACHING`, `WATCHING`), trigger price, distance %, confidence score, invalidation level, conditions, reasons. |
| **Pattern Agreement** | Primary pattern name, agreement count, agreement level (`HIGH` / `MEDIUM` / `LOW` / `NEUTRAL`). |
| **Hard-Gate Engine** | Decision signal (`IMMEDIATE_BUY`, `IMMEDIATE_SELL`, `ALMOST`, `WAIT`), BUY/SELL hard gates (`OPEN`/`CLOSED`), score (0-100), primary rejection reason if gate is closed. |
| **Trade Sizing & Levels** | Recommended Entry price, Stop Loss (SL), Targets (T1, T2, T3), Risk-to-Reward (RR), share quantity (`qty`), total capital required. |
| **Decision Audit Log** | Persistent JSONL record of every scan cycle saved to `decision_log.jsonl`. |

---

## 5. BUY execution flow (step by step)

When you see **`IMMEDIATE_BUY`** (or you were watching an `ALMOST_BUY` and the 5m candle
**closed above** `Confirm Level`):

1. **Confirm the gate is OPEN** and Daily/15m/5m trends are aligned BULLISH.
2. **Entry** = the level shown on the card (e.g. ₹2505, just above resistance).
3. **Stop-loss** = the card's SL (e.g. ₹2495, just below the breakout level).
4. **Targets** = T1 / T2 / T3 on the card. Scale out: book partial at T1, trail the rest.
5. **Risk per trade** = config `risk_per_trade_percent` (default 0.5% of capital). Size
   the quantity so that `(Entry − SL) × qty ≈ 0.5% of capital`.
6. **Place the order** in your broker terminal at/near the Entry, with a SL order.
7. **Manage**: once price hits T1, move SL to entry (risk-free). Trail to T2/T3.
8. **Exit by 15:15** at the latest (square-off), even if targets aren't hit.

> The engine is a **breakout** system: you buy *near a relative high expecting higher*,
> not at the bottom. If you want to buy at support, that's a different strategy not
> enabled here.

---

## 5. SELL / SHORT execution flow

Mirror of the BUY flow when you see **`IMMEDIATE_SELL`** (or an `ALMOST_SELL` whose 5m
candle **closed below** `Cancel Level` / support):

1. **Confirm SELL gate OPEN**, trends BEARISH.
2. **Entry** = breakdown level on the card.
3. **Stop-loss** = card's SL (above the breakdown).
4. **Targets** = T1/T2/T3 below.
5. Size with the same `risk_per_trade_percent` rule.
6. Place short order + SL in broker terminal.
7. Trail SL to entry after T1; square off by 15:15.

---

## 6. Risk management (enforced by the engine / config)

These limits are in `intraday_config.json` → `settings`. Respect them:

- `max_trades_per_day`: 10
- `max_open_positions`: 5
- `max_consecutive_losses`: 3 → **stop trading for the day after 3 losses in a row**
- `max_daily_loss_percent`: 3% → **stop trading for the day if down 3%**
- `max_sector_exposure`: 30% (don't overload one sector)
- `min_rr`: 2.0 (only take setups with risk/reward ≥ 2)
- `risk_per_trade_percent`: 0.5%

**Daily discipline checklist:**
- [ ] Pre-market: ran `Scan Universe` (or loaded your own list).
- [ ] Only take `IMMEDIATE_*` / `BUY_NOW` / `SELL_NOW` with OPEN gate.
- [ ] Every trade has a hard SL = card's SL.
- [ ] RR ≥ 2 on every entry.
- [ ] No more than 5 open positions, 10 trades/day.
- [ ] Stop after 3 consecutive losses OR 3% daily loss.
- [ ] Square off everything by 15:15.

---

## 7. End of day

- At **15:15** the engine expects all positions closed (`square_off_time`).
- After market close, cards return to `MARKET CLOSED / NOT EVALUATED`.
- Optionally re-run `Scan Universe` in the evening to prep tomorrow's watchlist
  (pre-open data will be from today's close — still valid context).

---

## 8. Optional: MCP server (for Claude / automation)

If you want to ask an AI assistant for a setup instead of the dashboard:

```bash
python3 intraday_server.py
```

Tools: `intraday_trade_setup` (one stock), `intraday_market_scanner` (whole universe),
`get_intraday_indicators`, `premarket_scanner`, `premarket_stock_analysis`. These return
the **same canonical record** as the dashboard — same decision, gates, and levels.

---

## 9. FAQ

**Q: Do I need to add stocks myself?**
A: No. Click `Scan Universe` before open and the system fills the watchlist from
pre-market data. You can also edit `intraday_config.json` manually if you prefer.

**Q: When do I click scan/refresh during the day?**
A: You usually don't need to — the dashboard auto-refreshes every 60s and recomputes
every 5 min. Click `Refresh Now` only to nudge an earlier reload. Don't click
`Scan Universe` mid-day (it re-picks the whole list).

**Q: The card says WAIT — what then?**
A: Do nothing. WAIT means no valid setup; the hard gate is closed.

**Q: BUY gate closed but score is 90?**
A: No trade. Gate is authoritative; score is only a ranking aid.

**Q: Market closed shows "NOT EVALUATED"?**
A: Expected. The engine only evaluates during market hours; no stale signals are shown.

**Q: Why only 20 stocks?**
A: `watchlist_size` in config (default 20). Change it or pass `--top N` to
`auto_select.py`.