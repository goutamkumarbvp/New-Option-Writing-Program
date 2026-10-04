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
- **Automatic instrument master.** Kotak publishes a new scrip master every day under a dated path. With `KOTAK_API_KEY` set, the terminal asks Kotak for the day's file paths, loads the `KOTAK_SCRIP_SEGMENTS` files together and checks again every `INSTRUMENT_REFRESH_CHECK_SEC`. This needs only the consumer key, not a login.
  - Freshness follows the files' own date. Yesterday's files load only when nothing is loaded yet, and never count as today's, so orders stay blocked with `INSTRUMENT_MASTER_STALE` until today's files are in.
  - A load is all or nothing. A failed fetch keeps the current index and retries after 60 s, 120 s … up to an hour.
  - Parsing runs in a worker thread, so a 100k-row file does not stall live ticks.
  - `POST /instruments/refresh/KOTAK` with the operator token loads now. Status is in the System tab under `instrument_loader`. Set `KOTAK_INSTRUMENT_MASTER_AUTO=false` to turn it off.
- **Index streaming.** Subscribe indices by name on a cash segment in `SUBSCRIPTION_JSON`, for example `"nse_cm|Nifty 50"`, `"nse_cm|Nifty Bank"` and `"bse_cm|SENSEX"`. Numeric tokens stay instrument subscriptions.
  - Index ticks carry the F&O underlying (Nifty 50 → NIFTY, Nifty Bank → BANKNIFTY, Nifty Fin Service → FINNIFTY, Nifty Mid Select → MIDCPNIFTY, Nifty Next 50 → NIFTYNXT50, SENSEX, BANKEX). Add others with `KOTAK_INDEX_UNDERLYINGS_JSON`.
  - The Option Chain takes a live index or cash tick of the underlying as spot automatically, so `UNDERLYING_SPOT_TOKENS_JSON` is optional. It can still name the index, as `{"NIFTY": {"KOTAK": "Nifty 50"}}`, which the scenario-risk gate needs.
  - Index ticks have no bid or ask and keep the feed's own timestamp. A tick without one is rejected by the quality gate, never stamped with local time.
- The SDK writes `logs/neo-api-client.log` in the working directory, including mobile number and client code. `logs/` is gitignored.

## Credentialed test
Keep `LIVE_TRADING=false` and run:
`python scripts/credentialed_live_certification.py`

The script is read-only. It does not place, modify or cancel orders.

## Live-money gate
The final gate still requires controlled live evidence: real feed, real instrument master, broker reconciliation, margin evidence, small-size order acknowledgement, fill/rejection/cancel lifecycle, emergency exit, kill-switch test, recovery test and operator sign-off.
