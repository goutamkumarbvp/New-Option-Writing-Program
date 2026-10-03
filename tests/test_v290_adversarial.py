import ast, pathlib
ROOT=pathlib.Path(__file__).resolve().parents[1]
MAIN=(ROOT/'backend/app/main.py').read_text()
CFG=(ROOT/'backend/app/config.py').read_text()
APP=(ROOT/'backend/app/approval.py').read_text()
RISK=(ROOT/'backend/app/live_risk.py').read_text()
BROK=(ROOT/'backend/app/brokers.py').read_text()

def test_auto_mode_cannot_bypass_disabled_flag():
    assert "AUTO_TRADING_DISABLED" in MAIN
    assert "auto_trading_enabled" in APP

def test_fixed_approval_phrase_removed():
    assert "manual_token!='APPROVE'" not in APP
    assert "approval_token" not in MAIN.split("@app.post('/orders')",1)[1].split("@app.post('/kill')",1)[0]

def test_emergency_stop_exists_and_requires_operator():
    assert "/emergency-stop/{broker}" in MAIN
    assert "OPERATOR_AUTH_REQUIRED" in MAIN

def test_exposure_does_not_silently_value_missing_live_price_as_zero():
    assert "LIVE_EXPOSURE_PRICE_UNAVAILABLE" in RISK

def test_broker_cancel_surface_exists():
    assert "async def cancel(" in BROK
    assert "async def cancel_all(" in BROK

def test_live_tick_cannot_be_http_injected():
    assert "EXTERNAL_TICK_INGEST_DISABLED_IN_LIVE" in MAIN

def test_version():
    assert "VERSION = '3.0.1'" in (ROOT/'backend/app/version.py').read_text()

def test_fill_idempotency_has_broker_trade_id():
    s=(ROOT/'backend/app/ledger.py').read_text()
    assert 'broker_trade_id=Column' in s
    assert 'filter_by(broker_trade_id=trade_id)' in s

def test_terminal_order_cannot_regress():
    s=(ROOT/'backend/app/ledger.py').read_text()
    assert 'ALLOWED_TRANSITIONS' in s
    assert 'Never allow a terminal order to move backwards' in s

def test_slippage_is_a_hard_pretrade_gate():
    assert "SLIPPAGE_LIMIT" in MAIN
    assert "spread_bps>settings.max_slippage_bps" in MAIN

def test_kill_switch_is_restored_from_persistent_ledger():
    assert "latest_kill" in (ROOT/'backend/app/ledger.py').read_text()
    assert "persisted_kill" in MAIN
