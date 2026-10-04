# Controlled Live Certification — Human-Executed Gate

This test is intentionally separate from automated credential checks. **Do not execute with normal/full-size quantities.**

## Gate A — before live order
- Confirm market is open and instrument/expiry/token is current.
- Confirm `LIVE_TRADING=true` and `LIVE_PRODUCTION_ACK=I_UNDERSTAND_REAL_ORDERS` are deliberate.
- Confirm kill switch is clear.
- Confirm emergency manual exit is available.
- Confirm broker static-IP / app / exchange prerequisites.

## Gate B — one tiny controlled order
1. Use the smallest permitted quantity for a liquid, approved instrument.
2. Submit **one** order manually through the terminal.
3. Record local client_order_id and broker order_id.
4. Verify broker status transitions: SUBMITTED/OPEN/PARTIAL/FILLED or REJECTED/CANCELLED.
5. Verify fill/trade report and local ledger match.
6. Cancel any remaining open quantity immediately.
7. Verify reconciliation shows no unexplained order/position mismatch.

## Gate C — failure-path tests
- Reject path
- Partial fill path
- Cancel path
- WebSocket disconnect/reconnect
- REST fallback
- stale market data => NO TRADE
- kill switch => NO NEW ORDER
- watchdog failure => NO NEW ORDER

## Guided runner: `scripts/live_gate.py`
The runner automates the parts of Gates A-C that can be automated, against a running terminal. Evidence goes to `reports/live_gate_<date>.json`, which is gitignored; attach it to the signed record.

Run it from the machine whose static IP is registered with the broker, during market hours. The terminal must run through `docker compose` (Postgres and Redis) with `LIVE_TRADING=true` and `LIVE_PRODUCTION_ACK=I_UNDERSTAND_REAL_ORDERS`. Kotak credentials go in environment variables, and `OPERATOR_API_TOKEN` must be available to the script.

1. `python scripts/live_gate.py preflight --broker KOTAK --token <liquid option token>`: Gate A. It reconciles, checks readiness, auth, feed, instrument master, kill switch, risk snapshot, session and contract quote, and lists the manual confirmations.
2. `python scripts/live_gate.py order-cancel --broker KOTAK --token <token> --i-understand-real-orders`: Gate B. It sends ONE buy of ONE lot as a LIMIT below the bid but inside the price collar, after you type the contract symbol. It waits for the broker acknowledgement, cancels with `POST /orders/{id}/cancel`, waits for CANCELLED and reconciles. It refuses above `--max-premium` (Rs 2,000 by default) and never sells.
3. `kill-test` and `reject-test`: Gate C. Orders are blocked by the kill switch and the price collar inside the terminal and never reach the broker.
4. `recovery-arm`, then restart the terminal (`docker compose restart terminal`), then `recovery-verify`. This proves the kill state survives a restart.
5. `report` summarises the gates and leaves the operator sign-off fields blank to be filled by hand.

Still manual: partial fill, WebSocket disconnect/reconnect, REST fallback, and stale data and watchdog failure under live load.

## Certification rule
A credentialed read-only PASS is **not** the same as live-money certification. Final LIVE PRODUCTION certification requires evidence for Gates A-C and a signed operator record.
