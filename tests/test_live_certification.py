from pathlib import Path

ROOT=Path(__file__).parents[1]

def test_cert_harness_is_read_only():
    s=(ROOT/'scripts/credentialed_live_certification.py').read_text()
    assert '.place(' not in s
    assert 'place_order' not in s
    assert 'c.cancel' not in s and 'cancel_order' not in s

def test_v251_preserves_ws_event_fanout():
    s=(ROOT/'backend/app/main.py').read_text()
    assert 'ws_clients=set()' in s
    assert 'q=asyncio.Queue(maxsize=256);ws_clients.add(q)' in s
    assert 'q.put_nowait(event)' in s

def test_live_defaults_fail_closed():
    s=(ROOT/'backend/app/config.py').read_text()
    assert "LIVE_TRADING',False" in s
    assert "AUTO_TRADING_ENABLED',False" in s
