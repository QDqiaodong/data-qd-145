"""Cash-reconcile responsibility: bank receipts, settlement rounds, return flows.

This module turns one complete snapshot of bank cash events into a traceable
cash reconciliation report against frozen ledger invoices.  It never writes
the meter ledger, never recomputes readings, billing or late corrections, and
never creates a persistent ledger of its own: every run replays the events
from zero against the read-only frozen-invoice credentials.

Two coupled responsibilities live here:

  * Bank funds lifecycle.  A receipt adds that receipt's unreconciled cash; a
    settle books the round's reconciliation slices (later rounds never move
    booked slices that are still valid); a return first burns the same
    receipt's unreconciled cash, then revokes that receipt's valid slices in
    reverse settle order (newest settle first, invoice id descending inside
    one settle), partially if needed, reopening the corresponding invoice
    debt.  After every event, per receipt ``principal == returned +
    unreconciled cash + valid slices`` and per invoice ``frozen receivable ==
    valid slices + remaining debt``.

  * Restricted reconciliation.  Each settle allocates non-negative integer
    amounts on the eligible edges, using only this moment's cash and debts:
    (1) maximize the round total, (2) lexicographically maximize the
    per-invoice amounts in invoice priority order ``(due_rank, id)``, (3)
    lexicographically maximize the allocation matrix flattened receipt-first
    (receipts in event order) then invoice priority, forbidden edges zero.
    The solver is exact (one max flow, then per-edge circulation fixing),
    never a per-receipt greedy, never an enumeration of amounts or matrices,
    and never depends on set iteration order.

Both directions share one slice lineage: the booked matrix decides which
invoices a later return reopens, and revocation results feed the next
round's cash and debt capacities.  All arithmetic is exact integer
arithmetic in the smallest currency unit; no floats, no fixed-width
overflowing weights, no per-currency-unit expansion.
"""
from .common import require, ident, number, rows, read, sha256_hex
from . import ledger

MAX_REFS = 60
MAX_EVENTS = 200
MAX_RECEIPTS = 60
MAX_SETTLES = 10
MAX_ELIGIBLE = 600
MAX_AMOUNT = 10**18
MAX_DUE_RANK = 10**9
MAX_RECEIVABLE_SUM = 10**24

_INF = 10**30  # middle-edge capacity sentinel: above any reachable flow, exact


# --------------------------------------------------------------------------
# input validation
# --------------------------------------------------------------------------

