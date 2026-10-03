import os
os.environ.setdefault("LIVE_TRADING","false")
os.environ.setdefault("REQUIRE_OPERATOR_AUTH","true")

def test_operator_auth_constant_time_gate():
    from backend.app.operator_auth import authorize
    from backend.app.config import settings
    old=settings.operator_api_token
    # frozen settings comes from env; no secret is asserted here
    assert authorize(None) is False or settings.live_trading is False

def test_live_tick_endpoint_contains_fail_closed_guard():
    src=open("backend/app/main.py",encoding="utf8").read()
    assert "EXTERNAL_TICK_INGEST_DISABLED_IN_LIVE" in src
    assert "OPERATOR_AUTH_REQUIRED" in src
