"""Reading-chain responsibility: revisions, interval verification, impact closure.

This module never creates or mutates invoices.  It consumes the revisioned
reading store and produces, per meter, an ordered anchor set and a list of
verified intervals with anomaly flags.  ``impact_closure`` reports the meters
(and therefore accounts) whose verification result changed when a batch of
reading revisions is applied; the ledger module owns everything downstream.
"""
from bisect import bisect_left
from fractions import Fraction
from .common import parse_ts, rational
from .config import MAX_READINGS_PER_METER, BatchRejected

# Interval anomaly reasons (an interval may carry several).
GAP='gap'                       # adjacent readings further apart than max interval
DECREASE='decrease'             # cumulative reading fell (non-exchange)
OVERSPEED='overspeed'           # delta_kwh*3600 > max_rate * elapsed_seconds
NONPOSITIVE_ELAPSED='nonpositive_elapsed'
CROSSING='crossing'             # interval spans at least one non-adjacent boundary

REASON_ORDER=(GAP,DECREASE,OVERSPEED,NONPOSITIVE_ELAPSED,CROSSING)

def validate_reading(raw,cfg):
    """Identity / structural validation of one reading record."""
    from .common import require, ident, number
    require(raw,'reading_id meter timestamp value revision')
    rid=ident(raw['reading_id']);meter=ident(raw['meter'])
    if meter not in cfg['meter_owner']:raise BatchRejected('reading on unknown meter: '+meter)
    ts=parse_ts(raw['timestamp'])
    if not cfg['period_start']<=ts<=cfg['period_end']:
        raise BatchRejected('reading timestamp outside configured period range')
    value=rational(raw['value'])
    rev=number(raw['revision'],1,10**9)
    return {'reading_id':rid,'meter':meter,'timestamp':ts,'value':value,'revision':rev}

def build_meter_state(events):
    """Collapse revision events for one meter into current effective readings.

    ``events`` is the list of accepted revision records (newest commit last).
    The highest revision per reading_id wins; stale events have no effect.
    Returns reading_id -> effective record.
    """
    current={}
    for e in events:
        rid=e['reading_id']
        if rid not in current or e['revision']>current[rid]['revision']:
            current[rid]=e
    return current

def verify_meter(meter,events,cfg,exchange_at):
    """Verify one meter.

    ``exchange_at`` is the set of boundary timestamps where this meter takes
    part in a declared exchange (either side).  Returns a dict with anchors,
    ordered effective readings and verified intervals.
    """
    current=build_meter_state(events)
    if len(current)>MAX_READINGS_PER_METER:  # defense in depth; ingest enforces first
        raise BatchRejected('meter %s exceeds %d effective readings'%(meter,MAX_READINGS_PER_METER))
    ordered=sorted(current.values(),key=lambda r:(r['timestamp'],r['reading_id']))
    boundaries=cfg['boundaries']
    max_gap=cfg['max_interval_seconds'];max_rate=Fraction(cfg['max_rate_kwh_per_hour'])
    anchors={r['timestamp'] for r in ordered if r['timestamp'] in boundaries}
    span=(ordered[0]['timestamp'],ordered[-1]['timestamp']) if ordered else None
    intervals=[]
    for prev,nxt in zip(ordered,ordered[1:]):
        elapsed=nxt['timestamp']-prev['timestamp']
        delta=Fraction(nxt['value'])-Fraction(prev['value'])
        reasons=set()
        if elapsed<=0:
            reasons.add(NONPOSITIVE_ELAPSED)
        else:
            if elapsed>max_gap:reasons.add(GAP)
            if delta<0 and nxt['timestamp'] not in exchange_at:reasons.add(DECREASE)
            if delta*3600>max_rate*elapsed:reasons.add(OVERSPEED)
        lo_idx=bisect_left(boundaries,prev['timestamp'])
        hi_idx=bisect_left(boundaries,nxt['timestamp'])
        crossing=hi_idx-lo_idx>1
        if crossing:reasons.add(CROSSING)
        intervals.append({
            'left':prev['timestamp'],'right':nxt['timestamp'],
            'left_reading':prev['reading_id'],'right_reading':nxt['reading_id'],
            'delta':str(delta),'elapsed':elapsed,
            'reasons':sorted(reasons,key=REASON_ORDER.index),
            'clean':not reasons,
        })
    return {'meter':meter,'account':cfg['meter_owner'][meter],
            'ordered':ordered,'anchors':sorted(anchors),'span':span,'intervals':intervals}

def impact_closure(meter_verifications,touched_meters,cfg):
    """Correction-impact closure for a batch.

    A revision can only change intervals adjacent to its reading, and anomaly /
    unverified state propagates along the meter until a boundary or a restored
    state.  The closure therefore contains every touched meter; at the ledger
    level that expands to every account owning one, and all of that account's
    periods are recomputed (bounded by the 200k/meter and 120-period caps).
    """
    meters=set(touched_meters)
    accounts=set()
    for m in meters:
        if m in meter_verifications:accounts.add(meter_verifications[m]['account'])
        else:accounts.add(cfg['meter_owner'][m])
    return {'meters':sorted(meters),'accounts':sorted(accounts)}
