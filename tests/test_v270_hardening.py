import pathlib, ast
ROOT=pathlib.Path(__file__).resolve().parents[1]
def test_v270_durable_and_margin_gates():
    cfg=(ROOT/"backend/app/config.py").read_text()
    eb=(ROOT/"backend/app/eventbus.py").read_text()
    main=(ROOT/"backend/app/main.py").read_text()
    assert "REQUIRE_DURABLE_EVENT_BUS" in cfg
    assert "REDIS_URL" in cfg
    assert "DURABLE_EVENT_BUS_NOT_READY" in main or "DURABLE_EVENT_BUS_NOT_READY" in (ROOT/"backend/app/readiness.py").read_text()
    assert "MARGIN_EVIDENCE_UNAVAILABLE" in main
def test_v270_no_client_supplied_live_pnl_or_margin():
    s=(ROOT/"backend/app/main.py").read_text()
    assert "current_pnl:float" not in s
    assert "margin_pct:float" not in s
def test_v270_docker_dependencies():
    s=(ROOT/"docker-compose.yml").read_text()
    assert "postgres:" in s and "redis:" in s
    assert "127.0.0.1:8000:8000" in s
