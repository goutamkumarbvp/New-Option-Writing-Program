from dataclasses import dataclass
import uuid
@dataclass
class StrategyProposal:
    strategy_id:str; symbol:str; action:str; confidence:float; max_loss:float; evidence:dict; order:object=None; expected_edge:float=0.; estimated_cost:float=0.
class StrategyEngine:
    def propose(self,ctx):
        m=ctx.get("market",{}); sid=str(uuid.uuid4()); symbol=m.get("symbol","UNKNOWN")
        if not m.get("live"): return StrategyProposal(sid,symbol,"NO_TRADE",0,0,{"reason":"DATA_UNAVAILABLE"})
        by={x["name"]:x for x in ctx.get("agents",[])}
        if any(by.get(n,{}).get("state")!="LIVE" for n in ("DIRECTION","FLOW","EXECUTION","RISK")):
            return StrategyProposal(sid,symbol,"NO_TRADE",0,0,{"reason":"AGENT_EVIDENCE_INCOMPLETE"})
        d=float(by["DIRECTION"]["score"]); f=float(by["FLOW"]["score"]); ex=float(by["EXECUTION"]["score"]); risk=float(by["RISK"]["score"])
        cost=max(0,float(m.get("spread_bps",0) or 0)/10000); edge=max(0,abs(d-.5)*2*f*ex*risk)
        action="BUY_CE" if d>=.70 and f>=.50 else "BUY_PE" if d<=.30 and f>=.50 else "NO_TRADE"
        if edge <= cost*1.5: action="NO_TRADE"
        order=m.get("order") if action!="NO_TRADE" else None
        return StrategyProposal(sid,symbol,action,max(0,min(1,(d+f+ex+risk)/4)),float(m.get("max_loss",0) or 0),{"agents":ctx["agents"],"edge":edge,"cost":cost,"market_inputs":m},order,edge,cost)
