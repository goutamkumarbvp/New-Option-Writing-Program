from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field
class Mode(str,Enum): MANUAL='MANUAL'; AUTO='AUTO'
class Tick(BaseModel):
    broker:str; exchange:str; instrument_token:str; symbol:str; underlying:Optional[str]=None; expiry:Optional[str]=None; strike:Optional[float]=None; option_type:Optional[str]=None
    ltp:float=0; bid:float=0; ask:float=0; volume:int=0; oi:int=0; iv:Optional[float]=None; sequence:Optional[int]=None; exchange_ts_ms:int=0; receive_ts_ms:int=0
class OrderRequest(BaseModel):
    broker:str; exchange:str; symbol:str; side:str; qty:int=Field(gt=0); price:Optional[float]=None; order_type:str='MARKET'; product:str='NRML'; instrument_token:Optional[str]=None; mode:Mode=Mode.MANUAL; strategy_id:Optional[str]=None; tag:str='IORT'; client_order_id:Optional[str]=None
class StrategyProposal(BaseModel):
    strategy_id:str; symbol:str; action:str; confidence:float; max_loss:float; evidence:dict; order:Optional[OrderRequest]=None
class Approval(BaseModel): approved:bool; reasons:list[str]; proposal_id:str
class RiskDecision(BaseModel): approved:bool; reasons:list[str]; risk_state:str; soft_sl_distance:float; hard_sl_distance:float; monitor_agreement:bool

class OrderStatus(str, Enum):
    NEW='NEW'; SUBMITTED='SUBMITTED'; OPEN='OPEN'; PARTIAL='PARTIAL'; FILLED='FILLED'; CANCELLED='CANCELLED'; REJECTED='REJECTED'; UNKNOWN='UNKNOWN'
