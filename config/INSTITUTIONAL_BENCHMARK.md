# Institutional Benchmark — V2.2.0

Benchmark references: Bloomberg EMS/Tradebook, Trading Technologies OMS, FlexOMS/FlexTrader, Nasdaq trade-surveillance capabilities.

## Added in V2.2.0
- FIX-style protocol boundary with sequencing/idempotency and injected transport.
- Parent/child multi-leg OMS lifecycle.
- Hierarchical firm/account/product risk reservation model.
- Real-time drop-copy ingestion boundary.
- Post-trade pro-rata allocation engine.
- TCA metrics: arrival, VWAP and decision slippage.
- Participant-side surveillance alerts and exception workflow.
- Latency percentile monitor.
- Primary/secondary DR controller.
- Evidence-based release gate that refuses certification when load/soak/failover/security/broker-contract evidence is absent.

## Still not equivalent to commercial institutional platforms
This repository is not Bloomberg EMSX/TEMS, TT OMS, FlexOMS/FlexTrader, or Nasdaq SMARTS equivalent. Those products have years of production operation, broad venue/FIX connectivity, managed infrastructure, compliance ecosystems, ultra-low-latency/colocation capabilities, global support and independently evidenced scale/reliability. V2.2.0 adds architectural counterparts, but external production evidence remains mandatory.
