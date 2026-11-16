"""Period-ledger responsibility.

Consumes the reading-chain verification closure and owns:
  * open-period recomputation (quantities and held state),
  * frozen, immutable invoices for closed periods,
  * late-correction net adjustment targets and their tentative allocation,
  * account / period held flags and new-invoice blockers.

It never edits readings; the readings module never writes invoices.
"""
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timezone
from fractions import Fraction
from .common import sha256_hex
from .rating import rate
from .readings import verify_meter

# Ledger-side held reasons (reading reasons are passed through verbatim).
MISSING_ANCHOR='missing_anchor'
UNPAIRED_EXCHANGE='unpaired_exchange'
NONINTEGRAL='nonintegral_quantity'

def period_id(boundary_ts):
    dt=datetime.fromtimestamp(boundary_ts,timezone.utc)
    return '%04d-%02d'%(dt.year,dt.month)

def parse_period_id(pid,cfg):
    if type(pid) is int or (isinstance(pid,str) and pid.isdigit()):
        i=int(pid)
        if 0<=i<len(cfg['boundaries'])-1:return i
        raise ValueError('unknown period index: %r'%pid)
    try:dt=datetime.strptime(pid,'%Y-%m').replace(tzinfo=timezone.utc)
    except ValueError as error:raise ValueError('bad period id %r, expected YYYY-MM'%pid) from error
    ts=int(dt.timestamp())
    i=bisect_left(cfg['boundaries'],ts)
    if i>=len(cfg['boundaries'])-1 or cfg['boundaries'][i]!=ts:
        raise ValueError('period outside configuration: %r'%pid)
    return i

def _variable_charge(qty_int,tiers):
    bill=rate([{'id':'_','quantity':qty_int}],tiers,0)['bills'][0]
    return bill['variable_charge'],bill['lines']

def verify_all(state):
    """Run the reading chain over every meter from the committed events."""
    cfg=state['config']
    exchange_ts=defaultdict(set)
    for e in cfg['exchanges']:
        exchange_ts[e['out_meter']].add(e['at'])
        exchange_ts[e['in_meter']].add(e['at'])
    return {m:verify_meter(m,state['events'].get(m,[]),cfg,exchange_ts.get(m,set()))
            for m in cfg['meters']}

def recompute(state):
    """Rebuild all derived period/adjustment state from persistent facts.

    Deterministic: the same generation always yields the same derived view.
    Persistent facts are config, committed reading events, closed invoices and
    per-source closed net adjustments.  Everything else is rebuilt here.
    """
    cfg=state['config'];boundaries=cfg['boundaries'];n=len(boundaries)-1
    verif=verify_all(state)
    exch_at=defaultdict(list)
    for e in cfg['exchanges']:exch_at[e['at']].append(e)

    periods={}
    for a in cfg['accounts']:
        slots=[]
        for i in range(n):
            slots.append({'account':a,'index':i,'id':period_id(boundaries[i]),
                          'start':boundaries[i],'end':boundaries[i+1],
                          'quantity':Fraction(0),'reasons':set(),'tentative':[]})
        periods[a]=slots

    def hold(a,i,reason):
        if 0<=i<n:periods[a][i]['reasons'].add(reason)

    for meter,v in verif.items():
        a=v['account']
        touched_periods=set()
        for itv in v['intervals']:
            ri=bisect_left(boundaries,itv['right'])-1
            if 0<=ri<n:touched_periods.add(ri)
        # Every period touched by a meter chain needs an anchor of this meter
        # at both of its boundary instants.
        for i in touched_periods:
            if boundaries[i] not in v['anchors']:hold(a,i,MISSING_ANCHOR)
            if boundaries[i+1] not in v['anchors']:hold(a,i,MISSING_ANCHOR)
        for e in cfg['exchanges']:
            if meter not in (e['out_meter'],e['in_meter']):continue
            other=e['in_meter'] if meter==e['out_meter'] else e['out_meter']
            if e['at'] not in v['anchors'] or e['at'] not in verif[other]['anchors']:
                bi=bisect_left(boundaries,e['at'])
                hold(a,bi-1,UNPAIRED_EXCHANGE);hold(a,bi,UNPAIRED_EXCHANGE)
        for itv in v['intervals']:
            ri=bisect_left(boundaries,itv['right'])-1
            if itv['clean']:
                if 0<=ri<n and itv['left']>=boundaries[ri] and itv['right']<=boundaries[ri+1]:
                    periods[a][ri]['quantity']+=Fraction(itv['delta'])
            else:
                # Periods actually intersected by (left,right]. An interval whose
                # left end is itself a boundary starts inside period lo (not lo-1);
                # a crossing interval therefore holds every period it spans.
                lo=bisect_left(boundaries,itv['left'])
                first=lo if itv['left']==boundaries[lo] else max(0,lo-1)
                covered=range(first,min(n,ri+1))
                for i in covered:
                    for reason in itv['reasons']:hold(a,i,reason)

    tentative=[];unallocated=defaultdict(int);pending=set()
    closed=state['closed']

    def rateable(slot):
        q=slot['quantity']
        return not slot['reasons'] and q.denominator==1 and q>=0

    for a,slots in periods.items():
        for i,slot in enumerate(slots):
            q=slot['quantity']
            if q.denominator!=1:slot['reasons'].add(NONINTEGRAL)
            key='%s#%s'%(a,slot['id'])
            if key in closed:
                slot['state']='closed';slot['held']=bool(slot['reasons'])
                continue
            slot['state']='open';slot['held']=bool(slot['reasons'])

    # Closed-source correction targets, in deterministic source order.
    for key in sorted(closed):
        a,pid=key.split('#',1);i=parse_period_id(pid,cfg)
        slot=periods[a][i]
        src=state['sources'].get(key,{'closed_net':0})
        if not rateable(slot):
            pending.add(key);continue
        frozen=closed[key]
        var_now,_=_variable_charge(int(slot['quantity']),frozen['tariff']['tiers'])
        # Contract: corrected receivable rated on the frozen tariff (fixed
        # charge counted once, never re-charged), minus the source period's own
        # frozen invoice amount and the net adjustments already closed for it.
        # own_total excludes adjustments received by other periods, so closing a
        # receiver never double-counts its received amount here.
        target=var_now+frozen['tariff']['fixed_charge']-frozen['own_total']-src['closed_net']
        if target==0:continue
        receiver=next((s for s in periods[a] if s['state']=='open' and not s['held']),None)
        if receiver is None:
            unallocated[a]+=target
        else:
            entry={'source':key,'account':a,'period':receiver['id'],'amount':target}
            tentative.append(entry);receiver['tentative'].append(entry)

    for a,slots in periods.items():
        blocked_sources=sorted(k for k in pending if k.split('#',1)[0]==a)
        for slot in slots:
            slot['blocked_by']=blocked_sources
            slot['reasons']=sorted(slot['reasons'])
            slot['quantity_text']=str(slot['quantity'])
            del slot['quantity']

    account_held={}
    for a in cfg['accounts']:
        account_held[a]=bool(unallocated[a]) or any(k.split('#',1)[0]==a for k in pending) \
            or any(s['held'] for s in periods[a])

    state['derived']={'periods':periods,'verif':verif,'tentative':tentative,
                      'unallocated':dict(unallocated),'pending':sorted(pending),
                      'account_held':account_held}
    return state['derived']

