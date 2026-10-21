# START HERE — Simple Guide

This is your plain-language guide: what to do, how to read the dashboard, and
what to keep in mind. No jargon. Follow it top to bottom.

> **Golden rule:** this system is a decision helper. It does NOT place orders.
> You read it, you decide, you place the trade yourself in your broker app.

---

## PART 1 — One-time setup (do this once)

You only do this the very first time.

### 1. Open a terminal in the project folder
Open PowerShell and go to the project folder:
```
cd "C:\Users\pandiya.qburst\Downloads\claude check\claude check"
```

### 2. Make sure Python packages are installed
The project uses a `venv` folder (a private Python with the right packages).
If it already exists, skip this. If not, run:
```
python -m venv venv
venv\Scripts\python -m pip install -r requirements.txt
```
If you see an SSL / certificate error during install, use this instead:
```
venv\Scripts\python -m pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org -r requirements.txt
```

### 3. Put in your Angel One login details
Open the file named `.env` (if it's missing, copy `.env.example` and rename the
copy to `.env`). Fill in your real values:
```
ANGELONE_API_KEY=your key
ANGELONE_CLIENT_ID=your client id
ANGELONE_PIN=your pin
ANGELONE_TOTP_SECRET=your TOTP secret
INTRADAY_DATA_SOURCE=angelone
```
Keep this file private. Never share it.

---

## PART 2 — Your daily routine

### Step A — Before the market opens (do it the night before or by ~9:00 AM)

Build today's watchlist (the stocks the system will watch):
```
venv\Scripts\python auto_select.py --config intraday_config.json --top 15
```
This picks the best stocks for the day and saves them into the config file.

### Step B — Start the dashboard
```
venv\Scripts\python dashboard.py --config intraday_config.json --port 8080
```
Then open your web browser and go to:
```
http://localhost:8080
```
Leave this running during market hours. It refreshes on its own.

### Step C — Watch the dashboard during the day
The entry window is **9:45 AM to 2:30 PM**. Before 9:45 the system avoids
signals on purpose (the first 30 minutes are fake-move prone). All intraday
positions should be closed by **3:15 PM**.

### Step D — When you take a trade
If you decide to buy/sell based on a card, tell the system so it can watch it
for you and alert you when to exit. You do this from the dashboard's positions
area (enter the symbol, your entry price, and quantity).

### Step E — End of day
Close any open positions by 3:15 PM. Stop the dashboard (close the terminal or
press Ctrl+C in it).

---

## PART 3 — How to read the dashboard (the UI)

Each stock shows a **card**. Read a card top to bottom:

### 1. The headline — the decision
Big label at the top of the card:

| Label | What it means | What you do |
|-------|---------------|-------------|
| **IMMEDIATE_BUY** | All checks passed, buy setup is live NOW | Strongest buy signal. Consider entering. |
| **IMMEDIATE_SELL** | All checks passed, sell/short setup is live NOW | Strongest sell signal. |
| **BUY_WATCH / SELL_WATCH** | Strong setup, just waiting for the breakout | Get ready, not yet. |
| **ALMOST_BUY / ALMOST_SELL** | Close, but missing the final trigger | Keep watching. |
| **WAIT** | No valid setup right now | Do nothing. |
| **CANCEL** | A setup that was forming has broken down | Drop it. |

**Only IMMEDIATE_BUY / IMMEDIATE_SELL are real "act now" signals.** Everything
else is "watch" or "wait".

### 2. The hard-gate / checklist block
A list of checks with ✓ (passed) or ✗ (not met). This shows WHY the decision is
what it is. If the decision is WAIT, the ✗ items tell you what's blocking it.
**A closed gate means NO trade, no matter how good the score looks.**

### 3. The score (0–100)
Shown as a colored bar. Higher = stronger setup. But the score is secondary —
the checklist/gate above it is what really decides. A high score with a failed
gate is still a NO.

### 4. Trade levels
- **Entry** — the price to get in
- **Stop loss** — where you exit if wrong (your safety line)
- **Target 1 / 2 / 3** — where to book profit
- **Risk/Reward** — how much you can gain vs lose; higher is better

### 5. Trend and technicals (supporting evidence)
- **Daily / 15m / 5m trend** — the big picture down to the entry timeframe. You
  want these pointing the same way as your trade.
