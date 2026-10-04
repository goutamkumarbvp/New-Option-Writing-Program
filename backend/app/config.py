"""Runtime configuration.

Values are read from the environment once, when this module is imported.
Every live-trading switch defaults to the safe (disabled / fail-closed) value.
"""
import os
from dataclasses import dataclass, field


def _b(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in {'1', 'true', 'yes', 'on'}


def _i(name, default):
    return int(os.getenv(name, str(default)))


def _f(name, default):
    return float(os.getenv(name, str(default)))


def _s(name, default=''):
    return os.getenv(name, default)


@dataclass(frozen=True)
class Settings:
    # --- mode and authority -------------------------------------------------
    app_env: str = field(default_factory=lambda: _s('APP_ENV', 'production'))
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
    subscription_json: str = field(default_factory=lambda: _s('SUBSCRIPTION_JSON', '{}'))
    instrument_master_urls_json: str = field(default_factory=lambda: _s('INSTRUMENT_MASTER_URLS_JSON', '{}'))
    underlying_spot_tokens_json: str = field(default_factory=lambda: _s('UNDERLYING_SPOT_TOKENS_JSON', '{}'))
    # Kotak publishes a new scrip master every day under a dated path; the terminal finds
    # and loads it automatically (consumer key only, no login) and refreshes it daily.
    kotak_instrument_master_auto: bool = field(default_factory=lambda: _b('KOTAK_INSTRUMENT_MASTER_AUTO', True))
    kotak_scrip_segments: str = field(default_factory=lambda: _s('KOTAK_SCRIP_SEGMENTS', 'nse_cm,nse_fo,bse_fo,mcx_fo'))
    instrument_refresh_check_sec: float = field(default_factory=lambda: _f('INSTRUMENT_REFRESH_CHECK_SEC', 300))
    # Extra Kotak index-name -> F&O underlying mappings, e.g. {"Nifty IT": "NIFTYIT"} (built-ins cover
    # Nifty 50, Nifty Bank, Nifty Fin Service, Nifty Mid Select, Nifty Next 50, SENSEX, BANKEX).
    kotak_index_underlyings_json: str = field(default_factory=lambda: _s('KOTAK_INDEX_UNDERLYINGS_JSON', '{}'))
    # Option strikes kept subscribed around spot, per underlying:
    # {"NIFTY": {"expiries": 2, "strikes": 15}} = nearest 2 expiries, ATM +/-15 strikes, CE and PE.
    kotak_auto_chain_json: str = field(default_factory=lambda: _s('KOTAK_AUTO_CHAIN_JSON', '{}'))
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
    kotak_session_ttl_sec: int = field(default_factory=lambda: _i('KOTAK_SESSION_TTL_SEC', 6 * 3600))
    # Login backoff: failed logins wait base * 2^(n-1) seconds (capped) before the next attempt.
    # Credential rejections (wrong MPIN/TOTP) halt automatic login after this many in a row,
    # so the terminal cannot lock the account; an operator reset or a restart clears the halt.
    kotak_login_backoff_sec: float = field(default_factory=lambda: _f('KOTAK_LOGIN_BACKOFF_SEC', 30))
    kotak_login_backoff_max_sec: float = field(default_factory=lambda: _f('KOTAK_LOGIN_BACKOFF_MAX_SEC', 900))
    kotak_login_max_rejections: int = field(default_factory=lambda: _i('KOTAK_LOGIN_MAX_REJECTIONS', 2))

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
        return errors


settings = Settings()
