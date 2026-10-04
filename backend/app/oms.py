"""Order service: every pre-trade control, the write-ahead record, submission,
cancel-all and flatten.

Pre-trade evaluation, the write-ahead insert and the broker call run under a
per-broker lock, so two concurrent orders cannot both pass a limit that only
one of them fits under. The client_order_id primary key makes idempotency
atomic even across processes sharing the database.
"""
import asyncio
import json
import time
import uuid

from .approval import order_direction
from .brokers import order_tag
from .clock import expired
from .config import settings
from .engine import EngineContext
from .scenario import revalue


class Throttle:
    """Token bucket for orders per second (exchange / broker OPS limits)."""

    def __init__(self, rate):
        self.rate = max(rate, 0.01)
        self.tokens = self.rate
        self.at = time.monotonic()

    def allow(self):
        now = time.monotonic()
        self.tokens = min(self.rate, self.tokens + (now - self.at) * self.rate)
        self.at = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


def _round_tick(px, tick=0.05):
    return round(round(px / tick) * tick, 2)


class Blocked(Exception):
    def __init__(self, reason, **extra):
        super().__init__(reason)
        self.reason = reason
        self.extra = extra


class OrderService:
    def __init__(self, t):
        self.t = t  # Terminal container (see main.py)
        self.locks = {}
        self.throttle = Throttle(settings.max_orders_per_second)

    def _lock(self, broker):
        return self.locks.setdefault(broker, asyncio.Lock())

    async def _block(self, o, reason, **extra):
        payload = {'reason': reason, 'broker': getattr(o, 'broker', None), 'symbol': getattr(o, 'symbol', None),
                   'side': getattr(o, 'side', None), 'qty': getattr(o, 'qty', None), **extra}
        self.t.ledger.event('RISK_BLOCK', payload)
        await self.t.emit({'type': 'RISK_BLOCK', 'payload': payload})
        return {'status': 'BLOCKED', 'reason': reason, **extra}

    # ------------------------------------------------------------------ submit
    async def submit(self, o, operator_valid):
        t = self.t
        if settings.live_trading and not operator_valid:
            return await self._block(o, 'OPERATOR_AUTH_REQUIRED')
        if t.kill.triggered:
            return await self._block(o, 'KILL_SWITCH', kill=t.kill.state())
        if not settings.live_trading:
            return await self._block(o, 'LIVE_TRADING_DISABLED')
        if o.mode.value == 'AUTO' and not settings.auto_trading_enabled:
            return await self._block(o, 'AUTO_TRADING_DISABLED')
        errs = settings.validation_errors()
        if errs:
            return await self._block(o, 'CONFIGURATION_INVALID', errors=errs)
        if o.client_order_id:
            existing = t.ledger.get_order(o.client_order_id)
            if existing:
                return {'status': 'IDEMPOTENT_REPLAY', 'order': existing}
        if settings.smart_routing_enabled:
            selected, route_meta = await t.router.select(o.broker, t.feed)
        else:
            selected, route_meta = o.broker, {'mode': 'DISABLED'}
        if not selected:
            return await self._block(o, 'NO_HEALTHY_ROUTE', routing=route_meta)
        if selected != o.broker:
            o = o.model_copy(update={'broker': selected})
        if o.broker not in t.brokers.items:
            return await self._block(o, 'UNKNOWN_BROKER')
        broker = t.brokers.get(o.broker)
        async with self._lock(o.broker):
            try:
                ctx = await self._pretrade(o, broker, operator_valid)
            except Blocked as b:
                return await self._block(o, b.reason, **b.extra)
            except Exception as exc:  # noqa: BLE001 - any evidence failure blocks the order
                return await self._block(o, 'LIVE_RISK_EVIDENCE_FAILED', error=str(exc)[:300])
            if t.kill.triggered:  # the risk monitor may have engaged it while this order was being evaluated
                return await self._block(o, 'KILL_SWITCH', kill=t.kill.state())
            return await self._send(o, broker, ctx, route_meta, operator_valid)

    # ------------------------------------------------------------- pre-trade
    async def _pretrade(self, o, broker, operator_valid):
        t = self.t
        if not broker.configured():
            raise Blocked('BROKER_NOT_CONFIGURED')
        if t.ledger.ambiguous_orders(o.broker):
            raise Blocked('AMBIGUOUS_ORDERS_UNRESOLVED', orders=[x['client_order_id'] for x in t.ledger.ambiguous_orders(o.broker)])
        if not self.throttle.allow():
            raise Blocked('ORDER_RATE_LIMIT')
        if o.order_type in ('MARKET', 'SL-M') and not settings.allow_market_orders:
            raise Blocked('MARKET_ORDERS_DISABLED')

        # Instrument master
        rec = t.instruments.get(o.broker, o.instrument_token)
        if settings.require_instrument_master:
            if not rec:
                raise Blocked('INSTRUMENT_MASTER_TOKEN_NOT_FOUND')
            if not t.instruments.fresh_today(o.broker):
                raise Blocked('INSTRUMENT_MASTER_STALE')
        rec = rec or {}
        if rec:
            if rec.get('symbol') and rec['symbol'].upper() != o.symbol.upper():
                raise Blocked('INSTRUMENT_SYMBOL_MISMATCH', master_symbol=rec['symbol'])
            if rec.get('exchange') and rec['exchange'] != o.exchange:
                raise Blocked('INSTRUMENT_EXCHANGE_MISMATCH', master_exchange=rec['exchange'])
            lot = int(rec.get('lot_size') or 1)
            if o.qty % lot:
                raise Blocked('LOT_SIZE_VIOLATION', lot_size=lot)
            if expired(rec.get('expiry')):
                raise Blocked('INSTRUMENT_EXPIRED', expiry=rec.get('expiry'))
            tick = rec.get('tick_size') if o.broker == 'ZERODHA' else None
            if tick and o.price and abs(round(o.price / tick) * tick - o.price) > 1e-6:
                raise Blocked('TICK_SIZE_VIOLATION', tick_size=tick)

        # Market evidence
        tk = t.feed.tick(o.broker, o.instrument_token)
        if not tk or not t.feed.live_instrument(o.broker, o.instrument_token):
            raise Blocked('INSTRUMENT_DATA_UNAVAILABLE')
        if tk.bid <= 0 or tk.ask <= 0:
            raise Blocked('NO_TWO_SIDED_QUOTE')
        spread_bps = (tk.ask - tk.bid) / tk.ltp * 10000
        if spread_bps > settings.max_slippage_bps:
            raise Blocked('SLIPPAGE_LIMIT', spread_bps=round(spread_bps, 2))
        ref = tk.ask if o.side == 'BUY' else tk.bid
        if o.price is not None:
            dev = abs(o.price - tk.ltp) / tk.ltp * 10000
            if dev > settings.max_price_deviation_bps:
                raise Blocked('PRICE_COLLAR_BREACH', deviation_bps=round(dev, 1), ltp=tk.ltp)
        est_px = o.price or ref
        if est_px * o.qty > settings.max_order_value:
            raise Blocked('ORDER_VALUE_LIMIT', estimated_value=est_px * o.qty)

        gate = await t.readiness.check(o.broker, o.instrument_token)
        if not gate['ready']:
            raise Blocked('PRODUCTION_READINESS_FAILED', readiness=gate['reasons'])

        # Reconciliation evidence
        last = t.ledger.last_reconciliation(o.broker)
        if not last or last['age_sec'] > settings.reconciliation_max_age_sec:
            last = await t.reconciler.reconcile(broker, t.ledger)
        if not last['ok']:
            raise Blocked('BROKER_RECONCILIATION_REQUIRED', mismatches=last.get('mismatches'))

        # Broker-authoritative book
        snap = t.risk_monitor.fresh(o.broker) or await t.risk_monitor.refresh(broker)
        if not snap.ok:
            raise Blocked('LIVE_RISK_EVIDENCE_FAILED', error=snap.error, issues=snap.issues[:20])
        for other in t.brokers.configured():
            if other.name != o.broker:
                s = t.risk_monitor.fresh(other.name) or await t.risk_monitor.refresh(other)
                if not s.ok:
                    raise Blocked('FIRM_RISK_INCOMPLETE', broker=other.name, error=s.error, issues=s.issues[:5])
        agg = t.risk_monitor.aggregate()
        if agg['total_pnl'] <= -settings.portfolio_hard_sl:
            t.kill.trigger('HARD_PORTFOLIO_STOP', 'PRETRADE')
            raise Blocked('HARD_PORTFOLIO_STOP', total_pnl=agg['total_pnl'])
        if agg['total_pnl'] <= -settings.portfolio_soft_sl:
            raise Blocked('SOFT_PORTFOLIO_STOP', total_pnl=agg['total_pnl'])
        if settings.require_authoritative_daily_pnl:
            weak = [s.broker for s in t.risk_monitor.snapshots.values() if s.ok and s.day_pnl_source != 'BROKER']
            if weak:
                raise Blocked('DAILY_PNL_NOT_AUTHORITATIVE', brokers=weak)
        if agg['day_pnl'] <= -settings.max_daily_loss:
            raise Blocked('DAILY_LOSS_LIMIT', day_pnl=agg['day_pnl'])

        # Projected position including working orders on the same instrument (worst case: all fill)
        signed = o.qty if o.side == 'BUY' else -o.qty
        cur = (snap.position(o.instrument_token) or {}).get('qty', 0)
        working = [w for w in t.ledger.working_orders(o.broker) if w['instrument_token'] == o.instrument_token]
        pending = sum((w['qty'] - (w['filled_qty'] or 0)) * (1 if w['side'] == 'BUY' else -1) for w in working)
        projected = cur + pending + signed
        opening = abs(projected) > abs(cur + pending)
        increase = abs(projected) - abs(cur + pending) if opening else 0
        if abs(projected) > settings.max_position_qty:
            raise Blocked('POSITION_LIMIT', projected_qty=projected)

        # Self-trade prevention against our own working orders
        for w in working:
            if w['side'] != o.side:
                buy_px = o.price if o.side == 'BUY' else w['price']
                sell_px = w['price'] if o.side == 'BUY' else o.price
                if buy_px is None or sell_px is None or buy_px >= sell_px:
                    raise Blocked('SELF_TRADE_RISK', conflicting_order=w['client_order_id'])

        is_option = rec.get('option_type') in ('CE', 'PE')
        new_exposure = agg['premium_exposure'] + (est_px * increase if opening else 0)
        if new_exposure > settings.max_net_exposure:
            raise Blocked('NET_EXPOSURE_LIMIT', projected_exposure=new_exposure)
        short_add = 0.0
        if is_option and projected < 0 and opening:
            if not rec.get('strike'):
                raise Blocked('STRIKE_UNKNOWN_FOR_SHORT_OPTION')
            short_add = rec['strike'] * increase
            if agg['short_option_notional'] + short_add > settings.max_short_option_notional:
                raise Blocked('SHORT_OPTION_NOTIONAL_LIMIT', projected=agg['short_option_notional'] + short_add)
            if not settings.allow_naked_short_options:
                self._require_hedge(o, rec, snap, projected)

        # Margin: current evidence plus this order's requirement
        if snap.margin is None:
            if settings.require_margin_evidence:
                raise Blocked('MARGIN_EVIDENCE_UNAVAILABLE', error=snap.margin_error)
            required, margin_source, post_pct = 0.0, 'NONE', None
        else:
            required, margin_source = 0.0, 'NOT_REQUIRED'
            if opening:
                broker_req = await broker.order_margin(dict(o.model_dump(), client_order_id=o.client_order_id or 'MARGIN-CHECK'))
                if broker_req is not None:
                    required, margin_source = broker_req, 'BROKER'
                else:
                    if settings.require_broker_order_margin:
                        raise Blocked('BROKER_ORDER_MARGIN_UNAVAILABLE')
                    if is_option and o.side == 'SELL':
                        required = settings.short_option_margin_pct / 100 * (rec.get('strike') or est_px) * increase
                    elif str(rec.get('instrument_type', '')).upper().startswith('FUT'):
                        required = 0.15 * est_px * increase
                    else:
                        required = est_px * increase
                    margin_source = 'ESTIMATE'
            m = snap.margin
            post_pct = 100.0 * (m['used'] + required) / (m['used'] + max(m['available'], 0.0))
            if post_pct > settings.max_margin_utilization_pct:
                raise Blocked('MARGIN_LIMIT', post_trade_margin_pct=round(post_pct, 2), required=required, source=margin_source)

        # Scenario risk (full revaluation)
        scen = None
        if settings.require_scenario_risk:
            scen = self._scenario(o, rec, snap, est_px, signed)
            if not scen['ok']:
                raise Blocked('SCENARIO_RISK_EVIDENCE_UNAVAILABLE', errors=scen.get('errors'))
            if scen['worst_pnl'] < -settings.max_scenario_loss:
                raise Blocked('SCENARIO_RISK_LIMIT', worst_pnl=scen['worst_pnl'])

        # Evidence-scorer gate
        window = settings.ai_window_sec * 1000
        dp, _ = t.feed.window_change(o.broker, o.instrument_token, window)
        market = {'live': True, 'stale': False, 'symbol': o.symbol, 'order': o, 'liquid': True,
                  'flow': t.feed.flow(o.broker, o.instrument_token, window), 'price_change_pct': dp,
                  'iv_change_pct': None, 'spread_bps': spread_bps}
        portfolio = {'net_pnl': agg['total_pnl'], 'daily_pnl': agg['day_pnl'],
                     'margin_pct': post_pct if snap.margin else None, 'net_exposure': new_exposure,
                     'slippage_bps': spread_bps, 'broker_reconciled': True, 'risk_blocked': False}
        direction = order_direction(o.side, rec.get('option_type'))
        decision = t.pipeline.evaluate(EngineContext(market=market, portfolio=portfolio, broker_state={'reconciled': True},
                                                     mode=o.mode.value), operator_valid, direction)
        if not decision['approved']:
            raise Blocked('DECISION_BLOCKED', approval=decision['approval']['reasons'], risk=decision['risk']['reasons'])
        return {'arrival_price': ref, 'estimated_price': est_px, 'projected_qty': projected, 'margin_required': required,
                'margin_source': margin_source, 'post_trade_margin_pct': post_pct, 'scenario_worst': scen and scen['worst_pnl'],
                'decision': {'view': decision['approval']['view'], 'direction': direction,
                             'confidence': decision['approval']['confidence'], 'gate_mode': decision['approval']['gate_mode']}}

    def _require_hedge(self, o, rec, snap, projected):
        key = (rec.get('underlying'), rec.get('expiry'), rec.get('option_type'))
        if not all(key):
            raise Blocked('NAKED_SHORT_CHECK_UNAVAILABLE', reason='instrument metadata incomplete')
        longs = shorts = 0
        for p in snap.positions:
            if (p.get('underlying'), p.get('expiry'), p.get('option_type')) != key or p['token'] == str(o.instrument_token):
                continue
            if p['qty'] > 0:
                longs += p['qty']
            else:
                shorts += -p['qty']
        shorts += -projected
        if longs < shorts:
            raise Blocked('NAKED_SHORT_OPTION_BLOCKED', hedge_long_qty=longs, short_qty=shorts,
                          rule='each short option needs an equal long option of the same underlying, expiry and type')

    def _scenario(self, o, rec, snap, est_px, signed):
        spots = {}
        try:
            mapping = json.loads(settings.underlying_spot_tokens_json or '{}')
        except ValueError:
            mapping = {}
        for und, by_broker in mapping.items():
            tok = (by_broker or {}).get(o.broker)
            tk = self.t.feed.tick(o.broker, tok) if tok else None
            if tk and self.t.feed.live_instrument(o.broker, tok):
                spots[und] = tk.ltp
        legs = [dict(p) for p in snap.positions if p['qty']]
        legs.append({**rec, 'token': o.instrument_token, 'symbol': o.symbol, 'qty': signed, 'price': est_px})
        for und in {leg.get('underlying') for leg in legs if leg.get('option_type') in ('CE', 'PE')} - set(spots):
            tk = self.t.feed.live_spot(und) if und else None  # a streamed index or cash tick of the underlying
            if tk:
                spots[und] = tk.ltp
        return revalue(legs, spots)

    # ------------------------------------------------------------------ send
    async def _send(self, o, broker, ctx, route_meta, operator_valid):
        t = self.t
        client_id = o.client_order_id or uuid.uuid4().hex
        req = o.model_dump()
        req.update(client_order_id=client_id, tag=order_tag(client_id), mode=o.mode.value, arrival_price=ctx['arrival_price'])
        if not t.ledger.create_pending_order(req):
            return {'status': 'IDEMPOTENT_REPLAY', 'order': t.ledger.get_order(client_id)}
        result = await t.router.route(req)
        status = result.get('status', 'UNKNOWN')
        if status not in ('SUBMITTED', 'REJECTED', 'UNKNOWN'):
            status = 'UNKNOWN'
        t.ledger.transition(client_id, status, reason=result.get('reason'), broker_order_id=result.get('broker_order_id'))
        payload = {'client_order_id': client_id, 'broker': o.broker, 'symbol': o.symbol, 'side': o.side, 'qty': o.qty,
                   'status': status, 'broker_order_id': result.get('broker_order_id'), 'reason': result.get('reason'),
                   'routing': route_meta, 'pretrade': ctx}
        t.ledger.event('ORDER_SUBMISSION', payload)
        t.audit.append('ORDER_SUBMISSION', 'OPERATOR' if operator_valid else 'SYSTEM',
                       {'client_order_id': client_id, 'broker': o.broker, 'status': status})
        await t.emit({'type': 'ORDER_SUBMISSION', 'payload': payload})
        out = {'client_order_id': client_id, 'status': status, 'broker': o.broker,
               'broker_order_id': result.get('broker_order_id'), 'reason': result.get('reason'), 'pretrade': ctx}
        if status == 'UNKNOWN':
            out['action_required'] = 'Order may be live at the broker. New orders on this broker are blocked until reconciliation resolves it.'
        return out

    # --------------------------------------------------- cancel-all / flatten
    async def cancel_order(self, client_order_id, actor):
        """Cancel one working order at its broker. Risk-reducing, so the kill switch does not
        block it. The ledger status moves when the order monitor reads the broker's book;
        this call records the request and returns the broker's answer."""
        t = self.t
        o = t.ledger.get_order(client_order_id)
        if not o:
            return 404, {'status': 'NOT_FOUND'}
        if o['status'] not in ('SUBMITTED', 'OPEN', 'PARTIAL'):
            return 409, {'status': 'BLOCKED', 'reason': 'ORDER_NOT_WORKING', 'order_status': o['status']}
        if not o.get('broker_order_id'):
            return 409, {'status': 'BLOCKED', 'reason': 'BROKER_ORDER_ID_UNKNOWN'}
        try:
            broker = t.brokers.get(o['broker'])
        except KeyError:
            return 404, {'status': 'BLOCKED', 'reason': 'UNKNOWN_BROKER'}
        try:
            resp = await broker.cancel(o['broker_order_id'])
        except Exception as exc:  # noqa: BLE001 - the order may still be live; report, never assume
            resp = {'status': 'CANCEL_FAILED', 'error': f'{type(exc).__name__}:{str(exc)[:160]}'}
        payload = {'client_order_id': client_order_id, 'broker': o['broker'], 'broker_order_id': o['broker_order_id'],
                   'symbol': o['symbol'], 'result': resp.get('status')}
        t.ledger.event('ORDER_CANCEL_REQUEST', {**payload, 'response': resp})
        t.audit.append('ORDER_CANCEL_REQUEST', actor, payload)
        await t.emit({'type': 'ORDER_CANCEL_REQUEST', 'payload': payload})
        return (200 if resp.get('status') == 'CANCEL_REQUESTED' else 502), {**payload, 'response': resp}

    async def cancel_all_brokers(self, actor):
        out = {}
        for b in self.t.brokers.configured():
            try:
                out[b.name] = await b.cancel_all()
            except Exception as exc:  # noqa: BLE001
                out[b.name] = {'status': 'CANCEL_ALL_FAILED', 'error': str(exc)[:200]}
        self.t.ledger.event('CANCEL_ALL', {'actor': actor, 'results': out})
        self.t.audit.append('CANCEL_ALL', actor, {'brokers': list(out)})
        return out

    async def flatten_broker(self, broker, actor):
        """Close every open position on one broker with marketable LIMIT orders,
        buying back shorts before selling longs so no leg is left naked."""
        snap = await self.t.risk_monitor.refresh(broker)
        if snap.error:
            return {'status': 'FLATTEN_FAILED', 'error': snap.error}
        results = []
        for p in sorted((p for p in snap.positions if p['qty']), key=lambda p: p['qty']):
            side = 'BUY' if p['qty'] < 0 else 'SELL'
            px = p.get('price')
            if not px:
                results.append({'symbol': p['symbol'], 'status': 'SKIPPED', 'reason': 'NO_PRICE'})
                continue
            slip = settings.flatten_limit_slippage_bps / 10000
            limit = _round_tick(px * (1 + slip) if side == 'BUY' else max(px * (1 - slip), 0.05))
            cid = 'FLAT' + uuid.uuid4().hex[:24]
            req = {'client_order_id': cid, 'broker': broker.name, 'exchange': p['exchange'], 'symbol': p['symbol'],
                   'instrument_token': p['token'], 'side': side, 'qty': abs(p['qty']), 'price': limit, 'order_type': 'LIMIT',
                   'product': p.get('product') or 'NRML', 'tag': order_tag(cid), 'arrival_price': px}
            self.t.ledger.create_pending_order(req)
            r = await self.t.router.route(req)
            status = r.get('status') if r.get('status') in ('SUBMITTED', 'REJECTED', 'UNKNOWN') else 'UNKNOWN'
            self.t.ledger.transition(cid, status, reason=r.get('reason'), broker_order_id=r.get('broker_order_id'))
            results.append({'symbol': p['symbol'], 'side': side, 'qty': abs(p['qty']), 'price': limit, 'status': status,
                            'client_order_id': cid})
        self.t.ledger.event('FLATTEN', {'broker': broker.name, 'actor': actor, 'results': results})
        self.t.audit.append('FLATTEN', actor, {'broker': broker.name, 'orders': len(results)})
        return {'status': 'FLATTEN_SUBMITTED', 'results': results}

    async def flatten_all(self, actor):
        return {b.name: await self.flatten_broker(b, actor) for b in self.t.brokers.configured()}