- **VWAP** — above VWAP = bullish, below = bearish.
- **RVOL** — how busy the stock is vs normal. Higher = more conviction.
- **RSI, ATR** — momentum and volatility readings.

### 6. "Best Available" panel (context only)
If NOTHING qualifies for a real signal, the dashboard shows the single closest
BUY and SELL candidate, clearly labeled **"context only, NOT a trade signal"**,
with the reason it's not a full signal. This is just so your screen is never
blank — it is **not** a recommendation to trade. Treat it as "here's the least
far-off idea, decide for yourself."

### 7. Scan Diagnostic Funnel (bottom)
Shows how many stocks passed each check. Useful to understand why few/many
signals appeared today. Just information.

---

## PART 4 — What to consider (important mindset)

1. **No trade is a valid outcome.** Some days have no good setup. Sitting out is
   often the most profitable choice. Don't force a trade because you're bored.

2. **The gate beats the score.** If the checklist shows a ✗ on a required check,
   it's a NO even if the score is 80+.

3. **Always use the stop loss.** The system gives you one for a reason. Decide
   your exit before you enter, and respect it.

4. **"Best Available" is not a signal.** It's context so the screen isn't empty.
   Don't trade it just because it's the only thing showing.

5. **Follow the clock.** No entries before 9:45 AM or after 2:30 PM. Everything
   closed by 3:15 PM. Intraday means intraday.

6. **Discipline limits exist to protect you.** The system caps trades per day and
   stops after a losing streak or daily loss limit. These prevent revenge
   trading, which is what wrecks most intraday accounts. Don't override them.

7. **The system can't see news.** A surprise announcement can move a stock
   against any technical setup. Be aware of events on your stocks.

---

## PART 5 — Want more BUY/SELL calls? (optional, advanced)

Right now the system is in **strict** mode — very few but very clean signals.
There is a looser **tiered** mode that produces more calls. BUT do not just
switch it on blindly — more trades can mean less profit. First PROVE it helps,
using the backtest.

### How to prove it (the backtest)

1. **Download past data** (needs your Angel One login, run once):
   ```
   venv\Scripts\python -m backtest.history --config intraday_config.json --months 6
   ```
2. **Test the current settings:**
   ```
   venv\Scripts\python -m backtest.run --config intraday_config.json
   ```
   Look at the result:
   - **expectancy_r** — the key number. Positive = made money per trade.
     Negative = lost money (don't trade this).
   - **profit_factor** — above 1 is good; higher is better.
   - **trades_per_day** — how often it would trade.

3. **Compare strict vs looser settings automatically:**
   ```
   venv\Scripts\python -m backtest.sweep --config intraday_config.json
   ```
   It prints a table of options and a **RECOMMENDATION** line telling you which
   settings make the most money while still giving around one trade a day. If it
   says no option works, that's an honest "don't loosen it" — believe it.

4. **If the sweep recommends tiered mode**, open `intraday_config.json` and
   change:
   ```
   "gate_mode": "strict"
   ```
   to
   ```
   "gate_mode": "tiered"
   ```
   Save, then run the dashboard again. You'll get more signals. Watch them on
   paper for a couple of weeks before trusting real money.

Full details are in `backtest/TUNING_GUIDE.md`.

> **Honest note:** a backtest shows how settings would have done on PAST data.
> It is a strong hint, not a promise. Always paper-test a change for a while
> before risking real money.

---

## PART 6 — Quick troubleshooting

| Problem | Fix |
|---------|-----|
| Dashboard shows no data / "data missing" | Check your `.env` login details are correct and you have internet. |
| "No module named ..." error | Run the install step in Part 1 again (use the `--trusted-host` version if SSL error). |
| Backtest says "no cached history" | Run the `backtest.history` download step in Part 5 first. |
| Few or no signals all day | Normal in a quiet/choppy market. Check the "Best Available" panel for context. Consider the tiered-mode test in Part 5. |
| Page won't open | Make sure the dashboard terminal is still running, and you used the right port (8080). |

---

## One-line summary

Run `auto_select.py` in the morning → start `dashboard.py` → watch for
**IMMEDIATE_BUY / IMMEDIATE_SELL** cards during 9:45 AM–2:30 PM → respect the
stop loss and daily limits → close everything by 3:15 PM. To get more signals,
prove it pays off first with the backtest in Part 5.
