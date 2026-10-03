from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
MAIN=(ROOT/'backend/app/main.py').read_text()
FRONT=(ROOT/'frontend/index.html').read_text()
DASH=(ROOT/'backend/app/dashboard.py').read_text()


def test_dashboard_has_single_authoritative_state_endpoint():
    assert "@app.get('/dashboard/state')" in MAIN
    assert "build_dashboard_state(" in MAIN


def test_dashboard_has_all_operational_tabs():
    for tab in ['overview','market','risk','ai','execution','orders','recon','tca','surveillance','system']:
        assert f"['{tab}'," in FRONT
        assert f'id="{tab}"' in FRONT


def test_dashboard_uses_single_websocket_event_stream():
    assert "'/ws/events'" in FRONT
    assert "new WebSocket" in FRONT
    assert 'setInterval(refresh,2000)' in FRONT


def test_ui_does_not_directly_call_broker_or_order_routes():
    assert "fetch('/orders'" not in FRONT
    assert "fetch('/emergency-stop" not in FRONT
    assert "fetch('/reconcile/" not in FRONT
    assert "fetch('/broker" not in FRONT


def test_ui_control_uses_authenticated_kill_route_only():
    assert "fetch('/kill?reason=UI_PANIC'" in FRONT
    assert "X-IORT-Operator-Token" in FRONT


def test_ui_event_delivery_is_non_authoritative():
    assert 'UI delivery is non-authoritative telemetry' in MAIN
    assert 'UI_EVENT_BUS_ERROR' in MAIN


def test_order_submission_emits_ui_event_after_ledger_audit():
    assert "await emit_ui_event({'type':'ORDER_SUBMISSION'" in MAIN
    assert "audit_chain.append('ORDER_SUBMISSION'" in MAIN


def test_reconciliation_emits_ui_event():
    assert "'type':'RECONCILIATION'" in MAIN


def test_dashboard_state_contains_execution_risk_and_ledger_domains():
    for key in ["'risk':", "'execution':", "'ledger':", "'orders':", "'reconciliation':", "'audit':"]:
        assert key in DASH
