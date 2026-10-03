# V1.8.0 — Atomic architecture acceptance matrix

Every item below requires (a) an implementation module, (b) a reachable runtime path, and (c) a test or explicit external-integration boundary.

1. NSE/BSE/MCX domain -> Tick model + venue-aware instrument configuration.
2. Market-data layer -> broker WebSocket workers + reconnect/backoff.
3. Tick normalization -> canonical Tick path.
4. Data quality -> stale, timestamp, crossed-book, sequence and feed-gap gates.
5. Instrument Master -> instrument registry/import boundary; no guessed contracts.
6. Option Chain -> strike/type/expiry snapshot and PCR summary.
7. OI/Premium Flow -> delta classification with evidence.
8. IV/Greeks -> Greeks module; live IV is only accepted from feed/calculation inputs.
9. Regime Agent -> executable evidence scorer.
10. Flow Agent -> OI/premium evidence scorer.
11. Volatility Agent -> IV evidence scorer.
12. Direction Agent -> price evidence scorer.
13. Structure Agent -> structure evidence scorer.
14. Anomaly Agent -> feed anomaly veto evidence.
15. Execution Agent -> liquidity/execution readiness evidence.
16. Risk Agent -> risk-block evidence.
17. Strategy Engine -> real agent aggregation; insufficient evidence => NO_TRADE.
18. Manual/Auto -> explicit Mode + approval policy.
19. Approval Gate -> confidence/order/human approval checks.
20. Dual Risk -> independent stream and portfolio monitors; disagreement => veto.
21. Portfolio risk -> soft/hard SL, daily loss, margin, exposure, quantity, order-value, slippage.
22. Kill Switch -> hard order endpoint veto.
23. Watchdog -> heartbeat health state.
24. Smart Order Router -> broker registry + unique client order id.
25. Broker adapters -> Zerodha/Upstox/Angel/Kotak runtime adapters; vendor-specific failures fail closed.
26. Order lifecycle -> submitted is distinct from filled; broker order id persisted.
27. Persistent ledger -> orders/fills/positions/events in SQLAlchemy DB.
28. Reconciliation -> broker snapshots normalized and written to ledger/audit.
29. Event Bus -> async publish/subscribe with overflow fail-closed.
30. API/WebSocket -> health, tick, chain, decision, order, kill, event socket.
31. Dashboard -> market/risk/trading/broker health + panic UI; DATA UNAVAILABLE by default.
32. Audit -> durable event ledger for ticks/orders/reconciliation/kill.
33. Secrets -> .env runtime only; no credentials packaged.
34. No fake live state -> no synthetic market data or fake broker success.
35. External live certification -> explicitly NOT claimed until current account credentials, entitlements, static IP/network and real order/fill/reconciliation tests succeed.