def close_period(state,account,pid):
    cfg=state['config']
    if account not in cfg['accounts']:raise ValueError('unknown account: '+account)
    i=parse_period_id(pid,cfg)
    derived=state.get('derived') or recompute(state)
    slot=derived['periods'][account][i]
    if slot['state']!='open':raise ValueError('period %s for %s is already closed'%(pid,account))
    if slot['held']:raise ValueError('held period cannot be closed: %s'%pid)
    if slot['blocked_by']:raise ValueError('period blocked by pending source: %s'%slot['blocked_by'][0])
    if derived['unallocated'].get(account):
        raise ValueError('account has unallocated adjustment balance')
    qty=int(Fraction(slot['quantity_text']))
    variable,lines=_variable_charge(qty,cfg['tiers'])
    received=[{'source':e['source'],'amount':e['amount']} for e in slot['tentative']]
    invoice={
        'account':account,'period':slot['id'],'index':i,
        'currency':cfg['currency'],'quantity':qty,
        'tariff':{'currency':cfg['currency'],'fixed_charge':cfg['fixed_charge'],'tiers':cfg['tiers']},
        'lines':lines,'variable_charge':variable,'fixed_charge':cfg['fixed_charge'],
        'received_adjustments':received,
        'total':variable+cfg['fixed_charge']+sum(e['amount'] for e in received),
        'closed_generation':state['generation'],
    }
    invoice['invoice_sha256']=sha256_hex(invoice)
    key='%s#%s'%(account,slot['id'])
    state['closed'][key]={'invoice':invoice,'tariff':invoice['tariff'],
                          'own_total':variable+cfg['fixed_charge'],
                          'frozen_total':invoice['total']}
    srcs=state['sources']
    for e in received:
        srcs.setdefault(e['source'],{'closed_net':0})['closed_net']+=e['amount']
    state['generation']+=1
    recompute(state)
    return invoice

def status_view(state,account=None):
    derived=state.get('derived') or recompute(state)
    cfg=state['config']
    high=max((b['seq'] for b in state['batches']),default=0)
    view={'generation':state['generation'],'batch_high_water':high,
          'batch_count':len(state['batches']),'unallocated_adjustment':derived['unallocated'],
          'pending_sources':derived['pending'],'account_held':derived['account_held'],'accounts':{}}
    targets=cfg['accounts'] if account is None else [account]
    if account is not None and account not in cfg['accounts']:raise ValueError('unknown account: '+account)
    for a in targets:
        slots=[]
        for slot in derived['periods'][a]:
            key='%s#%s'%(a,slot['id'])
            item={'period':slot['id'],'index':slot['index'],'state':slot['state'],
                  'held':slot['held'],'reasons':slot['reasons'],
                  'quantity':slot['quantity_text'],
                  'tentative_adjustments':slot['tentative'],
                  'blocked_by':slot['blocked_by']}
            if key in state['closed']:
                inv=state['closed'][key]['invoice']
                item['frozen_invoice']={'total':inv['total'],'sha256':inv['invoice_sha256'],
                                        'variable_charge':inv['variable_charge'],
                                        'fixed_charge':inv['fixed_charge'],
                                        'received_adjustments':inv['received_adjustments']}
            slots.append(item)
        view['accounts'][a]={'held':derived['account_held'][a],
                             'unallocated_adjustment':derived['unallocated'].get(a,0),
                             'periods':slots}
    return view
