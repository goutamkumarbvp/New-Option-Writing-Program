from dataclasses import dataclass
@dataclass
class AgentResult:
    name:str; score:float; evidence:dict; state:str
class Agent:
    def __init__(self,name): self.name=name
    def run(self,ctx):
        m=ctx.get("market",{}); p=ctx.get("portfolio",{}); live=bool(m.get("live"))
        flow=m.get("flow",{}) or {}; label=str(flow.get("classification","INSUFFICIENT_DATA"))
        price=float(m.get("price_change_pct",0) or 0); iv=float(m.get("iv_change_pct",0) or 0); spread=float(m.get("spread_bps",0) or 0)
        if self.name=="REGIME": score=max(0,min(1,.5+price*.05)); ev={"price_change_pct":price,"regime":"TREND" if abs(price)>=.5 else "RANGE"}
        elif self.name=="FLOW": score={"POSSIBLE_LONG_BUILDUP":.8,"POSSIBLE_SHORT_COVERING":.75,"POSSIBLE_WRITING":.25,"POSSIBLE_LONG_UNWINDING":.2}.get(label,0) if live else 0; ev={"classification":label,"oi_delta":flow.get("oi_delta",0),"premium_delta":flow.get("premium_delta",0)}
        elif self.name=="VOLATILITY": score=max(0,min(1,.5-iv*.03)) if live else 0; ev={"iv_change_pct":iv}
        elif self.name=="DIRECTION": score=max(0,min(1,.5+price*.08+(.15 if label in ("POSSIBLE_LONG_BUILDUP","POSSIBLE_SHORT_COVERING") else -.15 if label in ("POSSIBLE_WRITING","POSSIBLE_LONG_UNWINDING") else 0))) if live else 0; ev={"price_change_pct":price,"flow":label}
        elif self.name=="STRUCTURE": score=.75 if live and m.get("order") and spread<=50 else .3 if live else 0; ev={"liquid":bool(m.get("liquid")),"spread_bps":spread}
        elif self.name=="EXECUTION": score=max(0,min(1,1-spread/100)) if live else 0; ev={"spread_bps":spread}
        elif self.name=="RISK": blocked=bool(p.get("risk_blocked",False)); score=0 if blocked or not live else 1; ev={"risk_blocked":blocked,"daily_pnl":p.get("daily_pnl",0),"margin_pct":p.get("margin_pct",0)}
        else: anomaly=bool(m.get("anomaly")) or not live; score=0 if anomaly else .9; ev={"anomaly":anomaly}
        return AgentResult(self.name,float(score),ev,"LIVE" if live else "UNAVAILABLE")
class AgentOrchestrator:
    NAMES=["REGIME","DIRECTION","FLOW","VOLATILITY","STRUCTURE","EXECUTION","RISK","ANOMALY"]
    def __init__(self): self.agents=[Agent(n) for n in self.NAMES]
    def run(self,ctx): return [a.run(ctx).__dict__ for a in self.agents]
