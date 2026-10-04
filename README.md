# IORT V3.0.0 — Institutional Live-Hardening Build

V2.9 adds adversarial live-execution hardening, persistent kill state, authenticated operator controls, emergency open-order cancellation, strict AUTO gating, broker-trade-ID fill idempotency, terminal order-state protection, live-price exposure evidence, and production container hardening. **Not live-money certified until real-account controlled evidence is completed.**

# Institutional Options Risk Terminal V3.0.0

## Status
**V3.0.0 — Institutional Live-Hardening / Production-Preparation Candidate**

This release is deliberately fail-closed. It is **not** a profitability guarantee, exchange certification, or claim of commercial equivalence to Bloomberg, Trading Technologies, FlexTrade, or Nasdaq SMARTS.

## Architecture
NSE / BSE / MCX
→ multi-broker live feeds
→ canonical Tick
→ data quality / staleness
→ option chain / OI / analytics
→ evidence-based AI agents
→ strategy
→ approval
→ dual independent risk
→ kill switch / watchdog
→ broker execution
→ order lifecycle
→ PostgreSQL ledger
→ reconciliation
→ TCA / audit / surveillance modules
→ dashboard

## V2.7 hardening
- Redis Streams durable event persistence in live mode.
- PostgreSQL required for live mode.
- Broker-authoritative positions and P&L before order submission.
- Broker margin evidence required by default for live mode.
- Instrument-master token gate.
- Target broker authentication + broker-specific live-feed gate.
- Pre-trade quantity/value/exposure/loss limits.
- Hard portfolio stop triggers the kill switch.
- Kotak canonical NSE/BSE/MCX segment mapping.
- Multi-venue stream subscription syntax for NSE/BSE/MCX.
- Official Upstox V3 SDK/protobuf path.
- Dashboard explicitly reports DATA UNAVAILABLE / durable-bus state.
- Live credentials never belong in the ZIP.

## Option Chain tab
The web terminal's **Option Chain** tab shows a strike ladder per underlying and expiry: calls left, strikes centre, puts right, with OI bars, OI change, volume, IV, Greeks, PCR, max pain, ATM straddle, ATM highlight, ITM shading and a spot marker.

- Data comes only from live, instrument-master-enriched broker ticks. With no ticks it shows DATA UNAVAILABLE.
- Spot comes from `UNDERLYING_SPOT_TOKENS_JSON` (`{"NIFTY": {"ANGEL": "26000"}}`). Without it, spot is a put-call parity estimate and is labelled as such. With neither, ATM, model IV and Greeks are left blank.
- IV and Greeks are Black-Scholes values from the quote mid. OI change counts from the first tick the terminal saw today.
- Clicking a **Bid** opens a SELL (write) ticket and an **Ask** opens a BUY ticket. Tickets submit to `POST /orders` with the row's explicit broker and a client order ID, so every pre-trade control, the kill switch and operator auth still apply. Paper mode refuses them with `LIVE_TRADING_DISABLED`.
- API: `GET /option-chain` lists chains and order policy; `GET /option-chain/{exchange}/{underlying}/{expiry}/ladder?depth=N` returns the ladder.

## Kotak Neo notes
- **Login backoff.** A failed Kotak login waits 30, 60, 120 … seconds (capped at 15 minutes) before the next attempt, however many components ask for a session. Network, proxy, timeout and server errors only back off.
- **Lockout protection.** When Kotak itself rejects the credentials twice in a row, automatic login halts and the dashboard shows `LOGIN_HALTED`. Fix the credentials, then press **Reset KOTAK login** on the Overview tab or call `POST /brokers/KOTAK/login-reset` with the operator token. Tune with `KOTAK_LOGIN_BACKOFF_SEC`, `KOTAK_LOGIN_BACKOFF_MAX_SEC` and `KOTAK_LOGIN_MAX_REJECTIONS`.
- **Expiry decoding.** `pExpiryDate` follows the official SDK rule: NSE F&O adds 315511200 seconds, BSE F&O and MCX are plain Unix seconds. A decoded date must be plausible and must match the trading symbol's month or weekly date; otherwise expiry stays empty and option gates fail closed. This lets Kotak option ticks populate the Option Chain tab.
- The SDK writes `logs/neo-api-client.log` in the working directory, including mobile number and client code. `logs/` is gitignored.

## Credentialed test
Keep `LIVE_TRADING=false` and run:
`python scripts/credentialed_live_certification.py`

The script is read-only. It does not place, modify or cancel orders.

## Live-money gate
The final gate still requires controlled live evidence: real feed, real instrument master, broker reconciliation, margin evidence, small-size order acknowledgement, fill/rejection/cancel lifecycle, emergency exit, kill-switch test, recovery test and operator sign-off.
