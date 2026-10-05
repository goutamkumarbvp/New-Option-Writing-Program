"""Runtime configuration.

Values are read from the environment once, when this module is imported.
Every live-trading switch defaults to the safe (disabled / fail-closed) value.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path


def parse_env_line(raw):
    """(key, value) for one .env line, or None. Accepts what Docker Compose accepts: blank lines and
    # comments, an optional 'export ', single or double quotes (kept verbatim inside, so JSON is
    safe), an unquoted value ending at ' #' or a tab-#, and Windows CRLF line ends."""
    line = raw.strip().lstrip('\ufeff')
    if not line or line.startswith('#') or '=' not in line:
        return None
    k, v = line.split('=', 1)
    k = k.strip()
    if k.startswith('export '):
        k = k[7:].strip()
    v = v.strip()
    if v[:1] in ('"', "'"):
        end = v.find(v[0], 1)
        v = v[1:end] if end > 0 else v[1:]
    elif v.startswith('#'):
        v = ''  # 'KEY=   # note' is an empty value followed by a comment
    else:
        for marker in (' #', '\t#'):
            v = v.split(marker, 1)[0]
        v = v.strip()
    return (k, v) if k else None


def _read_env_text(f):
    """Text of a .env file in UTF-8 (with or without BOM) or UTF-16 (what Notepad's 'Unicode' and Windows
    PowerShell 5.1 redirection write). Returns (text, problem)."""
    data = f.read_bytes()
    if data[:2] in (b'\xff\xfe', b'\xfe\xff') or b'\x00' in data[:200]:
        try:
            return data.decode('utf-16'), 'ENV_FILE_UTF16:save .env as UTF-8 (it was read anyway)'
        except UnicodeDecodeError:
            return '', 'ENV_FILE_UNREADABLE:save .env as UTF-8'
    return data.decode('utf-8-sig', errors='replace'), None


def load_dotenv(path=None):
    """Fill settings that the process environment does not set from the project's .env file.

    Docker Compose already hands .env to the container; this makes a run without Docker
    (python -m app, the Windows kit's native mode) read the same file. The process environment
    always wins. IORT_ENV_FILE points at another file; IORT_NO_DOTENV=1 turns loading off (tests).
    Returns the file that was read, or None. A file it cannot fully read never stops the terminal."""
    global DOTENV_PROBLEM
    if os.getenv('IORT_NO_DOTENV', '').strip().lower() in {'1', 'true', 'yes', 'on'}:
        return None
    here = Path(__file__).resolve()
    candidates = [Path(os.environ['IORT_ENV_FILE'])] if os.getenv('IORT_ENV_FILE') else [here.parents[2] / '.env']
    if path is not None:
        candidates = [Path(path)]
    for f in candidates:
        try:
            text, problem = _read_env_text(f)
        except OSError:
            continue
        DOTENV_PROBLEM = problem
        for raw in text.splitlines():
            kv = parse_env_line(raw.replace('\x00', ''))
            if kv:
                try:
                    os.environ.setdefault(*kv)
                except ValueError:  # a stray NUL or similar: skip the line, never crash
                    DOTENV_PROBLEM = DOTENV_PROBLEM or f'ENV_FILE_BAD_LINE:{kv[0]}'
        return str(f)
    return None


DOTENV_PROBLEM = None
DOTENV_FILE = load_dotenv()


# Settings that could not be parsed. They fall back to the default, are reported by name (never by
# value) and block live order routing through validation_errors(), so a typo can never loosen a limit
# or crash the terminal before it can say what is wrong.
CONFIG_ERRORS = []
_TRUE, _FALSE = {'1', 'true', 'yes', 'on'}, {'0', 'false', 'no', 'off'}


def _raw(name):
    v = os.getenv(name)
    return None if v is None or v.strip() == '' else v.strip()


def _b(name, default=False):
    v = _raw(name)
    if v is None:
        return default
    if v.lower() in _TRUE or v.lower() in _FALSE:
        return v.lower() in _TRUE
    CONFIG_ERRORS.append(name)
    return default


def _i(name, default):
    v = _raw(name)
    if v is None:
        return default
    try:
        return int(float(v))
    except ValueError:
        CONFIG_ERRORS.append(name)
        return default


