# Intraday Trader — Setup & Run Guide

A one-page checklist to get the `intraday_trader` system running and place your
first intraday trade. This is the **operational** companion to `README.md`
(architecture) and `TRADING_GUIDE.md` (daily workflow). Read all three.

> The system is **analysis / decision-support only**. It never places orders.
> You execute buy/sell in your Angel One (or any) broker terminal.

---

## 0. Prerequisites

- Linux/macOS terminal with Python 3.10+.
- An Angel One trading account with **API credentials** (API key, client ID, PIN,
  TOTP/authenticator secret).
- This project folder: `/home/pandia/Desktop/intraday_trader`.

---

## 1. One-time setup

```bash
cd /home/pandia/Desktop/intraday_trader

# Create the virtual environment (only if venv/ does not exist)
python3 -m venv venv

# Install dependencies — websocket-client is REQUIRED (SmartApi won't import without it)
venv/bin/pip install -r requirements.txt

# Create your credential file
cp .env.example .env
```

Edit `.env` and fill in the real values:

```ini
ANGELONE_API_KEY=your_api_key
ANGELONE_CLIENT_ID=your_client_id
ANGELONE_PIN=your_pin
ANGELONE_TOTP_SECRET=your_totp_secret
INTRADAY_DATA_SOURCE=angelone
```

> `INTRADAY_DATA_SOURCE` must be exactly `angelone`. Any other value raises an
> error on purpose so the engine cannot silently switch providers.

---

## 2. Verify data is flowing (no `data_missing`)
## 3. Start the dashboard (do this every morning)

```bash
cd /home/pandia/Desktop/intraday_trader
venv/bin/python dashboard.py --config intraday_config.json --port 8080
```

Open **http://localhost:8080** in your browser. Leave it running all day.
It auto-refreshes every 60s and the engine recomputes signals every 5 minutes.

---

## 4. Build the watchlist (before market open, ~9:00–9:15)

1. In the dashboard, click **`🔄 Scan Universe`**.
2. The system scores the NIFTY100 universe by liquidity + previous-day trend +
   relative strength + gap quality and writes the **top 20** into
   `intraday_config.json` automatically.
3. The page reloads and shows the live engine cards plus a Candidate Quality table.

Optional CLI alternative:
```bash
venv/bin/python auto_select.py --config intraday_config.json --top 20
```

> Do **not** click `Scan Universe` repeatedly during the day — it replaces your
> current 20-stock list. Run it once at open (or the night before).

---

## 5. Place an intraday trade (during market hours)

Market hours: **09:15–15:30 IST**. Engine entry window: **09:45–14:45**.
Square-off everything by **15:15**.

When a card shows **`IMMEDIATE_BUY`** (or `ALMOST_BUY` → 5m candle closes above
`Confirm Level`):

1. **Confirm the gate is OPEN** and Daily / 15m / 5m trends are aligned BULLISH.
2. **Entry** = the level on the card (just above resistance — this is a breakout system).
3. **Stop-loss** = the card's SL (just below resistance/support).
4. **Targets** = T1 / T2 / T3 on the card.
5. **Size** so that `(Entry − SL) × qty ≈ risk_per_trade_percent` of capital (default 0.5%).
6. **Place the order** in your broker terminal at/near Entry, with a hard SL order.
7. Once price hits T1, move SL to entry (risk-free). Trail to T2 / T3.
8. **Exit by 15:15** at the latest, even if targets aren't hit.

SELL/SHORT is the mirror image when you see **`IMMEDIATE_SELL`**.

> **The hard gate is the boss.** If BUY Gate = CLOSED there is **no trade**, no
> matter how high the score. Only act when the gate is OPEN *and* the signal is
> actionable (`IMMEDIATE_*` / `BUY_NOW` / `SELL_NOW`).

---

## 6. Daily risk discipline

- [ ] Ran `Scan Universe` before open.
- [ ] Only took `IMMEDIATE_*` / `BUY_NOW` / `SELL_NOW` with OPEN gate.
- [ ] Every trade has a hard SL = card's SL.
- [ ] Risk/reward ≥ 2 on every entry.
- [ ] ≤ 5 open positions, ≤ 10 trades/day.
- [ ] Stop after 3 consecutive losses OR 3% daily loss.
- [ ] Square off everything by 15:15.

