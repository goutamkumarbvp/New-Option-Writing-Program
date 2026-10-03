# V2.6.0 Release Certificate — Engineering Scope

## Status
**ENGINEERING VERIFIED / LIVE-PRODUCTION CANDIDATE**

This certificate covers deterministic source-level and automated verification performed on the packaged release. It does **not** certify exchange/broker live-money behaviour without a controlled live execution record.

## Verified controls
- Live trading OFF by default.
- Explicit production acknowledgement required before live orders.
- Target broker authentication gate.
- Target broker market-data heartbeat gate.
- Instrument-specific fresh-tick gate.
- Broker-side position snapshot before live order.
- Broker reconciliation before live order.
- Portfolio soft/hard loss gates from verified broker position P&L.
- Exposure gate from broker positions.
- Instrument token required for live orders.
- Invalid side/quantity/limit-price fail closed.
- Kill switch veto.
- Watchdog veto.
- WebSocket event fan-out.
- REST order-status polling remains a reconciliation/fallback path.
- Upstox V3 market feed uses the official Python SDK/protobuf implementation rather than a hand-built wire schema.
- No secrets packaged in the release.

## Explicit non-claims
This is **not** a claim of commercial equivalence to Bloomberg, Trading Technologies, FlexTrade, Nasdaq SMARTS, or any other institutional platform. Those platforms have materially broader venue/FIX connectivity, colocated infrastructure, HA/DR, surveillance, compliance, OMS/EMS workflows, TCA, allocations, operational controls and production evidence that cannot be recreated or proven by this repository alone.

It is also **not** a claim of guaranteed execution price, guaranteed stop-loss price, guaranteed uptime, or profitable trading.

## Final live-money gate
A real-money certification requires controlled evidence from the user's deployment environment: authenticated broker connectivity, current instruments, live market stream, one minimal-size controlled order, broker acknowledgement/order ID, fill or rejection, cancellation of any remainder, ledger match, reconciliation PASS, and failure-path tests.
