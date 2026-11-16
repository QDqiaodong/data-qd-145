"""v2 ledger configuration validation.

Configuration names the accounts, meter ownership, the continuous UTC monthly
period boundaries, per-meter reading-interval / rate limits and the tariff.
"""
from calendar import timegm
from datetime import datetime, timezone
from fractions import Fraction
from .common import require, ident, number, rows, parse_ts, rational

MAX_ACCOUNTS=20000
MAX_METERS=100000
MAX_EXCHANGES=100000
MAX_TIERS=100
MAX_PERIODS_PER_ACCOUNT=120
MAX_READINGS_PER_METER=200000

class BatchRejected(ValueError):
    """Structural / identity failure: the whole batch is refused, ledger unchanged."""

def _month_bounds(first,last):
    """Return the list of monthly boundary epoch seconds (first .. last)."""
    out=[];y,m=first
    while (y,m)<=last:
        out.append(timegm(datetime(y,m,1,tzinfo=timezone.utc).timetuple()))
        m+=1
        if m==13:m=1;y+=1
    return out

def _is_month_start(ts):
    dt=datetime.fromtimestamp(ts,timezone.utc)
    return dt.day==1 and dt.hour==0 and dt.minute==0 and dt.second==0

def validate_config(cfg):
    require(cfg,'currency fixed_charge tiers accounts meters period_start period_end max_interval_seconds max_rate_kwh_per_hour exchanges')
    currency=ident(cfg['currency'])
    fixed=number(cfg['fixed_charge'],0,10**9)
    tiers=[];previous=0
    for t in rows(cfg['tiers'],MAX_TIERS):
        require(t,'up_to rate');upper=t['up_to'];price=number(t['rate'],0,10**9)
        if previous is None:raise BatchRejected('unbounded tier must be last')
        if upper is not None:
            number(upper,1,10**12)
            if upper<=previous:raise BatchRejected('tier boundaries must increase')
        tiers.append({'up_to':upper,'rate':price});previous=upper
    if not tiers or tiers[-1]['up_to'] is not None:raise BatchRejected('last tier must be unbounded')

    accounts=[];seen=set()
    for a in rows(cfg['accounts'],MAX_ACCOUNTS):
        require(a,'id');aid=ident(a['id'])
        if aid in seen:raise BatchRejected('duplicate account id')
        seen.add(aid);accounts.append(aid)

    meters=[];mseen=set();meter_owner={}
    for m in rows(cfg['meters'],MAX_METERS):
        require(m,'id account');mid=ident(m['id']);owner=ident(m['account'])
        if mid in mseen:raise BatchRejected('duplicate meter id')
        if owner not in seen:raise BatchRejected('meter owned by unknown account: '+owner)
        mseen.add(mid);meters.append(mid);meter_owner[mid]=owner

    start=parse_ts(cfg['period_start']);end=parse_ts(cfg['period_end'])
    if not _is_month_start(start) or not _is_month_start(end) or end<=start:
        raise BatchRejected('period_start/period_end must be distinct UTC month boundaries')
    d0=datetime.fromtimestamp(start,timezone.utc);d1=datetime.fromtimestamp(end,timezone.utc)
    first=(d0.year,d0.month);last=(d1.year,d1.month)
    boundaries=_month_bounds(first,last)
    period_count=len(boundaries)-1
    if period_count>MAX_PERIODS_PER_ACCOUNT:
        raise BatchRejected('configuration spans more than %d periods'%MAX_PERIODS_PER_ACCOUNT)
    shortest=min(boundaries[i+1]-boundaries[i] for i in range(period_count))

    interval=cfg['max_interval_seconds']
    if type(interval) is not int or interval<=0 or interval>shortest:
        raise BatchRejected('max_interval_seconds must be a positive integer not exceeding the shortest period')
    rate=rational(cfg['max_rate_kwh_per_hour'])
    if Fraction(rate)==0:raise BatchRejected('max_rate_kwh_per_hour must be positive')

    exchanges=[];eset=set()
    for e in rows(cfg['exchanges'],MAX_EXCHANGES):
        require(e,'at out_meter in_meter')
        at=parse_ts(e['at']);om=ident(e['out_meter']);nm=ident(e['in_meter'])
        if om==nm:raise BatchRejected('exchange meters must differ')
        if om not in mseen or nm not in mseen:raise BatchRejected('exchange references unknown meter')
        if meter_owner[om]!=meter_owner[nm]:raise BatchRejected('exchange meters must share one account')
        if at not in boundaries:raise BatchRejected('exchange time must be a period boundary')
        key=(at,om,nm)
        if key in eset:raise BatchRejected('duplicate exchange declaration')
        eset.add(key);exchanges.append({'at':at,'out_meter':om,'in_meter':nm})
    return {
        'currency':currency,'fixed_charge':fixed,'tiers':tiers,'accounts':accounts,
        'meters':meters,'meter_owner':meter_owner,
        'period_start':start,'period_end':end,'boundaries':boundaries,
        'max_interval_seconds':interval,'max_rate_kwh_per_hour':rate,
        'exchanges':exchanges,
    }
