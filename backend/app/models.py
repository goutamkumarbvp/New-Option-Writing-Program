from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

EXCHANGES = ('NSE', 'BSE', 'NFO', 'BFO', 'MCX')
PRODUCTS = ('NRML', 'MIS', 'CNC')
ORDER_TYPES = ('MARKET', 'LIMIT', 'SL', 'SL-M')


class Mode(str, Enum):
    MANUAL = 'MANUAL'
    AUTO = 'AUTO'


class Tick(BaseModel):
    broker: str
    exchange: str
    instrument_token: str
    symbol: str
    underlying: Optional[str] = None
    expiry: Optional[str] = None
    strike: Optional[float] = None
    option_type: Optional[str] = None
    ltp: float = 0
    bid: float = 0
    ask: float = 0
    volume: int = 0
    oi: int = 0
    iv: Optional[float] = None
    sequence: Optional[int] = None
    exchange_ts_ms: int = 0
    receive_ts_ms: int = 0


class OrderRequest(BaseModel):
    """Canonical order request. Broker-specific vocabulary is mapped in brokers.py."""
    broker: str = Field(min_length=1, max_length=16)
    exchange: Literal['NSE', 'BSE', 'NFO', 'BFO', 'MCX']
    symbol: str = Field(min_length=1, max_length=64)
    side: Literal['BUY', 'SELL']
    qty: int = Field(gt=0)
    price: Optional[float] = Field(default=None, gt=0)
    trigger_price: Optional[float] = Field(default=None, gt=0)
    order_type: Literal['MARKET', 'LIMIT', 'SL', 'SL-M'] = 'LIMIT'
    product: Literal['NRML', 'MIS', 'CNC'] = 'NRML'
    instrument_token: str = Field(min_length=1, max_length=64)
    mode: Mode = Mode.MANUAL
    strategy_id: Optional[str] = Field(default=None, max_length=64)
    tag: str = Field(default='IORT', max_length=20)
    client_order_id: Optional[str] = Field(default=None, pattern=r'^[A-Za-z0-9_\-]{1,64}$')

    @field_validator('broker', 'exchange', 'side', 'order_type', 'product', mode='before')
    @classmethod
    def _upper(cls, v):
        return v.strip().upper() if isinstance(v, str) else v

    @model_validator(mode='after')
    def _prices(self):
        if self.order_type in ('LIMIT', 'SL') and self.price is None:
            raise ValueError('LIMIT_PRICE_REQUIRED')
        if self.order_type in ('SL', 'SL-M') and self.trigger_price is None:
            raise ValueError('TRIGGER_PRICE_REQUIRED')
        return self


class OrderStatus(str, Enum):
    PENDING_SUBMIT = 'PENDING_SUBMIT'
    SUBMITTED = 'SUBMITTED'
    OPEN = 'OPEN'
    PARTIAL = 'PARTIAL'
    FILLED = 'FILLED'
    CANCELLED = 'CANCELLED'
    REJECTED = 'REJECTED'
    EXPIRED = 'EXPIRED'
    UNKNOWN = 'UNKNOWN'


class StrategyProposal(BaseModel):
    strategy_id: str
    symbol: str
    action: str
    confidence: float
    max_loss: float
    evidence: dict
    order: Optional[OrderRequest] = None


class Approval(BaseModel):
    approved: bool
    reasons: list[str]
    proposal_id: str


class RiskDecision(BaseModel):
    approved: bool
    reasons: list[str]
    risk_state: str
    soft_sl_distance: float
    hard_sl_distance: float
    monitor_agreement: bool
