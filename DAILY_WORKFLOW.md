# Daily Workflow — How to Use This App Every Day

A simple, repeatable routine. Follow it top to bottom each trading day.

> Reminder: this app helps you decide. It does NOT place orders. You read the
> signal, you decide, you place the order yourself in your broker app.

---

## PART 1 — Before the market opens (do this by ~9:00 AM)

### Step 1: Open a terminal in the project folder
Open PowerShell and run:
```
cd "C:\Users\pandiya.qburst\Downloads\claude check\claude check"
```

### Step 2: Build today's watchlist
This picks the best stocks to watch today and saves them into your config:
```
venv\Scripts\python auto_select.py --config intraday_config.json --top 15
```
Wait for it to finish (it prints the chosen stocks).

### Step 3: Start the dashboard
```
venv\Scripts\python dashboard.py --config intraday_config.json --port 8080
```
Leave this terminal running the whole day.

### Step 4: Open the dashboard in your browser
Go to:
```
http://localhost:8080
```
Leave this tab open. It refreshes itself about every 60 seconds.

### Step 5: Read the pre-market picture (quick look)
- Check the **market status** at the top (open/closed).
- Note the overall market direction (Nifty / Bank Nifty) if shown.
- Glance at the watchlist cards to see which stocks look like they're setting up.
- **Do nothing yet.** No entries before 9:45 AM — the first 30 minutes are
  fake-move prone and the app holds signals back on purpose.

That's it for pre-market. Now you wait and watch.

---

## PART 2 — During the market: how to take a BUY or SELL entry

The entry window is **9:45 AM to 2:30 PM**. Here's the exact decision flow.

### Step 1: Watch for a real signal
On a stock card, look at the big label at the top:

- **IMMEDIATE_BUY** or **IMMEDIATE_SELL** -> this is a real "act now" signal.
- **BUY_WATCH / SELL_WATCH / ALMOST_*** -> getting close, keep watching, don't act yet.
- **WAIT** -> do nothing.
- **EARLY HEADS-UP** -> a possible turn forming early. This is LOWER confidence.
  Use it only to start watching that stock — do NOT enter on it alone.

**Only act on IMMEDIATE_BUY / IMMEDIATE_SELL.**

### Step 2: Confirm the setup is clean (10-second check)
On that card, confirm:
- The **checklist/gate block** shows all pass (no cross on a required check).
- **Daily, 15m, 5m trends** point the same way as your trade.
- Price is on the right side of **VWAP** (above for BUY, below for SELL).
- **Risk/Reward** is good (about 2:1 or better).

If any required check fails, skip it — a closed gate means no trade, no matter
how high the score looks.

### Step 3: Note the trade levels from the card
Write these down (the card gives them to you):
- **Entry** price
- **Stop loss** price
- **Target 1 / Target 2 / Target 3**

### Step 4: Work out your position size (do NOT skip this)
Risk a small fixed amount per trade — the app default is 0.5% of your account.

```
shares = (account size x risk%) / (entry price - stop price)
```
Example: 1,00,000 account, risk 0.5% = 500. Entry 500, stop 495 (5 risk).
Shares = 500 / 5 = 100 shares.

This way one bad trade can never badly hurt your account.

### Step 5: Place the order in YOUR broker app
The dashboard does not place orders. Go to your broker (Zerodha, Angel One app,
etc.) and place:
- A **buy** (for IMMEDIATE_BUY) or **sell/short** (for IMMEDIATE_SELL) order at
  the entry price, with your calculated quantity.
- A **stop-loss order** at the stop price immediately. Never trade without the
  stop in place.

### Step 6: Tell the app you entered (so it watches the trade for you)
Back on the dashboard, in the **My Positions** section:
- Enter the **symbol**, **direction** (BUY/SELL), your **entry price**, and
  **quantity** -> click **Add Position**.
- Now the app watches this trade and alerts you (banner + sound) when to act.

---

## PART 3 — Managing the trade after you're in

The app will alert you, but here's the plan so you know what the alerts mean:

1. **At Target 1 hit:** book about half, and move your stop up to your entry
   price (breakeven). Now the trade can't become a loss. (The app flags this.)
2. **Let the rest run** toward Target 2 / Target 3.
3. **If the stop is hit:** exit. No "just a little more." Respect the stop.
4. **"Momentum fading" / "VWAP lost" alert while in profit:** consider booking
   early — the move may be ending.
5. **"Time stop" alert:** the trade went nowhere for ~30 min — exit, it likely
   failed.
6. **Square off everything by 3:15 PM.** Never carry an intraday trade overnight.

When you exit in your broker app, click **Close** on that position in the
dashboard so it stops tracking it.

---

## PART 4 — If you see no signals all day

That's normal, especially in a quiet/choppy market. Options:
- Check the **"Best Available"** panel — it shows the closest BUY/SELL candidate,
  clearly labeled **context only, NOT a trade signal**. It's just so your screen
  isn't blank. Do not trade it just because it's the only thing showing.
- Accept that **no trade is a valid, often profitable choice.** Don't force one.
- If you consistently get too few signals and want more, run the **Run
  Backtest** button to see whether switching to looser ("tiered") settings keeps
  a positive edge. Only switch if the backtest's RECOMMENDATION supports it.

---

## PART 5 — End of day

1. Make sure all positions are closed by 3:15 PM.
2. Click **Close** on any remaining tracked positions in the dashboard.
3. Stop the dashboard (press Ctrl+C in its terminal, or just close the window).

---

## The daily rhythm in one line

**Morning:** build watchlist -> start dashboard -> open browser -> wait.
**9:45 AM-2:30 PM:** act only on IMMEDIATE_BUY / IMMEDIATE_SELL -> confirm gate
-> size your position -> place order + stop in your broker -> add it to My
Positions.
**In the trade:** follow the alerts, respect the stop, book at targets.
**By 3:15 PM:** close everything.

---

## Golden rules (never break these)

1. Only act on **IMMEDIATE** signals (not WATCH / ALMOST / EARLY HEADS-UP).
2. **Always place the stop loss** at the same time as the entry.
3. **Size small and fixed** (0.5%-1% risk per trade). Never oversize.
4. **No entries** before 9:45 AM or after 2:30 PM. **Close all by 3:15 PM.**
5. If the app's **daily limit** blocks you (max trades, loss limit, losing
   streak), **stop for the day.** That rule exists to protect you.
6. **No trade is a valid outcome.** Don't force a trade out of boredom.
7. The app **can't see news** — be aware of events on your stocks.
