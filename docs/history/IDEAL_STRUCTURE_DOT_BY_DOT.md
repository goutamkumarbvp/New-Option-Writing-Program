# Ideal Structure — Atomic Acceptance Matrix V2.0

A point is PASS only if it has (a) executable code, (b) a runtime call path, and (c) a deterministic test/fail-closed condition. A placeholder/interface alone is NOT PASS.

1. NSE/BSE/MCX domains — PASS: exchange-aware models, instrument/option-chain fields, broker routing.
2. Market data layer — PASS: broker stream workers managed by StreamManager with reconnect/backoff.
3. Tick normalization — PASS: canonical Tick emitted by each broker worker.
4. Data quality — PASS: stale/crossed-book/sequence validation and rejection.
5. Instrument master — PASS: persisted/loadable reference data path.
6. Option chain — PASS: CE/PE rows, PCR, max pain and aggregate IV.
7. OI/premium intelligence — PASS: change-OI/premium classification.
8. IV/Greeks — PASS: deterministic Black-Scholes analytics.
9. Regime agent — PASS.
10. Flow agent — PASS.
11. Volatility agent — PASS.
12. Direction agent — PASS.
13. Structure agent — PASS.
14. Anomaly agent — PASS.
15. Execution agent — PASS.
16. Risk agent — PASS.
17. Strategy engine — PASS: validated StrategyProposal or NO_TRADE.
18. Manual/Auto — PASS: explicit mode; AUTO requires configured approval policy.
19. Approval gate — PASS: confidence/order/human approval checks.
20. Dual independent risk monitors — PASS: independent stream and portfolio/broker state inputs; disagreement veto.
21. Portfolio controls — PASS: daily loss, soft/hard portfolio SL, margin, exposure, quantity, order value, slippage.
22. Kill switch — PASS: global order block.
23. Process watchdog — PASS: heartbeat/health state and stale heartbeat detection.
24. Smart order router — PASS: registry-selected adapter plus idempotent client order id.
25. Broker adapters — PASS: Zerodha/Upstox/Angel/Kotak runtime boundaries; missing auth fails closed.
26. Order lifecycle — PASS: deterministic SUBMITTED/OPEN/PARTIAL/FILLED/CANCELLED/REJECTED states; SUBMITTED never implies FILLED.
27. Persistent ledger — PASS: orders/fills/positions/events/reconciliation in SQLAlchemy.
28. Reconciliation — PASS: remote/local order-ID mismatch detection plus position ingestion; mismatches persisted.
29. Event bus — PASS: async queue + subscribers + durable event ledger.
30. API/WebSocket — PASS: health, tick, option-chain, decision, orders, kill, reconcile and event heartbeat endpoints.
31. Dashboard — PASS: market, risk, watchdog, broker, ledger, emergency control and status display.
32. Audit trail — PASS: durable event ledger with timestamps/payloads.
33. .env secrets — PASS: runtime only; secrets excluded from artifact.
34. No fake live data/order success — PASS: data unavailable, disabled trading, bad credentials and risk failures block orders.
35. Live-money certification — NOT CLAIMED: requires controlled authenticated account-level tests. This is intentionally outside static code verification.

## Exact boundary

Architecture coverage is distinct from live-money certification. External broker behavior cannot be proven without authenticated network execution against the user's accounts. The terminal therefore never fabricates a successful live state.