---

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `SmartApi` import error / all timeframes `data_missing` | `websocket-client` not installed | `venv/bin/pip install -r requirements.txt` |
| 5m shows `0` candles but daily/15m OK | Angel One returns empty for very short 5m windows | Already fixed via broker fallback; ensure `brokers/angelone.py` fallback is intact |
| `CERTIFICATE_VERIFY_FAILED` | Angel One SSL | Already handled (`verify=False`); do not remove |
| `ValueError: Angel One` on startup | `INTRADAY_DATA_SOURCE` ≠ `angelone` | Set `INTRADAY_DATA_SOURCE=angelone` in `.env` |
| Cards show `MARKET CLOSED / NOT EVALUATED` | Outside market hours | Expected; signals only compute 09:15–15:30 IST |
| `last_price` is `None` in quote | Market closed / symbol not traded | Expected off-hours; retest in session |
| Dashboard prints `No stocks configured` and exits (old behavior) / shows "No stocks configured" page (new) | `intraday_config.json` → `stocks` is empty `[]` | Run `venv/bin/python auto_select.py --config intraday_config.json --top 20` (writes 20 symbols), OR click **Scan Universe** in the dashboard, OR add `{"symbol": "RELIANCE"}` entries manually. The dashboard no longer hard-exits on empty stocks. |
| `Scan Universe` writes 0 stocks / fails | NIFTY master unreachable + no cached fallback | `auto_select.py` falls back to a curated 49-name universe automatically; just re-run it. |
| `15m_data_missing` / `5m_data_missing` / `daily_data_missing` cards during **live** market | Angel One throttles the rapid burst of dashboard calls and returns genuine `error:True` (not just empty). Old code cached the error for 30s and only fell back on empty-no-error, so it cascaded. | **Fixed:** `shared.DataProvider.get_ohlcv` now retries 4× with escalating back-off, never caches error results, and `brokers/angelone.py` swallows throttling errors and falls back across periods (daily prefers *longer* windows). If a card still shows `DATA_INSUFFICIENT`, it is a transient throttle — the next 300s refresh usually clears it. |
| `'>=' not supported between instances of 'NoneType' and 'float'` / dashboard crash mid-scan | Engine compared `rvol` (which can be `None` pre-volume-build) against a float without a guard | **Fixed:** `engine.py` volume block now guards `if rvol is not None and rvol >= ...` (matching the RSI guard). Re-validate with `venv/bin/python -m pytest tests/`. |
| One card shows `insufficient_daily_candles` | Daily fetch throttled → broker fell back to a *short* `5d` window (~3 candles) | **Fixed:** daily interval now falls back to *longer* windows (`1y/6mo/3mo`), not shorter. If it recurs, it is transient throttling on that one symbol — refresh clears it. |

---

## 8. Optional: MCP server (for AI assistant / automation)

```bash
venv/bin/python intraday_server.py
```

Exposes `intraday_trade_setup`, `intraday_market_scanner`, `get_intraday_indicators`,
`premarket_scanner`, `premarket_stock_analysis` — same canonical records as the dashboard.

---

## File map

| File | Purpose |
|------|---------|
| `engine.py` | Hard-gate trading engine (single source of truth) |
| `patterns.py` | 8-Pattern entry engine + `PatternStateStore` state persistence |
| `shared.py` | `DataProvider` + `Indicators` (Angel One data fetch + TA) |
| `dashboard.py` | Web UI (scanner + cards + Pattern Scanner Matrix) |
| `decision_log.py` | Audit decision logger (`decision_log.jsonl`) |
| `intraday_server.py` | MCP server (stdio, 5 tools) |
| `auto_select.py` | Pre-market watchlist builder |
| `intraday_config.json` | Settings + stock universe |
| `.env` | Angel One credentials (never commit) |
| `requirements.txt` | Python dependencies (incl. `websocket-client`) |
| `tests/test_components.py` | Offline tests (run with `pytest`) |


Run this from the project folder. It checks the live quote + all three candle
timeframes against Angel One:

```bash
venv/bin/python -c "
import sys; sys.path.insert(0,'.')
import shared
dp = shared.DataProvider()
q = dp.get_realtime_quote('RELIANCE')
print('quote last_price :', q.get('last_price'))
specs = [('daily','1d','1d'),('15m','5d','15m'),('5m','5d','5m')]
for label, period, interval in specs:
    c = dp.get_ohlcv('RELIANCE', period, interval, 'nse', 75).get('candles', [])
    print('%s candles : %d' % (label, len(c)))
"
```

✅ **Expected:** `last_price` is a number, and all three candle counts are `> 0`.
❌ **If you see `0` candles or `data_missing`:** Angel One returned empty data —
check credentials / market hours, or re-run `pip install -r requirements.txt`.

You can also run the offline test suite (no network needed):

```bash
venv/bin/pip install pytest
venv/bin/python -m pytest tests/ -q
```
