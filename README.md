# Institutional Options Risk Terminal (IORT) 3.2.0

An options risk and option-writing terminal for NSE, BSE and MCX: multi-broker live feeds, a live option chain, portfolio risk with Greeks and full-revaluation scenarios, a strategy builder for hedged option writing, and a pre-trade risk engine that every order passes through.

## Status
**3.2.0: production-preparation candidate.** The build is deliberately fail-closed: missing evidence blocks orders instead of guessing. `LIVE_TRADING` defaults to false, and live money still needs the controlled evidence listed under *Live-money gate*. It is not a profitability guarantee, an exchange certification, or a claim of equivalence to commercial terminals.

## What is new in 3.2.0
- **Option Chain:** strike ladder with OI, OI change, IV and Greeks, ATM/ITM/spot markers, OI and OI-change charts, IV smile, and a click-to-trade ticket.
- **Portfolio:** broker-authoritative positions with model IV and Greeks, firm totals in rupees, payoff curves and a spot × vol scenario heatmap.
- **Strategy builder:** short straddles, strangles, iron condors and flies, and credit spreads picked from the live chain by delta. It shows exact max profit and loss, breakevens, probability of profit, margin estimate, and hedge-first execution.
- **Kotak Neo:** login backoff with lockout halt, daily automatic instrument master, expiry decoding, index streaming, and option-chain auto-subscription around spot.
- **Operations:** Prometheus `/metrics`, and a daily refresh of fixed-URL instrument masters.
- **Terminal UI:** modular frontend under a strict Content-Security-Policy, with IST clock and NSE/MCX session state, feed latency, toasts, the Ctrl+K command palette and keyboard shortcuts.

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
- Spot is a live index or cash tick of the underlying (for example a streamed Kotak "Nifty 50"), or the token named in `UNDERLYING_SPOT_TOKENS_JSON`. Without either, spot is a labelled put-call parity estimate. With none, ATM, model IV and Greeks are left blank.
- Charts under the ladder show open interest and OI change by strike (calls vs puts, ATM highlighted) and the IV smile with a spot marker. Every chart has a hover and keyboard readout; the ladder is its table view.
- IV and Greeks are Black-Scholes values from the quote mid. OI change counts from the first tick the terminal saw today.
- Clicking a **Bid** opens a SELL (write) ticket and an **Ask** opens a BUY ticket. Tickets submit to `POST /orders` with the row's explicit broker and a client order ID, so every pre-trade control, the kill switch and operator auth still apply. Paper mode refuses them with `LIVE_TRADING_DISABLED`.
- API: `GET /option-chain` lists chains and order policy; `GET /option-chain/{exchange}/{underlying}/{expiry}/ladder?depth=N` returns the ladder.

## Portfolio tab
`GET /risk/portfolio` and the **Portfolio** tab value every open position from the broker snapshots, never from the terminal's own records.
- Each position gets a model IV and position Greeks against a live spot.
- Totals are in rupees: delta notional, P&L from a 1% move through gamma, theta per day, vega per vol point, and total and day P&L.
- A spot × vol grid re-prices every leg with Black-Scholes, over spot −7% to +7% and vol −5 to +10 points.
- Payoff curves per underlying show P&L at the nearest expiry and today.
- A position that cannot be priced is listed with the reason, marks totals incomplete, and makes the scenario grid fail closed.

## Strategy builder
The **Strategy** tab and `GET /strategy/template/{exchange}/{underlying}/{expiry}/{name}` build structures from the live chain.
- Templates: short straddle, short strangle, iron condor, iron fly, bull put spread and bear call spread. Short strikes are chosen by target delta, wings by strike offset.
- Analysis (`POST /strategy/analyze`): legs are priced at executable quotes (sell at bid, buy at ask). It returns net credit, exact max profit and loss with unbounded detection, breakevens, net Greeks, payoff curves, a model probability of profit at ATM IV, and a margin estimate. The estimate is max loss for defined risk and `SHORT_OPTION_MARGIN_PCT` otherwise; the broker is authoritative.
- **Execute, hedges first** sends legs one at a time through `POST /orders`, buy legs first. It stops at the first leg that is not accepted. The order path blocks a new short until its hedge is in the book, and naked shorts are flagged before you send.

## Operations
- `GET /metrics` serves Prometheus text format: feed liveness and rejections by reason, broker and stream health, orders by status, ambiguous orders, firm P&L and exposure, snapshot freshness, instrument masters, loaders and auto-subscribed tokens.
- Brokers with a fixed URL in `INSTRUMENT_MASTER_URLS_JSON` (Zerodha, Angel, Upstox) reload once a day after `INSTRUMENT_REFRESH_AFTER_IST` (08:30), when the new file is published.

## Terminal UI
- The frontend lives in `frontend/` (`index.html`, `css/terminal.css`, `js/*.js`) and is served at `/` and `/static`. No inline script remains, so the Content-Security-Policy allows scripts only from the terminal's own origin.
- The header shows an IST clock, NSE and MCX session state (exchange holidays are not modelled) and feed latency. Toasts announce kill-switch, risk-block, fill, loader and stream events.
- Keyboard: **Ctrl/⌘+K** command palette, **Alt+1–9** tabs, **?** help, **Esc** closes dialogs, **R** refreshes, **G** toggles Greeks. Shortcuts never place orders, and every order and control action asks for confirmation.

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
- **Option chain auto-subscription.** `KOTAK_AUTO_CHAIN_JSON` (for example `{"NIFTY": {"expiries": 2, "strikes": 15}}`) keeps the nearest expiries' CE/PE strikes within ±N of spot subscribed, re-centred every `KOTAK_AUTO_CHAIN_INTERVAL_SEC`.
  - Option tokens change every expiry, so this is what keeps the chain live without editing `SUBSCRIPTION_JSON`.
  - Open positions always stay subscribed and come first under `KOTAK_AUTO_CHAIN_MAX_TOKENS`. A buffer of a few strikes stops churn while spot sits on a strike boundary.
  - Status is on the Market tab.
- The SDK writes `logs/neo-api-client.log` in the working directory, including mobile number and client code. `logs/` is gitignored.

## Credentials
Broker credentials never go in chat, git or `.env`. Set them as environment variables: `KOTAK_API_KEY`, `KOTAK_MOBILE`, `KOTAK_CLIENT_CODE`, `KOTAK_MPIN` and `KOTAK_TOTP_SECRET`.
- In a cloud session, add them in the environment settings.
- On a server, use its secret store or export them before `docker compose up`; compose passes them through to the terminal.
- Process environment variables take precedence over `.env`.

## Credentialed test
Keep `LIVE_TRADING=false` and run:
`python scripts/credentialed_live_certification.py`

The script is read-only. It does not place, modify or cancel orders.

## Live-money gate
The final gate still requires controlled live evidence: real feed, real instrument master, broker reconciliation, margin evidence, small-size order acknowledgement, fill/rejection/cancel lifecycle, emergency exit, kill-switch test, recovery test and operator sign-off.

`scripts/live_gate.py` runs the automatable parts (preflight, one tiny resting buy plus cancel, kill-switch and reject paths, restart recovery) and writes an evidence report. See `scripts/CONTROLLED_LIVE_TEST.md`. Working orders can be cancelled one at a time from the Orders tab or with `POST /orders/{client_order_id}/cancel` (operator token, audited).
