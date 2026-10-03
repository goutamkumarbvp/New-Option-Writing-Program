import ast
import subprocess
import sys
from pathlib import Path

from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_cert_harness_never_calls_order_mutation_methods():
    tree = ast.parse((ROOT / 'scripts/credentialed_live_certification.py').read_text())
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not called & {'place', 'place_order', 'cancel', 'cancel_all', 'cancel_order', 'modify_order', 'flatten_broker'}


def test_cert_harness_without_credentials_is_not_certified(tmp_path):
    env = {'PATH': '/usr/bin:/bin', 'DATABASE_URL': 'sqlite:///:memory:', 'DATA_DIR': str(tmp_path), 'HOME': str(tmp_path), 'CERT_OUTPUT_DIR': str(tmp_path)}
    r = subprocess.run([sys.executable, str(ROOT / 'scripts/credentialed_live_certification.py')], env=env, capture_output=True,
                       text=True, timeout=60)
    assert (tmp_path / 'credentialed_live_certification.json').exists()
    assert r.returncode == 2 and '"status": "NOT_CERTIFIED"' in r.stdout and '"live_order_test_executed": false' in r.stdout


def test_defaults_are_fail_closed(monkeypatch):
    for k in ('LIVE_TRADING', 'AUTO_TRADING_ENABLED', 'ALLOW_MARKET_ORDERS', 'ALLOW_NAKED_SHORT_OPTIONS', 'AUTO_FLATTEN_ON_HARD_STOP'):
        monkeypatch.delenv(k, raising=False)
    s = Settings()
    assert not s.live_trading and not s.auto_trading_enabled and not s.allow_market_orders and not s.allow_naked_short_options
    assert s.require_margin_evidence and s.require_instrument_master and s.require_operator_auth


def test_live_config_validation():
    s = Settings()
    object.__setattr__(s, 'live_trading', True)
    object.__setattr__(s, 'operator_api_token', 'short')
    object.__setattr__(s, 'portfolio_soft_sl', 9999)
    errs = s.validation_errors()
    assert 'OPERATOR_API_TOKEN_TOO_SHORT' in errs and 'SOFT_SL_GREATER_THAN_HARD_SL' in errs
