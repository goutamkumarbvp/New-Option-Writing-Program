import time
from dataclasses import asdict, dataclass, field

from .ai import AgentOrchestrator
from .approval import ApprovalGate
from .risk import ProductionDualRiskEngine
from .strategy import StrategyEngine


@dataclass
class EngineContext:
    market: dict = field(default_factory=dict)
    portfolio: dict = field(default_factory=dict)
    broker_state: dict = field(default_factory=dict)
    mode: str = 'MANUAL'
    now_ms: int = field(default_factory=lambda: int(time.time() * 1000))


class DecisionPipeline:
    def __init__(self):
        self.agents = AgentOrchestrator()
        self.strategy = StrategyEngine()
        self.approval = ApprovalGate()
        self.risk = ProductionDualRiskEngine()

    def evaluate(self, ctx, approval_token=None, direction=None):
        ar = self.agents.run({'market': ctx.market, 'portfolio': ctx.portfolio})
        p = self.strategy.propose({'market': ctx.market, 'portfolio': ctx.portfolio, 'agents': ar})
        a = self.approval.evaluate(p, ctx.mode, bool(approval_token), direction)
        r = self.risk.evaluate_context(ctx, p)
        prop = asdict(p)
        prop['order'] = p.order.model_dump() if hasattr(p.order, 'model_dump') else p.order
        return {'agents': ar, 'proposal': prop, 'approval': a, 'risk': r, 'approved': a['approved'] and r['approved']}