def _period(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and value:
        return value
    raise ValueError('period must be a YYYY-MM string or a period index')


def _sha(value):
    if not isinstance(value, str) or len(value) != 64 or \
            any(c not in '0123456789abcdef' for c in value):
        raise ValueError('expected a 64-character lowercase hex sha256')
    return value


def validate_input(data):
    """Structural validation of one cash-reconcile input.  Raises ValueError."""
    require(data, 'currency invoice_refs events')
    currency = ident(data['currency'])

    refs = []
    seen_refs = set()
    for raw in rows(data['invoice_refs'], MAX_REFS):
        require(raw, 'id account period invoice_sha256 due_rank')
        rid = ident(raw['id'])
        if rid in seen_refs:
            raise ValueError('duplicate invoice ref id: ' + rid)
        seen_refs.add(rid)
        refs.append({'id': rid, 'account': ident(raw['account']),
                     'period': _period(raw['period']),
                     'invoice_sha256': _sha(raw['invoice_sha256']),
                     'due_rank': number(raw['due_rank'], 0, MAX_DUE_RANK)})

    events = []
    event_ids = set()
    receipt_ids = set()
    n_receipts = 0
    n_settles = 0
    eligible_total = 0
    for raw in rows(data['events'], MAX_EVENTS):
        if not isinstance(raw, dict):
            raise ValueError('event must be a JSON object')
        kind = raw.get('kind')
        if kind == 'receipt':
            require(raw, 'id kind amount eligible')
        elif kind == 'return':
            require(raw, 'id kind receipt_id amount')
        elif kind == 'settle':
            require(raw, 'id kind')
        else:
            raise ValueError('unknown event kind: %r' % (kind,))
        eid = ident(raw['id'])
        if eid in event_ids:
            raise ValueError('duplicate event id: ' + eid)
        if eid in seen_refs:
            raise ValueError('event id collides with an invoice ref id: ' + eid)
        event_ids.add(eid)
        if kind == 'receipt':
            amount = number(raw['amount'], 1, MAX_AMOUNT)
            eligible = raw['eligible']
            if not isinstance(eligible, list):
                raise ValueError('eligible must be an array')
            eligible_total += len(eligible)
            if eligible_total > MAX_ELIGIBLE:
                raise ValueError('eligible references exceed %d in total' % MAX_ELIGIBLE)
            seen = set()
            targets = []
            for item in eligible:
                iid = ident(item)
                if iid not in seen_refs:
                    raise ValueError('eligible references undefined invoice id: ' + iid)
                if iid in seen:
                    raise ValueError('duplicate invoice id in eligible: ' + iid)
                seen.add(iid)
                targets.append(iid)
            n_receipts += 1
            if n_receipts > MAX_RECEIPTS:
                raise ValueError('more than %d receipt events' % MAX_RECEIPTS)
            receipt_ids.add(eid)
            events.append({'id': eid, 'kind': 'receipt', 'amount': amount,
                           'eligible': targets})
        elif kind == 'return':
            receipt_id = ident(raw['receipt_id'])
            if receipt_id not in receipt_ids:
                raise ValueError('return references no prior receipt: ' + receipt_id)
            amount = number(raw['amount'], 1, MAX_AMOUNT)
            events.append({'id': eid, 'kind': 'return', 'receipt_id': receipt_id,
                           'amount': amount})
        else:
            n_settles += 1
            if n_settles > MAX_SETTLES:
                raise ValueError('more than %d settle events' % MAX_SETTLES)
            events.append({'id': eid, 'kind': 'settle'})
    return {'currency': currency, 'refs': refs, 'events': events}


def resolve(state, spec):
    """Resolve every invoice ref against the read-only ledger snapshot.

    Each ref must map to one distinct closed invoice; the frozen receivable
    is the invoice total.  Returns ref id -> ref with canonical period and
    receivable.
    """
    currency = spec['currency']
    resolved = {}
    keys = set()
    total = 0
    for ref in spec['refs']:
        info = ledger.frozen_invoice(state, ref['account'], ref['period'],
                                     ref['invoice_sha256'], currency)
        key = (info['account'], info['period'])
        if key in keys:
            raise ValueError('invoice referenced more than once: %s#%s' % key)
        keys.add(key)
        receivable = info['receivable']
        if type(receivable) is not int or receivable < 0:
            raise ValueError('frozen receivable must be a non-negative integer')
        total += receivable
        if total > MAX_RECEIVABLE_SUM:
            raise ValueError('selected invoice totals exceed 10^24')
        resolved[ref['id']] = {'id': ref['id'], 'account': info['account'],
                               'period': info['period'],
                               'invoice_sha256': info['invoice_sha256'],
                               'due_rank': ref['due_rank'], 'receivable': receivable}
    return resolved


def normalized(spec, resolved):
    """Canonical semantic input: refs and eligible normalized, events kept in order.

    Permuting ``invoice_refs`` or any ``eligible`` array yields the identical
    canonical JSON and therefore the identical semantic input SHA.
    """
    refs = sorted(resolved.values(), key=lambda r: (r['due_rank'], r['id']))
    priority = {r['id']: i for i, r in enumerate(refs)}
    events = []
    for ev in spec['events']:
        if ev['kind'] == 'receipt':
            events.append({'id': ev['id'], 'kind': 'receipt', 'amount': ev['amount'],
                           'eligible': sorted(ev['eligible'], key=priority.__getitem__)})
        elif ev['kind'] == 'return':
            events.append({'id': ev['id'], 'kind': 'return',
                           'receipt_id': ev['receipt_id'], 'amount': ev['amount']})
        else:
            events.append({'id': ev['id'], 'kind': 'settle'})
    return {'currency': spec['currency'],
            'invoice_refs': [{'id': r['id'], 'account': r['account'],
                              'period': r['period'],
                              'invoice_sha256': r['invoice_sha256'],
                              'due_rank': r['due_rank']} for r in refs],
            'events': events}


# --------------------------------------------------------------------------
# exact lexicographic settlement solver
# --------------------------------------------------------------------------

class _Edge:
    __slots__ = ('to', 'cap', 'rev', 'on')

    def __init__(self, to, cap):
        self.to = to
        self.cap = cap
        self.rev = None
        self.on = True


def _dfs(g, u, t, it, level, f):
    if u == t:
        return f
    gu = g[u]
    while it[u] < len(gu):
        e = gu[it[u]]
        if e.on and e.cap > 0 and level[e.to] == level[u] + 1:
            d = _dfs(g, e.to, t, it, level, f if f < e.cap else e.cap)
            if d:
                e.cap -= d
                e.rev.cap += d
                return d
        it[u] += 1
    return 0


class _Graph:
    """Residual graph with disableable arcs; one Dinic max flow per query."""

    def __init__(self, n):
        self.g = [[] for _ in range(n)]

    def add(self, fr, to, cap):
        fwd = _Edge(to, cap)
        bwd = _Edge(fr, 0)
        fwd.rev = bwd
        bwd.rev = fwd
        self.g[fr].append(fwd)
        self.g[to].append(bwd)
        return fwd

    def maxflow(self, s, t, limit=None):
        g = self.g
        n = len(g)
        flow = 0
        while limit is None or flow < limit:
            level = [-1] * n
            level[s] = 0
            q = [s]
            head = 0
            while head < len(q):
                u = q[head]
                head += 1
                nxt = level[u] + 1
                for e in g[u]:
                    if e.on and e.cap > 0 and level[e.to] < 0:
                        level[e.to] = nxt
                        q.append(e.to)
            if level[t] < 0:
                break
            it = [0] * n
            while limit is None or flow < limit:
                pushed = _dfs(g, s, t, it, level,
                              _INF if limit is None else limit - flow)
                if not pushed:
                    break
                flow += pushed
        return flow


def solve_settlement(rcash, idebt, eligible):
    """Exact three-level optimum for one settle round.

    ``rcash`` is the unreconciled cash of each receipt in event order,
    ``idebt`` the remaining debt of each invoice in priority order, and
    ``eligible[i]`` the invoice indices receipt ``i`` may pay.  Returns
    ``(total, invoice_vector, matrix)`` with ``matrix`` mapping
    ``(receipt_index, invoice_index)`` to its amount.

    Level 1 maximizes the total; level 2 fixes the per-invoice amounts
    lexicographically in priority order; level 3 fixes the edge amounts
    lexicographically in (receipt, invoice) order.  Each fix is the exact
    maximum residual circulation through that edge with every previously
    fixed edge removed from the residual graph -- greedy component-wise
    maximization, which is the definition of the lexicographic maximum.
    """
    R = len(rcash)
    I = len(idebt)
    if not R or not I:
        return 0, [0] * I, {}
    g = _Graph(2 + R + I)
    for ri, amount in enumerate(rcash):
        g.add(0, 2 + ri, amount)
    mid = {}
    for ri in range(R):
        for ii in eligible[ri]:
            mid[(ri, ii)] = g.add(2 + ri, 2 + R + ii, _INF)
    snk = [g.add(2 + R + ii, 1, idebt[ii]) for ii in range(I)]

    total = g.maxflow(0, 1)

    # Level 2: per-invoice amounts, fixed one by one in priority order.  The
    # return path of the circulation may cross the source (receipt totals are
    # free) and the not-yet-fixed sink edges, but never a fixed sink edge or
    # the edge being maximized itself.
    dvec = [0] * I
    for ii in range(I):
        e = snk[ii]
        f = e.rev.cap
        e.on = False
        e.rev.on = False
        room = idebt[ii] - f
        if room > 0:
            inc = g.maxflow(1, 2 + R + ii, limit=room)
            if inc:
                e.cap -= inc
                e.rev.cap += inc
                f += inc
        dvec[ii] = f
    assert sum(dvec) == total

    # Level 3: edge amounts in (receipt, invoice) order.  All sink edges are
    # fixed (disabled) by now, so circulations keep every invoice amount.
    matrix = {}
    for ri in range(R):
        for ii in range(I):
            e = mid.get((ri, ii))
            if e is None:
                continue
            e.on = False
            e.rev.on = False
            inc = g.maxflow(2 + R + ii, 2 + ri)
            if inc:
                e.cap -= inc
                e.rev.cap += inc
            matrix[(ri, ii)] = e.rev.cap
    for ii in range(I):
        assert sum(matrix.get((ri, ii), 0) for ri in range(R)) == dvec[ii]
    return total, dvec, matrix


# --------------------------------------------------------------------------
# event replay: funds lifecycle coupled with the settlement solver
# --------------------------------------------------------------------------

def _settle(ev, receipts, invoices, priority, settles, all_slices):
    rids = [rid for rid, rc in receipts.items() if rc['cash'] > 0]
    iids = [iid for iid in priority if invoices[iid]['debt'] > 0]
    ipos = {iid: i for i, iid in enumerate(iids)}
    rcash = [receipts[rid]['cash'] for rid in rids]
    idebt = [invoices[iid]['debt'] for iid in iids]
    eligible = [sorted(ipos[x] for x in receipts[rid]['eligible'] if x in ipos)
                for rid in rids]
    total, dvec, matrix = solve_settlement(rcash, idebt, eligible)

    full = {iid: 0 for iid in priority}
    for i, iid in enumerate(iids):
        full[iid] = dvec[i]
    entries = []
    seq = len(settles)
    for ri, rid in enumerate(rids):
        spent = 0
        for ii, iid in enumerate(iids):
            amount = matrix.get((ri, ii), 0)
            if not amount:
                continue
            spent += amount
            sl = {'settle': ev['id'], 'settle_seq': seq, 'receipt': rid,
                  'invoice': iid, 'amount': amount, 'revoked': 0, 'revocations': []}
            receipts[rid]['slices'].append(sl)
            all_slices.append(sl)
            inv = invoices[iid]
            inv['paid'] += amount
            inv['debt'] -= amount
            entries.append({'receipt': rid, 'invoice': iid, 'amount': amount})
        receipts[rid]['cash'] -= spent
    settles.append({'id': ev['id'],
                    'objectives': [total] + [full[iid] for iid in priority],
                    'matrix': entries})


def _return(ev, receipts, invoices):
    rc = receipts[ev['receipt_id']]
    amount = ev['amount']
    if amount > rc['principal'] - rc['returned']:
        raise ValueError('return exceeds the outstanding principal of receipt '
                         + ev['receipt_id'])
    rc['returned'] += amount
    take = rc['cash'] if rc['cash'] < amount else amount
    rc['cash'] -= take
    rem = amount - take
    if rem:
        # Reverse settle order; inside one settle, invoice id descending.
        ordered = sorted(rc['slices'],
                         key=lambda s: (s['settle_seq'], s['invoice']), reverse=True)
        for sl in ordered:
            effective = sl['amount'] - sl['revoked']
            if effective <= 0:
                continue
            rev = effective if effective < rem else rem
            sl['revoked'] += rev
            sl['revocations'].append({'event': ev['id'], 'amount': rev})
            inv = invoices[sl['invoice']]
            inv['paid'] -= rev
            inv['debt'] += rev
            rem -= rev
            if not rem:
                break
    if rem:
        raise ValueError('internal error: return could not be fully applied')


def _conservation_check(receipts, invoices):
    """Both conservations must hold after every single event."""
    paid = {iid: 0 for iid in invoices}
    for rid, rc in receipts.items():
        effective = 0
        for sl in rc['slices']:
            effective += sl['amount'] - sl['revoked']
            paid[sl['invoice']] += sl['amount'] - sl['revoked']
        if rc['principal'] != rc['returned'] + rc['cash'] + effective:
            raise ValueError('internal error: receipt conservation broken for ' + rid)
    for iid, inv in invoices.items():
        if inv['paid'] != paid[iid] or inv['receivable'] != inv['paid'] + inv['debt']:
            raise ValueError('internal error: invoice conservation broken for ' + iid)


def _snapshot(ev, receipts, invoices):
    return {'id': ev['id'], 'kind': ev['kind'],
            'cash': {rid: rc['cash'] for rid, rc in receipts.items()},
            'returned': {rid: rc['returned'] for rid, rc in receipts.items()},
            'paid': {iid: inv['paid'] for iid, inv in invoices.items()},
            'debt': {iid: inv['debt'] for iid, inv in invoices.items()}}


def reconcile(spec, resolved):
    """Replay the whole event snapshot from zero into a report result."""
    refs = sorted(resolved.values(), key=lambda r: (r['due_rank'], r['id']))
    priority = [r['id'] for r in refs]
    invoices = {r['id']: {'receivable': r['receivable'], 'paid': 0,
                          'debt': r['receivable']} for r in refs}
    receipts = {}
    settles = []
    all_slices = []
    snapshots = []
    for ev in spec['events']:
        kind = ev['kind']
        if kind == 'receipt':
            receipts[ev['id']] = {'principal': ev['amount'], 'returned': 0,
                                  'cash': ev['amount'], 'eligible': ev['eligible'],
                                  'slices': []}
        elif kind == 'settle':
            _settle(ev, receipts, invoices, priority, settles, all_slices)
        else:
            _return(ev, receipts, invoices)
        _conservation_check(receipts, invoices)
        snapshots.append(_snapshot(ev, receipts, invoices))

    result = {'business_status': 'ok', 'currency': spec['currency'],
              'invoices': [{'id': r['id'], 'account': r['account'],
                            'period': r['period'],
                            'invoice_sha256': r['invoice_sha256'],
                            'due_rank': r['due_rank'],
                            'receivable': r['receivable']} for r in refs],
              'events': snapshots,
              'settles': settles,
              'slices': [{'receipt': sl['receipt'], 'settle': sl['settle'],
                          'invoice': sl['invoice'], 'amount': sl['amount'],
                          'revoked': sl['revoked'],
                          'effective': sl['amount'] - sl['revoked'],
                          'revocations': sl['revocations']} for sl in all_slices],
              'final': {
                  'receipts': [{'id': rid, 'principal': rc['principal'],
                                'returned': rc['returned'], 'cash': rc['cash'],
                                'reconciled': sum(s['amount'] - s['revoked']
                                                  for s in rc['slices'])}
                               for rid, rc in receipts.items()],
                  'invoices': [{'id': iid, 'receivable': invoices[iid]['receivable'],
                                'paid': invoices[iid]['paid'],
                                'debt': invoices[iid]['debt']} for iid in priority]}}
    return result


def run(ledger_path, source_path):
    """Full cash-reconcile report for one ledger snapshot and one event input."""
    state, ledger_sha = ledger.load_snapshot(ledger_path)
    data, _ = read(source_path)
    spec = validate_input(data)
    resolved = resolve(state, spec)
    result = reconcile(spec, resolved)
    return {'schema_version': 5, 'ledger_sha256': ledger_sha,
            'source_sha256': sha256_hex(normalized(spec, resolved)),
            'result': result}
