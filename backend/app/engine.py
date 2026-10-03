from dataclasses import dataclass,field
import time
from dataclasses import asdict
from .ai import AgentOrchestrator
from .strategy import StrategyEngine
from .risk import ProductionDualRiskEngine
from .approval import ApprovalGate
@dataclass
class EngineContext:market:dict=field(default_factory=dict);portfolio:dict=field(default_factory=dict);broker_state:dict=field(default_factory=dict);mode:str='MANUAL';now_ms:int=field(default_factory=lambda:int(time.time()*1000))
class DecisionPipeline:
    def __init__(self):self.agents=AgentOrchestrator();self.strategy=StrategyEngine();self.approval=ApprovalGate();self.risk=ProductionDualRiskEngine()
    def evaluate(self,ctx,approval_token=None):
        ar=self.agents.run({'market':ctx.market,'portfolio':ctx.portfolio});p=self.strategy.propose({'market':ctx.market,'portfolio':ctx.portfolio,'agents':ar});a=self.approval.evaluate(p,ctx.mode,approval_token);r=self.risk.evaluate_context(ctx,p)
        return {'agents':ar,'proposal':asdict(p),'approval':a,'risk':r,'approved':a['approved'] and r['approved']}
