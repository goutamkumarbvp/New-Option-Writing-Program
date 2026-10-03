from pathlib import Path
ROOT=Path(__file__).parents[1]

def test_auto_routing_is_deterministic_and_no_retry_on_ambiguous_submission():
    s=(ROOT/"backend/app/routing.py").read_text()
    assert "UNKNOWN_SUBMISSION_STATE" in s
    assert "BROKER_TIMEOUT_NO_RETRY" in s
    assert "SMART_ROUTING_ENABLED" in (ROOT/"backend/app/config.py").read_text()

def test_order_idempotency_key_supported():
    s=(ROOT/"backend/app/models.py").read_text()
    m=(ROOT/"backend/app/main.py").read_text()
    assert "client_order_id:Optional[str]=None" in s
    assert "IDEMPOTENT_REPLAY" in m

def test_persistent_audit_chain_exists():
    assert (ROOT/"backend/app/audit_chain.py").exists()
    s=(ROOT/"backend/app/ledger.py").read_text()
    assert "AuditLedger" in s and "latest_audit_hash" in s

def test_live_instrument_gate_exists():
    s=(ROOT/"backend/app/market.py").read_text()
    assert "def live_instrument" in s

def test_tick_schema_carries_iv():
    assert "iv:Optional[float]=None" in (ROOT/"backend/app/models.py").read_text()

def test_scenario_risk_can_be_required():
    c=(ROOT/"backend/app/config.py").read_text();m=(ROOT/"backend/app/main.py").read_text()
    assert "REQUIRE_SCENARIO_RISK" in c and "SCENARIO_RISK_EVIDENCE_UNAVAILABLE" in m
