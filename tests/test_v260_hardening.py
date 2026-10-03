from pathlib import Path
ROOT=Path(__file__).parents[1]

def test_version_consistent():
    assert 'VERSION =' in (ROOT/'backend/app/version.py').read_text()
    s=(ROOT/'backend/app/main.py').read_text()
    assert "version=VERSION" in s
    assert "'version':VERSION" in s
    assert '2.4.0' not in s

def test_upstox_uses_official_sdk_not_hand_built_wire_schema():
    s=(ROOT/'backend/app/broker_streams.py').read_text()
    assert 'MarketDataStreamerV3' in s
    assert 'descriptor_pb2' not in s
    assert 'MarketDataFeedV3.proto' not in s

def test_live_order_has_fail_closed_evidence_gates():
    s=(ROOT/'backend/app/main.py').read_text()
    for token in ('TARGET_BROKER_NOT_AUTHENTICATED','TARGET_BROKER_FEED_NOT_LIVE','BROKER_RECONCILIATION_REQUIRED','LIVE_RISK_SNAPSHOT_FAILED','INSTRUMENT_TOKEN_REQUIRED'):
        assert token in s or token in (ROOT/'backend/app/readiness.py').read_text()

def test_external_broker_orders_are_imported_before_reconciliation():
    s=(ROOT/'backend/app/reconcile.py').read_text()
    assert 'EXTERNAL-{broker.name}-{bo}' in s
