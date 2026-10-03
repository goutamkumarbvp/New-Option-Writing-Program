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

## Certification rule
A credentialed read-only PASS is **not** the same as live-money certification. Final LIVE PRODUCTION certification requires evidence for Gates A-C and a signed operator record.
