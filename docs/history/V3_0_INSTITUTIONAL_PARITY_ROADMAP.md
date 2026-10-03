# IORT V3.0.0 — Institutional Parity Roadmap

## Implemented in software
- NSE/BSE/MCX canonical market-data architecture
- Zerodha / Upstox / Angel One / Kotak broker adapters
- durable Redis Streams event publication
- PostgreSQL order/fill/position/reconciliation ledger
- broker-authoritative pre-trade evidence
- instrument-specific freshness gate
- persistent kill switch
- dual independent risk monitors
- smart broker selection with explicit no-retry on ambiguous submission
- client-order idempotency
- persistent hash-chained audit records
- option-chain OI/PCR/max-pain/IV aggregation when broker feed supplies IV
- Greeks/scenario/TCA/algorithmic planning modules
- parent/child multi-leg OMS model
- allocation, surveillance, exception and DR control models
- WebSocket institutional dashboard

## Institutional capabilities documented by reference platforms
Current TT documentation describes consolidated order books, dynamic pre-trade risk, FIX routing/drop-copy, multi-leg parent/child handling, allocations and surveillance. FlexTrade documents multi-asset OMS, options/multi-leg workflows, risk, exception handling and TCA. Bloomberg documents end-to-end OMS, compliance, reconciliation and authoritative book-of-record workflows. Nasdaq documents cross-product/venue surveillance and investigative views.

## Not honestly certifiable from this code-only environment
- direct exchange/FIX certification and venue conformance
- colocated production latency
- real broker/exchange execution, fills, partial fills and rejects
- real disconnect/recovery under market load
- disaster-recovery drill and backup/restore proof
- independent penetration test
- dependency vulnerability scan by an installed scanner
- long-duration soak/load benchmark
- production operational sign-off

These require external infrastructure or real-account evidence and are deliberately not marked PASS.
