# Live data and paper-risk extension

The existing hard-gate intraday strategy and MCP output format are unchanged.
This project uses **Angel One as the sole market-data provider**. There is no
yfinance fallback. The broker adapter is **read-only** and contains **no
order-placement method**.

## Required setup

Install dependencies in the project virtual environment, copy the env template,
and export credentials through your shell or a secrets manager. Never commit `.env`.

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
# export the values in .env through your shell / secrets manager
export INTRADAY_DATA_SOURCE=angelone
export ANGELONE_API_KEY='...'
export ANGELONE_CLIENT_ID='...'
export ANGELONE_PIN='...'
export ANGELONE_TOTP_SECRET='...'
```

`INTRADAY_DATA_SOURCE` must be `angelone`. Any other value raises an error so
the engine cannot silently switch providers.

The adapter logs in lazily, resolves NSE/BSE equity tokens from Angel One’s
instrument master, retrieves quotes with `ltpData`, and requests historical
OHLCV through `getCandleData`. Strategy code still consumes the same normalized
`quote` and `candles` contracts supplied by `DataProvider`. Symbols are stored
and displayed as bare names (`RELIANCE`, not `RELIANCE.NS`).

## Operational guardrails

- Use Angel One credentials only in environment variables or a secret manager.
- Treat signals as analysis/paper-trading information; validate market data and order execution independently.
- `risk.py` limits paper sizing by per-trade loss, daily loss, maximum position value, and position count. It does **not** send broker orders.
- `market_data.py` can aggregate incoming ticks into bounded local five-minute candles when a WebSocket feed is added later. The current broker adapter intentionally uses REST candles to avoid duplicate/incomplete streams.

## Offline verification

```bash
python3 -m pytest -q tests
```

These tests do not require broker credentials or network access.
