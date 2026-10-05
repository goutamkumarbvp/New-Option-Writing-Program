"""The .env reader matches what Docker Compose accepts, and the shipped .env.example is valid."""
import json
import os
from pathlib import Path

import pytest

from app.brokers import totp_secret_valid
from app.config import Settings, load_dotenv, parse_env_line

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('raw,want', [
    ('KEY=value', ('KEY', 'value')),
    ('  KEY = value  ', ('KEY', 'value')),
    ('export KEY=value', ('KEY', 'value')),
    ('KEY=', ('KEY', '')),
    ('KEY=      # note', ('KEY', '')),
    ('KEY=1000   # note', ('KEY', '1000')),
    ('KEY=1000\t# note', ('KEY', '1000')),
    ("KEY='{\"a\": [\"nse_cm|Nifty 50\"]}'", ('KEY', '{"a": ["nse_cm|Nifty 50"]}')),
    ('KEY="a # not a comment"', ('KEY', 'a # not a comment')),
    ('KEY=a#b', ('KEY', 'a#b')),
    ('﻿KEY=bom', ('KEY', 'bom')),
    ('KEY=crlf\r', ('KEY', 'crlf')),
    ('# comment', None), ('', None), ('no equals', None), ('=value', None),
])
def test_parse_env_line(raw, want):
    assert parse_env_line(raw) == want


def test_load_dotenv_never_overrides_the_process_environment(tmp_path, monkeypatch):
    f = tmp_path / '.env'
    f.write_bytes('﻿IORT_TEST_A=from-file\r\nIORT_TEST_B=from-file\r\n'.encode('utf-8'))
    monkeypatch.setenv('IORT_TEST_B', 'from-env')
    monkeypatch.delenv('IORT_TEST_A', raising=False)
    monkeypatch.delenv('IORT_NO_DOTENV', raising=False)
    assert load_dotenv(f) == str(f)
    assert os.environ['IORT_TEST_A'] == 'from-file' and os.environ['IORT_TEST_B'] == 'from-env'
    monkeypatch.setenv('IORT_NO_DOTENV', '1')
    assert load_dotenv(f) is None


def test_env_example_is_complete_and_parses():
    keys = {}
    for raw in (ROOT / '.env.example').read_text(encoding='utf-8').splitlines():
        kv = parse_env_line(raw)
        if kv:
            assert kv[0] not in keys, f'duplicate {kv[0]}'
            keys[kv[0]] = kv[1]
    assert not any(v.startswith('#') for v in keys.values())
    for k, v in keys.items():
        if k.endswith('_JSON'):
            json.loads(v or '{}')
    fields = {f.upper() for f in Settings.__dataclass_fields__}
    assert set(keys) - fields <= {'POSTGRES_PASSWORD'}, 'unknown setting in .env.example'
    missing = {f for f in fields if f.startswith(('KOTAK_', 'STREAM_'))} - set(keys)
    assert not missing, f'Kotak/stream settings missing from .env.example: {missing}'
    # Section 1 is the Kotak Neo login, credentials empty; order routing stays locked by default.
    first = [k for k in keys][:6]
    assert first == ['KOTAK_API_KEY', 'KOTAK_MOBILE', 'KOTAK_CLIENT_CODE', 'KOTAK_MPIN', 'KOTAK_TOTP_SECRET', 'KOTAK_TOTP']
    assert all(keys[k] == '' for k in first) and keys['LIVE_TRADING'] == 'false'


def test_totp_secret_validation():
    assert totp_secret_valid('JBSWY3DPEHPK3PXP') and totp_secret_valid('jbsw y3dp ehpk 3pxp')
    assert not totp_secret_valid('123456') and not totp_secret_valid('not-base32-!!') and not totp_secret_valid('')


def test_env_example_raw_text_has_no_parser_traps():
    # Checked on the raw text, with no parser: these lines read differently in different .env parsers.
    import re
    for n, line in enumerate((ROOT / '.env.example').read_text(encoding='utf-8').splitlines(), 1):
        if line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        assert not re.match(r'\s+#', value), f'line {n}: empty value followed by a comment'
        assert '\t' not in line and ' #' not in value, f'line {n}: inline comment'
        assert not (value.startswith('"') and value[1:2] in '{['), f'line {n}: JSON must be single-quoted'
        assert '$' not in value, f'line {n}: $ is expanded by Docker Compose'


def test_bad_values_never_crash_and_block_live_routing(monkeypatch):
    import app.config as cfg
    monkeypatch.setenv('DATA_STALE_MS', '1500ms')
    monkeypatch.setenv('REQUIRE_MARGIN_EVIDENCE', 'ture')
    monkeypatch.setenv('KOTAK_AUTO_CHAIN_JSON', '\'{"NIFTY": {"strikes": 5}}\'')
    try:
        cfg.CONFIG_ERRORS.clear()
        s = cfg.Settings()
        assert s.data_stale_ms == 1500 and s.require_margin_evidence is True  # defaults, never a looser value
        assert json.loads(s.kotak_auto_chain_json) == {'NIFTY': {'strikes': 5}}  # outer quotes removed
        errs = s.validation_errors()
        assert 'CONFIG_INVALID:DATA_STALE_MS' in errs and 'CONFIG_INVALID:REQUIRE_MARGIN_EVIDENCE' in errs
    finally:
        cfg.CONFIG_ERRORS.clear()


def test_utf16_env_file_is_read(tmp_path, monkeypatch):
    import app.config as cfg
    f = tmp_path / '.env'
    f.write_bytes('IORT_TEST_U16=yes\r\n'.encode('utf-16'))
    monkeypatch.delenv('IORT_TEST_U16', raising=False)
    monkeypatch.delenv('IORT_NO_DOTENV', raising=False)
    try:
        assert load_dotenv(f) == str(f) and os.environ['IORT_TEST_U16'] == 'yes'
        assert cfg.DOTENV_PROBLEM.startswith('ENV_FILE_UTF16')
    finally:
        cfg.DOTENV_PROBLEM = None
