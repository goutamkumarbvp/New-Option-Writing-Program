from .config import settings

CONFIDENCE = 0.6


def order_direction(side, option_type=None):
    """BUY CE / SELL PE are bullish; BUY PE / SELL CE are bearish; otherwise BUY is bullish."""
    side = str(side).upper()
    if option_type == 'PE':
        return 'BEARISH' if side == 'BUY' else 'BULLISH'
    return 'BULLISH' if side == 'BUY' else 'BEARISH'


class ApprovalGate:
    def evaluate(self, p, mode, manual_token_valid=False, direction=None):
        reasons = []
        gate = settings.ai_gate_mode
        if mode == 'AUTO':
            if not settings.auto_trading_enabled:
                reasons.append('AUTO_TRADING_DISABLED')
            if settings.require_human_approval_auto and not manual_token_valid:
                reasons.append('HUMAN_APPROVAL_REQUIRED')
        if direction:
            if gate == 'REQUIRE_AGREEMENT' and (p.action != direction or p.confidence < CONFIDENCE):
                reasons.append('AI_SIGNAL_NOT_IN_AGREEMENT')
            elif gate == 'VETO_CONTRADICTION' and p.action in ('BULLISH', 'BEARISH') and p.action != direction \
                    and p.confidence >= CONFIDENCE:
                reasons.append('AI_SIGNAL_CONTRADICTS_ORDER')
        return {'approved': not reasons, 'reasons': reasons, 'proposal_id': p.strategy_id, 'gate_mode': gate,
                'view': p.action, 'order_direction': direction, 'confidence': p.confidence}