def _f(name, default):
    v = _raw(name)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        CONFIG_ERRORS.append(name)
        return default


def _s(name, default=''):
    return os.getenv(name, default)


def _js(name, default='{}'):
    """A JSON setting. Outer quotes copied from a .env file into a Windows or cloud environment
    variable are removed, so '{"NIFTY":...}' works the same in both places."""
    v = os.getenv(name, default).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        v = v[1:-1].strip()
    return v or default


@dataclass(frozen=True)
class Settings:
    # --- mode and authority -------------------------------------------------
    live_trading: bool = field(default_factory=lambda: _b('LIVE_TRADING', False))
    auto_trading_enabled: bool = field(default_factory=lambda: _b('AUTO_TRADING_ENABLED', False))
    require_human_approval_auto: bool = field(default_factory=lambda: _b('REQUIRE_HUMAN_APPROVAL_AUTO', True))
    live_production_ack: str = field(default_factory=lambda: _s('LIVE_PRODUCTION_ACK'))
    operator_api_token: str = field(default_factory=lambda: _s('OPERATOR_API_TOKEN'))
    operator_token_min_length: int = field(default_factory=lambda: _i('OPERATOR_TOKEN_MIN_LENGTH', 24))
    require_operator_auth: bool = field(default_factory=lambda: _b('REQUIRE_OPERATOR_AUTH', True))
    allowed_origins: str = field(default_factory=lambda: _s('ALLOWED_ORIGINS'))
    audit_hmac_key: str = field(default_factory=lambda: _s('AUDIT_HMAC_KEY'))
    audit_chain_required: bool = field(default_factory=lambda: _b('AUDIT_CHAIN_REQUIRED', True))

    # --- infrastructure -----------------------------------------------------
    database_url: str = field(default_factory=lambda: _s('DATABASE_URL', 'postgresql+psycopg://iort:iort@postgres:5432/iort'))
    redis_url: str = field(default_factory=lambda: _s('REDIS_URL', 'redis://redis:6379/0'))
    require_durable_event_bus: bool = field(default_factory=lambda: _b('REQUIRE_DURABLE_EVENT_BUS', True))
    data_dir: str = field(default_factory=lambda: _s('DATA_DIR', './data'))

    # --- evidence requirements ---------------------------------------------
    require_margin_evidence: bool = field(default_factory=lambda: _b('REQUIRE_MARGIN_EVIDENCE', True))
    require_instrument_master: bool = field(default_factory=lambda: _b('REQUIRE_INSTRUMENT_MASTER', True))
    require_authoritative_daily_pnl: bool = field(default_factory=lambda: _b('REQUIRE_AUTHORITATIVE_DAILY_PNL', True))
    require_live_ltp_for_exposure: bool = field(default_factory=lambda: _b('REQUIRE_LIVE_LTP_FOR_EXPOSURE', True))
    require_scenario_risk: bool = field(default_factory=lambda: _b('REQUIRE_SCENARIO_RISK', False))
    require_broker_order_margin: bool = field(default_factory=lambda: _b('REQUIRE_BROKER_ORDER_MARGIN', False))

    # --- market data --------------------------------------------------------
    data_stale_ms: int = field(default_factory=lambda: _i('DATA_STALE_MS', 1500))
    max_clock_skew_ms: int = field(default_factory=lambda: _i('MAX_CLOCK_SKEW_MS', 2000))
    stream_stall_sec: int = field(default_factory=lambda: _i('STREAM_STALL_SEC', 30))
    # Feed reconnect: a drop after a healthy connection retries at once, then 1, 2, 5, 10, 20 s up to the cap.
    # A connection counts as healthy once it delivered data or stayed up STREAM_HEALTHY_RESET_SEC.
    stream_auto_reconnect: bool = field(default_factory=lambda: _b('STREAM_AUTO_RECONNECT', True))
    stream_reconnect_max_sec: float = field(default_factory=lambda: _f('STREAM_RECONNECT_MAX_SEC', 30))
    stream_healthy_reset_sec: float = field(default_factory=lambda: _f('STREAM_HEALTHY_RESET_SEC', 30))
    stream_relogin_after_failures: int = field(default_factory=lambda: _i('STREAM_RELOGIN_AFTER_FAILURES', 3))
    kotak_stream_stall_sec: float = field(default_factory=lambda: _f('KOTAK_STREAM_STALL_SEC', 15))
    subscription_json: str = field(default_factory=lambda: _js('SUBSCRIPTION_JSON'))
    instrument_master_urls_json: str = field(default_factory=lambda: _js('INSTRUMENT_MASTER_URLS_JSON'))
    underlying_spot_tokens_json: str = field(default_factory=lambda: _js('UNDERLYING_SPOT_TOKENS_JSON'))
    # Kotak publishes a new scrip master every day under a dated path; the terminal finds
    # and loads it automatically (consumer key only, no login) and refreshes it daily.
    kotak_instrument_master_auto: bool = field(default_factory=lambda: _b('KOTAK_INSTRUMENT_MASTER_AUTO', True))
    kotak_scrip_segments: str = field(default_factory=lambda: _s('KOTAK_SCRIP_SEGMENTS', 'nse_cm,nse_fo,bse_fo,mcx_fo'))
    instrument_refresh_check_sec: float = field(default_factory=lambda: _f('INSTRUMENT_REFRESH_CHECK_SEC', 300))
    # Brokers with a fixed INSTRUMENT_MASTER_URLS_JSON URL publish the day's file in the morning;
    # the file is reloaded once after this IST time each day.
    instrument_refresh_after_ist: str = field(default_factory=lambda: _s('INSTRUMENT_REFRESH_AFTER_IST', '08:30'))
    # Extra Kotak index-name -> F&O underlying mappings, e.g. {"Nifty IT": "NIFTYIT"} (built-ins cover
    # Nifty 50, Nifty Bank, Nifty Fin Service, Nifty Mid Select, Nifty Next 50, SENSEX, BANKEX).
    kotak_index_underlyings_json: str = field(default_factory=lambda: _js('KOTAK_INDEX_UNDERLYINGS_JSON'))
    # Option strikes kept subscribed around spot, per underlying:
    # {"NIFTY": {"expiries": 2, "strikes": 15}} = nearest 2 expiries, ATM +/-15 strikes, CE and PE.
    kotak_auto_chain_json: str = field(default_factory=lambda: _js('KOTAK_AUTO_CHAIN_JSON'))
    kotak_auto_chain_interval_sec: float = field(default_factory=lambda: _f('KOTAK_AUTO_CHAIN_INTERVAL_SEC', 30))
    kotak_auto_chain_max_tokens: int = field(default_factory=lambda: _i('KOTAK_AUTO_CHAIN_MAX_TOKENS', 1000))

    # --- portfolio limits (INR unless noted) -------------------------------
    portfolio_soft_sl: float = field(default_factory=lambda: _f('PORTFOLIO_SOFT_SL', 2000))
    portfolio_hard_sl: float = field(default_factory=lambda: _f('PORTFOLIO_HARD_SL', 4000))
    max_daily_loss: float = field(default_factory=lambda: _f('MAX_DAILY_LOSS', 4000))
    max_margin_utilization_pct: float = field(default_factory=lambda: _f('MAX_MARGIN_UTILIZATION_PCT', 70))
    max_net_exposure: float = field(default_factory=lambda: _f('MAX_NET_EXPOSURE', 1000000))
    max_short_option_notional: float = field(default_factory=lambda: _f('MAX_SHORT_OPTION_NOTIONAL', 2500000))
    max_position_qty: int = field(default_factory=lambda: _i('MAX_POSITION_QTY', 1000))
    max_order_value: float = field(default_factory=lambda: _f('MAX_ORDER_VALUE', 500000))
    max_slippage_bps: float = field(default_factory=lambda: _f('MAX_SLIPPAGE_BPS', 50))
    max_price_deviation_bps: float = field(default_factory=lambda: _f('MAX_PRICE_DEVIATION_BPS', 500))
    max_scenario_loss: float = field(default_factory=lambda: _f('MAX_SCENARIO_LOSS', 1000000))
    max_abs_delta: float = field(default_factory=lambda: _f('MAX_ABS_DELTA', 1000000))
    max_abs_vega: float = field(default_factory=lambda: _f('MAX_ABS_VEGA', 1000000))
    short_option_margin_pct: float = field(default_factory=lambda: _f('SHORT_OPTION_MARGIN_PCT', 18))
    max_orders_per_second: float = field(default_factory=lambda: _f('MAX_ORDERS_PER_SECOND', 5))
    risk_free_rate: float = field(default_factory=lambda: _f('RISK_FREE_RATE', 0.065))

    # --- order policy -------------------------------------------------------
    allow_market_orders: bool = field(default_factory=lambda: _b('ALLOW_MARKET_ORDERS', False))
    allow_naked_short_options: bool = field(default_factory=lambda: _b('ALLOW_NAKED_SHORT_OPTIONS', False))
    ai_gate_mode: str = field(default_factory=lambda: _s('AI_GATE_MODE', 'VETO_CONTRADICTION').upper())
    ai_window_sec: int = field(default_factory=lambda: _i('AI_WINDOW_SEC', 60))
    smart_routing_enabled: bool = field(default_factory=lambda: _b('SMART_ROUTING_ENABLED', True))
    routing_brokers: str = field(default_factory=lambda: _s('ROUTING_BROKERS', 'ZERODHA,KOTAK,UPSTOX,ANGEL'))

    # --- control loops ------------------------------------------------------
    watchdog_timeout_sec: int = field(default_factory=lambda: _i('WATCHDOG_TIMEOUT_SEC', 5))
    order_reconcile_interval_sec: int = field(default_factory=lambda: _i('ORDER_RECONCILE_INTERVAL_SEC', 2))
    reconcile_interval_sec: int = field(default_factory=lambda: _i('RECONCILE_INTERVAL_SEC', 30))
    risk_monitor_interval_sec: float = field(default_factory=lambda: _f('RISK_MONITOR_INTERVAL_SEC', 2))
    risk_snapshot_max_age_sec: float = field(default_factory=lambda: _f('RISK_SNAPSHOT_MAX_AGE_SEC', 10))
    reconciliation_max_age_sec: float = field(default_factory=lambda: _f('RECONCILIATION_MAX_AGE_SEC', 60))
    broker_health_ttl_sec: float = field(default_factory=lambda: _f('BROKER_HEALTH_TTL_SEC', 10))
    emergency_flatten_enabled: bool = field(default_factory=lambda: _b('EMERGENCY_FLATTEN_ENABLED', True))
    auto_flatten_on_hard_stop: bool = field(default_factory=lambda: _b('AUTO_FLATTEN_ON_HARD_STOP', False))
    flatten_limit_slippage_bps: float = field(default_factory=lambda: _f('FLATTEN_LIMIT_SLIPPAGE_BPS', 300))

    # --- broker credentials (runtime only; never commit) -------------------
    zerodha_api_key: str = field(default_factory=lambda: _s('ZERODHA_API_KEY'))
    zerodha_access_token: str = field(default_factory=lambda: _s('ZERODHA_ACCESS_TOKEN'))
    upstox_access_token: str = field(default_factory=lambda: _s('UPSTOX_ACCESS_TOKEN'))
    angel_api_key: str = field(default_factory=lambda: _s('ANGEL_API_KEY'))
    angel_access_token: str = field(default_factory=lambda: _s('ANGEL_ACCESS_TOKEN'))
    angel_client_code: str = field(default_factory=lambda: _s('ANGEL_CLIENT_CODE'))
    angel_feed_token: str = field(default_factory=lambda: _s('ANGEL_FEED_TOKEN'))
    kotak_api_key: str = field(default_factory=lambda: _s('KOTAK_API_KEY'))
    kotak_mobile: str = field(default_factory=lambda: _s('KOTAK_MOBILE'))
    kotak_client_code: str = field(default_factory=lambda: _s('KOTAK_CLIENT_CODE'))
    kotak_totp: str = field(default_factory=lambda: _s('KOTAK_TOTP'))
    kotak_totp_secret: str = field(default_factory=lambda: _s('KOTAK_TOTP_SECRET'))
    kotak_mpin: str = field(default_factory=lambda: _s('KOTAK_MPIN'))
    # A session lasts the IST trading day (it is replaced at day rollover or when Kotak ends it). The TTL is
    # only a backstop: a short one forced a midday re-login that also dropped the live feed.
    kotak_session_ttl_sec: int = field(default_factory=lambda: _i('KOTAK_SESSION_TTL_SEC', 20 * 3600))
    # Login backoff: failed logins wait base * 2^(n-1) seconds (capped) before the next attempt.
    # Credential rejections (wrong MPIN/TOTP) halt automatic login after this many in a row,
    # so the terminal cannot lock the account; an operator reset or a restart clears the halt.
    kotak_login_backoff_sec: float = field(default_factory=lambda: _f('KOTAK_LOGIN_BACKOFF_SEC', 30))
    kotak_login_backoff_max_sec: float = field(default_factory=lambda: _f('KOTAK_LOGIN_BACKOFF_MAX_SEC', 900))
    kotak_login_max_rejections: int = field(default_factory=lambda: _i('KOTAK_LOGIN_MAX_REJECTIONS', 2))
    # Network, gateway and maintenance failures never reach the credential check: they retry sooner
    # (5, 10, 20, 40 s, capped at 60 s) and never count toward the halt.
    kotak_login_transport_backoff_sec: float = field(default_factory=lambda: _f('KOTAK_LOGIN_TRANSPORT_BACKOFF_SEC', 5))
    kotak_login_transport_backoff_max_sec: float = field(default_factory=lambda: _f('KOTAK_LOGIN_TRANSPORT_BACKOFF_MAX_SEC', 60))
    kotak_login_step_timeout_sec: float = field(default_factory=lambda: _f('KOTAK_LOGIN_STEP_TIMEOUT_SEC', 20))
    # Session expiries that may trigger an automatic re-login within 15 minutes (prevents login storms).
    kotak_relogin_budget: int = field(default_factory=lambda: _i('KOTAK_RELOGIN_BUDGET', 4))
    # Session upkeep: a session from an earlier IST day is replaced, and on weekdays after this IST time
    # the terminal logs in by itself so the open starts with a fresh one ('' turns this off). After this
    # many failed calls in a row with no success, the session is treated as silently expired.
    kotak_prelogin_ist: str = field(default_factory=lambda: _s('KOTAK_PRELOGIN_IST', '08:50'))
    kotak_relogin_after_errors: int = field(default_factory=lambda: _i('KOTAK_RELOGIN_AFTER_ERRORS', 3))
    # Kotak accepts API orders only from the static public IP registered with it. With live order routing,
    # orders stay blocked unless this machine's public IP (looked up at PUBLIC_IP_URL) matches.
    registered_static_ip: str = field(default_factory=lambda: _s('REGISTERED_STATIC_IP'))
    require_static_ip_match: bool = field(default_factory=lambda: _b('REQUIRE_STATIC_IP_MATCH', True))
    public_ip_url: str = field(default_factory=lambda: _s('PUBLIC_IP_URL', 'https://api.ipify.org'))

    def validation_errors(self):
        """Configuration errors that must stop live trading."""
        errors = []
        if self.portfolio_soft_sl > self.portfolio_hard_sl:
            errors.append('SOFT_SL_GREATER_THAN_HARD_SL')
        for name in ('portfolio_soft_sl', 'portfolio_hard_sl', 'max_daily_loss', 'max_position_qty', 'max_order_value'):
            if getattr(self, name) <= 0:
                errors.append(f'{name.upper()}_MUST_BE_POSITIVE')
        if self.ai_gate_mode not in {'ADVISORY', 'VETO_CONTRADICTION', 'REQUIRE_AGREEMENT'}:
            errors.append('AI_GATE_MODE_INVALID')
        if self.live_trading and self.require_operator_auth and len(self.operator_api_token) < self.operator_token_min_length:
            errors.append('OPERATOR_API_TOKEN_TOO_SHORT')
        errors += [f'CONFIG_INVALID:{name}' for name in dict.fromkeys(CONFIG_ERRORS)]
        if DOTENV_PROBLEM:
            errors.append(DOTENV_PROBLEM)
        return errors


settings = Settings()
