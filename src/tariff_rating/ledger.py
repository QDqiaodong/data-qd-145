"""Persistent ledger: batch identity, revisions, commit generations, atomicity.

The ledger is one JSON file replaced atomically per commit.  Every successful
ingest or close advances the generation; a failed commit leaves the previous
file untouched, so replaying a batch can only observe the old generation or a
complete new one -- never a partial write.
"""
import copy, json, os
from pathlib import Path
from .common import canonical, sha256_hex, read, save, ident, number
from .config import validate_config, BatchRejected, MAX_READINGS_PER_METER
from .readings import validate_reading, impact_closure
from .periods import recompute, close_period, status_view, parse_period_id

LEDGER_VERSION=2
FAULT_ENV='TARIFF_LEDGER_FAULT'

def new_state():
    return {'version':LEDGER_VERSION,'generation':0,'config':None,'batches':[],
            'events':{},'closed':{},'sources':{}}

def load(path):
    p=Path(path)
    if not p.exists():raise ValueError('ledger does not exist yet; ingest the first batch (seq 1 with config)')
    if p.is_symlink():raise ValueError('ledger path cannot be a symlink')
    raw=p.read_bytes()
    def unique(pairs):
        result={}
        for k,v in pairs:
            if k in result:raise ValueError('duplicate JSON key in ledger: '+k)
            result[k]=v
        return result
    state=json.loads(raw,object_pairs_hook=unique)
    if state.get('version')!=LEDGER_VERSION:raise ValueError('unsupported ledger version')
    recompute(state)
    return state

def _commit(state,path):
    if os.environ.get(FAULT_ENV):  # simulated crash before publication
        raise OSError('injected commit failure; ledger publication skipped')
    state.pop('derived',None)  # derived views are rebuilt from persistent facts
    save(path,state)
    recompute(state)

def _rollback(state,snapshot):
    state.clear();state.update(snapshot)
    recompute(state)  # failed commit: only the old generation is observable

def _normalize_batch(data):
    if not isinstance(data,dict):raise BatchRejected('batch must be a JSON object')
    allowed={'batch_id','seq','config','readings'}
    if set(data)!=allowed:raise BatchRejected('expected batch fields: batch_id seq config readings')
    if data['seq'] is None:raise BatchRejected('batch seq is required')
    bid=ident(data['batch_id']);seq=number(data['seq'],1,10**12)
    readings=data['readings']
    if not isinstance(readings,list):raise BatchRejected('readings must be an array')
    return bid,seq,data['config'],readings

def ingest(state,path,data):
    """Validate and commit one batch. Raises BatchRejected on any identity failure."""
    bid,seq,cfg_raw,raw_readings=_normalize_batch(data)
    has_config=cfg_raw is not None

    # ---- batch identity conflicts are structural and refused outright ----
    replay_candidate=next((b for b in state['batches']
                           if b['batch_id']==bid and b['seq']==seq),None)
    for b in state['batches']:
        if b is replay_candidate:continue
        if b['batch_id']==bid or b['seq']==seq:
            raise BatchRejected('batch id/seq reuse with different identity (id=%s seq=%d)'%(bid,seq))
    # Persistent batch numbering is 1..N with no gaps; replays keep their old seq.
    high_water=max((b['seq'] for b in state['batches']),default=0)
    if replay_candidate is None and seq!=high_water+1:
        raise BatchRejected('batch seq must be exactly %d (continuous numbering from 1), got %d'
                            %(high_water+1,seq))

    if has_config:
        if replay_candidate is None and (seq!=1 or state['config'] is not None):
            raise BatchRejected('config is accepted only in the first batch (seq 1)')
        cfg=validate_config(cfg_raw) if replay_candidate is None else state['config']
    else:
        if state['config'] is None:raise BatchRejected('first batch (seq 1) must carry config')
        cfg=state['config']

    # ---- reading identity validation (whole batch is all-or-nothing) ----
    norm=[validate_reading(r,cfg) for r in raw_readings]
    seen_in_batch=set()
    for r in norm:
        key=(r['meter'],r['reading_id'])
        if key in seen_in_batch:
            raise BatchRejected('duplicate reading_id within batch: '+r['reading_id'])
        seen_in_batch.add(key)

    index={}  # (meter, rid) -> list of accepted revision events, newest last
    for meter,events in state['events'].items():
        for e in events:index.setdefault((meter,e['reading_id']),[]).append(e)

    stale=[];accepted=[]
    counts={meter:len({e['reading_id'] for e in evs}) for meter,evs in state['events'].items()}
    for r in norm:
        key=(r['meter'],r['reading_id']);history=index.get(key)
        counts.setdefault(r['meter'],0)
        if history is None:
            if r['revision']!=1:raise BatchRejected('new reading_id must start at revision 1: '+r['reading_id'])
            if counts[r['meter']]>=MAX_READINGS_PER_METER:
                raise BatchRejected('meter %s already at %d effective readings'%(r['meter'],MAX_READINGS_PER_METER))
            counts[r['meter']]+=1
        else:
            top=max(history,key=lambda e:e['revision'])
            if r['revision']<top['revision']:
                stale.append({'meter':r['meter'],'reading_id':r['reading_id'],
                              'revision':r['revision'],'known_revision':top['revision']})
                continue
            if r['revision']==top['revision']:
                if canonical(r)==canonical(top):continue  # idempotent resend
                raise BatchRejected('reading %s revision %d resubmitted with different content'
                                    %(r['reading_id'],r['revision']))
        accepted.append(r)

    digest=sha256_hex({'batch_id':bid,'seq':seq,
                       'config':cfg if has_config else None,'readings':norm})

    # ---- exact replay: same id AND seq AND sha returns the stored result ----
    for b in state['batches']:
        if b['batch_id']==bid and b['seq']==seq:
            if b['sha256']!=digest:
                raise BatchRejected('batch id/seq reused with different content sha256 (id=%s seq=%d)'%(bid,seq))
            return dict(b['result'],replayed=True)
    for b in state['batches']:  # sha collision guard across different identities
        if b['sha256']==digest:
            raise BatchRejected('canonical content sha256 reused under different batch identity')

    # ---- commit: mutate working state, recompute, publish atomically ----
    snapshot=copy.deepcopy(state)
    try:
        if has_config:state['config']=cfg
        touched=set()
        for r in accepted:
            state['events'].setdefault(r['meter'],[]).append(r);touched.add(r['meter'])
        state['generation']+=1
        derived=recompute(state)
        result={'batch_id':bid,'seq':seq,'sha256':digest,'generation':state['generation'],
                'accepted':len(accepted),'stale':stale,
                'stale_count':len(stale),'impact':impact_closure(derived['verif'],touched,cfg),
                'held_periods':_held_periods(derived)}
        state['batches'].append({'batch_id':bid,'seq':seq,'sha256':digest,
                                 'generation':state['generation'],'result':result})
        _commit(state,path)
    except BaseException:
        _rollback(state,snapshot)
        raise
    return dict(result,replayed=False)

def _held_periods(derived):
    out=[]
    for a,slots in derived['periods'].items():
        for s in slots:
            if s['held']:out.append({'account':a,'period':s['id'],'reasons':s['reasons']})
    return out

def close(state,path,account,period):
    if state['config'] is None:raise BatchRejected('ledger has no configuration yet')
    pid=period
    parse_period_id(pid,state['config'])  # validates
    snapshot=copy.deepcopy(state)
    try:
        invoice=close_period(state,account,pid)
        _commit(state,path)
    except BaseException:
        _rollback(state,snapshot)
        raise
    return invoice

def status(state,account=None):
    return status_view(state,account)
