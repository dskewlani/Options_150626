# Options Trader Pro 📈

A professional-grade Streamlit options trading dashboard for NSE/BSE markets.  
Powered by **Angel One SmartAPI WebSocket** for live tick data.

---

## Project structure

```
trading_app/
├── app.py            ← Streamlit dashboard (main entry point)
├── angel_api.py      ← Angel One REST + WebSocket handler
├── engine.py         ← Signal engine, Greeks, Max Pain, consensus scoring
├── storage.py        ← SQLite trade log, P&L history, settings
├── requirements.txt
├── .env.example      ← Copy to .env and fill credentials
└── README.md
```

---

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure credentials
```bash
cp .env.example .env
```
Edit `.env`:
```
ANGEL_API_KEY=your_api_key
ANGEL_CLIENT_ID=your_client_id
ANGEL_PASSWORD=your_password
ANGEL_TOTP_SECRET=your_totp_secret
```

### 3. Run
```bash
streamlit run app.py
```

---

## How the WebSocket works

1. On app startup, `start_websocket()` launches a **daemon thread** that connects to Angel One `SmartWebSocketV2`.
2. The index token (NIFTY=26000, BANKNIFTY=26009, FINNIFTY=26037) and India VIX token are subscribed immediately.
3. Enter an **expiry date** in the sidebar (e.g. `26JUN2025`) — the app resolves NFO tokens for all ±N strikes around ATM via REST, then subscribes them to the live WebSocket.
4. Every tick fires `_on_data()` which writes into `_tick_store` (thread-safe dict).
5. On every Streamlit refresh cycle, `refresh_market_data()` reads from `_tick_store` — **no polling, no REST for prices**.
6. If a WebSocket tick isn't available yet (market closed / startup), the app falls back to REST LTP → simulation, so the UI always shows data.
7. **Auto-reconnect**: on disconnect or error the thread waits with exponential backoff (max 60s) and reconnects automatically up to 10 times.

---

## Dashboard sections

| Section | What's live |
|---------|-------------|
| KPI bar | Index LTP, VIX — both from WS ticks |
| Signal engine | RSI, MACD, Supertrend, VWAP, PCR, OI Δ, IV Rank, Bollinger |
| Consensus panel | Weighted score 0–100 → Strong Buy / Buy / Avoid / Sell / Strong Sell |
| Option chain | CE/PE LTP from WS ticks (🔴 marker); falls back to synthetic if token not subscribed |
| ATM Greeks | Black-Scholes: Delta, Gamma, Theta, Vega, IV, Break-even |
| P&L curve | Session P&L with max-loss line |
| Open positions | MTM from live WS tick where token is subscribed |
| Trade logger | Confirmation modal → SQLite storage |

---

## Deploying to Streamlit Cloud

Put credentials in **Settings → Secrets**:
```toml
ANGEL_API_KEY = "..."
ANGEL_CLIENT_ID = "..."
ANGEL_PASSWORD = "..."
ANGEL_TOTP_SECRET = "..."
```

---

## Important notes

- **Never commit `.env`** — it's in `.gitignore`.
- The TOTP secret rotates Angel One's 2FA — treat it like a password.
- Rotate your Angel One API key periodically from the SmartAPI portal.
- Paper-trade the signals for at least 2 weeks before live execution.
