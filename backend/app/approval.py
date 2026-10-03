from .config import settings
class ApprovalGate:
    def evaluate(self,p,mode,manual_token_valid=False):
        reasons=[]
        if p.action=='NO_TRADE': reasons.append('NO_VALID_STRATEGY')
        if p.action!='NO_TRADE' and p.confidence<.60: reasons.append('CONFIDENCE_BELOW_THRESHOLD')
        if p.order is None and p.action!='NO_TRADE': reasons.append('ORDER_MISSING')
        if mode=='AUTO':
            if not settings.auto_trading_enabled: reasons.append('AUTO_TRADING_DISABLED')
            if settings.require_human_approval_auto and not manual_token_valid: reasons.append('HUMAN_APPROVAL_REQUIRED')
        return {'approved':not reasons,'reasons':reasons,'proposal_id':p.strategy_id}
