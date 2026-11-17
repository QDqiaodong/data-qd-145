"""Cash-reconcile tests: frozen-invoice extraction, funds lifecycle, exact
settlement, slice lineage, CLI contract."""
import json, os, random, subprocess, sys, tempfile, unittest
from pathlib import Path
from tariff_rating import ledger as L
from tariff_rating import cash
from tariff_rating.common import sha256_hex

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
ENV = {**os.environ, 'PYTHONPATH': str(SRC)}

DAY = 24*3600
ANCHORS = ['2025-01-01T00:00:00Z', '2025-02-01T00:00:00Z',
           '2025-03-01T00:00:00Z', '2025-04-01T00:00:00Z']
ANCHOR_TS = [1735689600, 1738368000, 1740787200, 1743465600]


def ts(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def rd(rid, meter, when, value, revision=1):
    return {'reading_id': rid, 'meter': meter, 'timestamp': when,
            'value': value, 'revision': revision}


def dense(values, meter='M1'):
    out = []
    for i, v in enumerate(values):
        out.append(rd('r%d' % i, meter, ANCHORS[i], v))
    for i in range(len(values)-1):
        out.append(rd('m%d' % i, meter, ts(ANCHOR_TS[i]+15*DAY),
                      (values[i]+values[i+1])//2))
    return out


def config(fixed=50, tiers=None):
    return {'currency': 'TEST', 'fixed_charge': fixed,
            'tiers': tiers or [{'up_to': 100, 'rate': 2},
                               {'up_to': 200, 'rate': 3}, {'up_to': None, 'rate': 5}],
            'accounts': [{'id': 'A'}], 'meters': [{'id': 'M1', 'account': 'A'}],
            'period_start': ANCHORS[0], 'period_end': ANCHORS[3],
            'max_interval_seconds': 28*DAY, 'max_rate_kwh_per_hour': '10',
            'exchanges': []}


def batch(bid, seq, readings, cfg=None):
    return {'batch_id': bid, 'seq': seq, 'config': cfg, 'readings': readings}


def build_adjustment_ledger(path):
    """Close-produced invoices carrying positive and negative adjustments.

    Jan: total 550 (own 550, no adjustments); Feb: total 650 (own 400, +250);
    Mar: total 490 (own 550, -310 and +250).
    """
    state = L.new_state()
    L.ingest(state, path, batch('b1', 1, dense([0, 200, 400, 600]), config()))
    jan = L.close(state, path, 'A', '2025-01')
    L.ingest(state, path, batch('b2', 2, [rd('r1', 'M1', ANCHORS[1], 250, revision=2)]))
    feb = L.close(state, path, 'A', '2025-02')
    L.ingest(state, path, batch('b3', 3, [rd('r1', 'M1', ANCHORS[1], 180, revision=3)]))
    mar = L.close(state, path, 'A', '2025-03')
    return state, {'2025-01': jan, '2025-02': feb, '2025-03': mar}


def fake_state(invoices):
    """Minimal read-only ledger snapshot with honestly hashed frozen invoices."""
    closed = {}
    for account, period, total in invoices:
        body = {'account': account, 'period': period, 'currency': 'TEST',
                'total': total}
        closed['%s#%s' % (account, period)] = {
            'invoice': dict(body, invoice_sha256=sha256_hex(body))}
    return {'version': 2, 'config': {'boundaries': ANCHOR_TS}, 'closed': closed}


def ref(iid, receivable, rank=0, account='A', period='2025-01'):
    return {'id': iid, 'account': account, 'period': period,
            'invoice_sha256': 'a'*64, 'due_rank': rank, 'receivable': receivable}


def spec_of(refs, events):
    return {'currency': 'TEST', 'refs': refs, 'events': events}


def resolve_fake(refs):
    resolved = {}
    for r in refs:
        resolved[r['id']] = dict(r)
    return resolved


class FrozenExtraction(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.path = Path(self.td.name) / 'ledger.json'
        self.state, self.inv = build_adjustment_ledger(self.path)

    def tearDown(self):
        self.td.cleanup()

    def extract(self, account, period, sha, currency='TEST'):
        return L.frozen_invoice(self.state, account, period, sha, currency)

    def test_receivable_is_frozen_total_not_own_total(self):
        # Feb carries +250, Mar carries -310/+250: invoice.total already nets
        # them; own_total (400 / 550) must never be used as the receivable.
        cases = {'2025-01': (550, []), '2025-02': (650, [250]),
                 '2025-03': (490, [-310, 250])}
        for period, (total, adjustments) in cases.items():
            inv = self.inv[period]
            self.assertEqual([e['amount'] for e in inv['received_adjustments']],
                             adjustments)
            got = self.extract('A', period, inv['invoice_sha256'])
            self.assertEqual(got['receivable'], total)
            self.assertEqual(got['period'], period)
            self.assertEqual(got['invoice_sha256'], inv['invoice_sha256'])
            closed = self.state['closed']['A#'+period]
            self.assertNotEqual(got['receivable'], closed['own_total']
                                if period != '2025-01' else -1)

    def test_held_source_period_still_payable_and_ledger_untouched(self):
        # Break the Feb-1 anchor: the closed Jan source turns held/pending.
        L.ingest(self.state, self.path,
                 batch('b4', 4, [rd('r1', 'M1', '2025-02-02T00:00:00Z', 180, revision=4)]))
        st = L.status(self.state)
        self.assertIn('A#2025-01', st['pending_sources'])
        before = self.path.read_bytes()
        got = self.extract('A', '2025-01', self.inv['2025-01']['invoice_sha256'])
        self.assertEqual(got['receivable'], 550)  # frozen credential still payable
        self.assertEqual(self.path.read_bytes(), before)  # read-only

    def test_snapshot_load_triggers_no_recompute(self):
        state, digest = L.load_snapshot(self.path)
        self.assertNotIn('derived', state)  # no reading-chain recompute
        import hashlib
        self.assertEqual(digest, hashlib.sha256(self.path.read_bytes()).hexdigest())
        got = L.frozen_invoice(state, 'A', '2025-02',
                               self.inv['2025-02']['invoice_sha256'], 'TEST')
        self.assertEqual(got['receivable'], 650)

    def test_tampered_selected_invoice_detected(self):
        key = 'A#2025-02'
        sha = self.inv['2025-02']['invoice_sha256']
        for mutate in (lambda inv: inv.update(total=999),
                       lambda inv: inv.update(invoice_sha256='0'*64),
                       lambda inv: inv.update(received_adjustments=[])):
            state, _ = L.load_snapshot(self.path)
            mutate(state['closed'][key]['invoice'])
            with self.assertRaises(ValueError):
                L.frozen_invoice(state, 'A', '2025-02', sha, 'TEST')

    def test_request_mismatch_rejected(self):
        sha = self.inv['2025-02']['invoice_sha256']
        with self.assertRaises(ValueError):
            self.extract('A', '2025-02', '0'*64)            # wrong sha
        with self.assertRaises(ValueError):
            self.extract('B', '2025-02', sha)               # wrong account
        with self.assertRaises(ValueError):
            self.extract('A', '2025-03', sha)               # wrong period
        with self.assertRaises(ValueError):
            self.extract('A', '2025-02', sha, currency='EUR')  # wrong currency
        with self.assertRaises(ValueError):
            self.extract('A', '2025-04', sha)               # outside config

    def test_period_index_form_accepted(self):
        sha = self.inv['2025-02']['invoice_sha256']
        got = self.extract('A', 1, sha)
        self.assertEqual(got['period'], '2025-02')


class Settlement(unittest.TestCase):
    def test_cross_eligibility_total_first(self):
        # r1 (first) can pay both invoices, r2 (later) only A.  A per-receipt
        # greedy would let r1 fill A and stall at total 10; the exact solver
        # reaches total 20 by routing r1 to B.
        total, dvec, matrix = cash.solve_settlement([10, 10], [10, 10],
                                                    [[0, 1], [0]])
        self.assertEqual(total, 20)
        self.assertEqual(dvec, [10, 10])
        self.assertEqual(matrix, {(0, 0): 0, (0, 1): 10, (1, 0): 10})

    def test_zero_amount_invoice_empty_eligible_and_huge_amounts(self):
        total, dvec, matrix = cash.solve_settlement([10], [0, 7], [[0, 1]])
        self.assertEqual((total, dvec), (7, [0, 7]))
        self.assertEqual(matrix[(0, 0)], 0)
        total, dvec, matrix = cash.solve_settlement([10], [5], [[]])
        self.assertEqual((total, dvec, matrix), (0, [0], {}))
        total, dvec, matrix = cash.solve_settlement([10**18], [10**18], [[0]])
        self.assertEqual((total, dvec), (10**18, [10**18]))
        self.assertEqual(matrix[(0, 0)], 10**18)
        # split a huge receipt across two invoices exactly
        total, dvec, matrix = cash.solve_settlement(
            [10**18], [10**18 - 3, 3], [[0, 1]])
        self.assertEqual((total, dvec), (10**18, [10**18 - 3, 3]))

    def test_level2_circulation_reroutes_to_higher_priority_invoice(self):
        # After the plain max flow, invoice C (priority 2) is short and D
        # (priority 3) is full; the level-2 circulation must reroute one unit
        # D -> C across receipts (r2 -> A -> r1 -> C) without touching the
        # already fixed A and B amounts.
        rcash = [5, 1, 2]
        idebt = [1, 4, 2, 2]
        elig = [[1], [0, 1, 2, 3], [0, 1, 3]]
        total, dvec, matrix = cash.solve_settlement(rcash, idebt, elig)
        self.assertEqual(total, 7)
        self.assertEqual(dvec, [1, 4, 1, 1])
        self.assertEqual(matrix, {(0, 1): 4, (1, 0): 0, (1, 1): 0, (1, 2): 1,
                                  (1, 3): 0, (2, 0): 1, (2, 1): 0, (2, 3): 1})
        self.assertEqual((total, tuple(dvec),
                          tuple(matrix.get((r, i), 0) for r in range(3)
                                for i in range(4))),
                         _brute_settlement(rcash, idebt, elig))

    def test_level3_circulation_concentrates_on_earliest_receipt(self):
        # The lex-max matrix pulls as much as possible onto r0's first edge,
        # rerouting one unit through the source vertex (r1 -> B, r2 -> A give
        # way to r0 -> A) while every invoice amount stays fixed.
        rcash = [3, 5, 3]
        idebt = [5, 4]
        elig = [[0, 1], [0, 1], [0]]
        total, dvec, matrix = cash.solve_settlement(rcash, idebt, elig)
        self.assertEqual(total, 9)
        self.assertEqual(dvec, [5, 4])
        self.assertEqual(matrix, {(0, 0): 3, (0, 1): 0, (1, 0): 1, (1, 1): 4,
                                  (2, 0): 1})
        self.assertEqual((total, tuple(dvec),
                          tuple(matrix.get((r, i), 0) for r in range(3)
                                for i in range(2))),
                         _brute_settlement(rcash, idebt, elig))

    def test_brute_force_crosscheck(self):
        rng = random.Random(2026)
        for _ in range(300):
            R = rng.randint(1, 4)
            I = rng.randint(1, 4)
            rcash = [rng.randint(0, 5) for _ in range(R)]
            idebt = [rng.randint(0, 5) for _ in range(I)]
            elig = [[i for i in range(I) if rng.random() < 0.6]
                    for _ in range(R)]
            if sum(len(s) for s in elig) > 9:
                continue
            got = cash.solve_settlement(rcash, idebt, elig)
            want = _brute_settlement(rcash, idebt, elig)
            flat = tuple(got[2].get((r, i), 0) for r in range(R) for i in range(I))
            self.assertEqual((got[0], tuple(got[1]), flat), want)


def _brute_settlement(rcash, idebt, elig):
    """Independent exhaustive oracle over every feasible allocation matrix."""
    R, I = len(rcash), len(idebt)
    edges = [(r, i) for r in range(R) for i in range(I) if i in elig[r]]
    best = (0, tuple([0]*I), tuple([0]*(R*I)))
    rowsum = [0]*R
    colsum = [0]*I
    x = {}

    def rec(k):
        nonlocal best
        if k == len(edges):
            key = (sum(colsum), tuple(colsum),
                   tuple(x.get((r, i), 0) for r in range(R) for i in range(I)))
            if key > best:
                best = key
            return
        r, i = edges[k]
        for v in range(min(rcash[r]-rowsum[r], idebt[i]-colsum[i]) + 1):
            x[(r, i)] = v
            rowsum[r] += v
            colsum[i] += v
            rec(k+1)
            rowsum[r] -= v
            colsum[i] -= v
        del x[(r, i)]
    rec(0)
    return best


class Lifecycle(unittest.TestCase):
    """One receipt settles across two rounds onto several invoices, then
    consecutive partial returns: burn unreconciled cash first, then revoke
    slices (newest settle, invoice id descending), partially if needed."""

    def run_reconcile(self, events):
        refs = [ref('i1', 10, 0), ref('i2', 20, 1), ref('i3', 30, 2)]
        return cash.reconcile(spec_of(refs, events), resolve_fake(refs))

    def test_two_settles_then_partial_returns(self):
        events = [
            {'id': 'r1', 'kind': 'receipt', 'amount': 25,
             'eligible': ['i1', 'i2', 'i3']},
            {'id': 's1', 'kind': 'settle'},
            {'id': 'r2', 'kind': 'receipt', 'amount': 40, 'eligible': ['i2', 'i3']},
            {'id': 's2', 'kind': 'settle'},
            {'id': 't1', 'kind': 'return', 'receipt_id': 'r1', 'amount': 12},
            {'id': 't2', 'kind': 'return', 'receipt_id': 'r1', 'amount': 5},
            {'id': 't3', 'kind': 'return', 'receipt_id': 'r2', 'amount': 8},
        ]
        res = self.run_reconcile(events)
        s1, s2 = res['settles']
        self.assertEqual(s1['objectives'], [25, 10, 15, 0])
        self.assertEqual(s1['matrix'], [{'receipt': 'r1', 'invoice': 'i1', 'amount': 10},
                                        {'receipt': 'r1', 'invoice': 'i2', 'amount': 15}])
        self.assertEqual(s2['objectives'], [35, 0, 5, 30])
        self.assertEqual(s2['matrix'], [{'receipt': 'r2', 'invoice': 'i2', 'amount': 5},
                                        {'receipt': 'r2', 'invoice': 'i3', 'amount': 30}])
        slices = {(s['receipt'], s['invoice']): s for s in res['slices']}
        # t1 burns no cash (r1 cash is 0) and revokes i2 (id desc) partially;
        # t2 finishes i2 then takes 2 from i1; t3 burns r2 cash 5 then i3 by 3.
        self.assertEqual(slices[('r1', 'i1')]['revocations'],
                         [{'event': 't2', 'amount': 2}])
        self.assertEqual(slices[('r1', 'i2')]['revocations'],
                         [{'event': 't1', 'amount': 12}, {'event': 't2', 'amount': 3}])
        self.assertEqual(slices[('r2', 'i3')]['revocations'],
                         [{'event': 't3', 'amount': 3}])
        self.assertEqual(slices[('r2', 'i2')]['effective'], 5)
        # per-event cash/debt snapshots
        want = [{'cash': {'r1': 25}, 'debt': {'i1': 10, 'i2': 20, 'i3': 30}},
                {'cash': {'r1': 0}, 'debt': {'i1': 0, 'i2': 5, 'i3': 30}},
                {'cash': {'r1': 0, 'r2': 40}, 'debt': {'i1': 0, 'i2': 5, 'i3': 30}},
                {'cash': {'r1': 0, 'r2': 5}, 'debt': {'i1': 0, 'i2': 0, 'i3': 0}},
                {'cash': {'r1': 0, 'r2': 5}, 'debt': {'i1': 0, 'i2': 12, 'i3': 0}},
                {'cash': {'r1': 0, 'r2': 5}, 'debt': {'i1': 2, 'i2': 15, 'i3': 0}},
                {'cash': {'r1': 0, 'r2': 0}, 'debt': {'i1': 2, 'i2': 15, 'i3': 3}}]
        for snap, w in zip(res['events'], want):
            self.assertEqual(snap['cash'], w['cash'])
            self.assertEqual(snap['debt'], w['debt'])
            # per-invoice conservation is observable in every snapshot
            for iid, d in snap['debt'].items():
                self.assertEqual(d + snap['paid'][iid],
                                 {'i1': 10, 'i2': 20, 'i3': 30}[iid])
        # final balances
        fr = {r['id']: r for r in res['final']['receipts']}
        self.assertEqual(fr['r1'], {'id': 'r1', 'principal': 25, 'returned': 17,
                                    'cash': 0, 'reconciled': 8})
        self.assertEqual(fr['r2'], {'id': 'r2', 'principal': 40, 'returned': 8,
                                    'cash': 0, 'reconciled': 32})
        fi = {i['id']: i for i in res['final']['invoices']}
        self.assertEqual(fi['i1'], {'id': 'i1', 'receivable': 10, 'paid': 8, 'debt': 2})
        self.assertEqual(fi['i2'], {'id': 'i2', 'receivable': 20, 'paid': 5, 'debt': 15})
        self.assertEqual(fi['i3'], {'id': 'i3', 'receivable': 30, 'paid': 27, 'debt': 3})
        # receipt conservation: principal == returned + cash + reconciled
        for r in fr.values():
            self.assertEqual(r['principal'], r['returned'] + r['cash'] + r['reconciled'])

    def test_return_burns_cash_before_touching_slices(self):
        events = [
            {'id': 'r1', 'kind': 'receipt', 'amount': 20, 'eligible': ['i1']},
            {'id': 't1', 'kind': 'return', 'receipt_id': 'r1', 'amount': 7},
            {'id': 's1', 'kind': 'settle'},
            {'id': 't2', 'kind': 'return', 'receipt_id': 'r1', 'amount': 13},
        ]
        res = self.run_reconcile(events)
        # t1 only burns cash (no slices yet): cash 20 -> 13
        self.assertEqual(res['events'][1]['cash'], {'r1': 13})
        s1 = res['settles'][0]
        self.assertEqual(s1['matrix'], [{'receipt': 'r1', 'invoice': 'i1', 'amount': 10}])
        # t2: cash 3 first, then 10 from the only slice
        sl = res['slices'][0]
        self.assertEqual(sl['revocations'], [{'event': 't2', 'amount': 10}])
        self.assertEqual(res['events'][3]['cash'], {'r1': 0})
        self.assertEqual(res['events'][3]['debt'], {'i1': 10, 'i2': 20, 'i3': 30})


class Linkage(unittest.TestCase):
    """The booked matrix drives return reopening; revocations feed the next
    solver round.  Same net principal, but a return before vs after the first
    settle yields different legal histories and debts."""

    def scenario(self, return_first):
        refs = [ref('A', 10, 0), ref('B', 10, 1)]
        if return_first:
            events = [
                {'id': 'r1', 'kind': 'receipt', 'amount': 20, 'eligible': ['A', 'B']},
                {'id': 't1', 'kind': 'return', 'receipt_id': 'r1', 'amount': 6},
                {'id': 'r2', 'kind': 'receipt', 'amount': 20, 'eligible': ['A']},
                {'id': 's1', 'kind': 'settle'},
            ]
        else:
            events = [
                {'id': 'r1', 'kind': 'receipt', 'amount': 20, 'eligible': ['A', 'B']},
                {'id': 's1', 'kind': 'settle'},
                {'id': 'r2', 'kind': 'receipt', 'amount': 20, 'eligible': ['A']},
                {'id': 't1', 'kind': 'return', 'receipt_id': 'r1', 'amount': 6},
                {'id': 's2', 'kind': 'settle'},
            ]
        return cash.reconcile(spec_of(refs, events), resolve_fake(refs))

    def test_return_after_first_settle(self):
        res = self.scenario(return_first=False)
        s1, s2 = res['settles']
        self.assertEqual(s1['matrix'], [{'receipt': 'r1', 'invoice': 'A', 'amount': 10},
                                        {'receipt': 'r1', 'invoice': 'B', 'amount': 10}])
        # the return revokes the booked B slice (invoice id descending), so B
        # reopens; r2 is eligible only for A and cannot repay B: s2 books nothing.
        self.assertEqual(s2['objectives'], [0, 0, 0])
        self.assertEqual(s2['matrix'], [])
        slices = {(s['receipt'], s['invoice']): s for s in res['slices']}
        self.assertEqual(slices[('r1', 'B')]['revocations'], [{'event': 't1', 'amount': 6}])
        self.assertEqual(slices[('r1', 'A')]['effective'], 10)
        fi = {i['id']: i for i in res['final']['invoices']}
        self.assertEqual(fi['B']['debt'], 6)     # B left owing
        self.assertEqual(fi['A']['debt'], 0)
        fr = {r['id']: r for r in res['final']['receipts']}
        self.assertEqual(fr['r2']['cash'], 20)   # r2 never usable
        self.assertEqual(fr['r2']['reconciled'], 0)
        # revoked amount did not become new cash for r1
        self.assertEqual(res['events'][3]['cash'], {'r1': 0, 'r2': 20})

    def test_return_before_first_settle(self):
        res = self.scenario(return_first=True)
        (s1,) = res['settles']
        # return burned cash first: r1 has 14; B needs r1's 10, so r1->A is
        # capped at 4 and r2 covers the rest of A.
        self.assertEqual(s1['objectives'], [20, 10, 10])
        self.assertEqual(s1['matrix'], [{'receipt': 'r1', 'invoice': 'A', 'amount': 4},
                                        {'receipt': 'r1', 'invoice': 'B', 'amount': 10},
                                        {'receipt': 'r2', 'invoice': 'A', 'amount': 6}])
        fi = {i['id']: i for i in res['final']['invoices']}
        self.assertEqual(fi['B']['debt'], 0)
        fr = {r['id']: r for r in res['final']['receipts']}
        self.assertEqual(fr['r2']['cash'], 14)
        self.assertEqual(fr['r2']['reconciled'], 6)

    def test_same_net_principal_different_history(self):
        after = self.scenario(return_first=False)
        before = self.scenario(return_first=True)
        for res in (after, before):
            fr = res['final']['receipts']
            net = sum(r['principal'] - r['returned'] for r in fr)
            self.assertEqual(net, 34)
        # but the legal reconciliation history and debts differ
        self.assertNotEqual([s['matrix'] for s in after['settles']],
                            [s['matrix'] for s in before['settles']])
        da = {i['id']: i['debt'] for i in after['final']['invoices']}
        db = {i['id']: i['debt'] for i in before['final']['invoices']}
        self.assertNotEqual(da, db)

    def test_paid_off_invoice_reopened_then_repaid(self):
        refs = [ref('A', 10, 0)]
        events = [
            {'id': 'r1', 'kind': 'receipt', 'amount': 10, 'eligible': ['A']},
            {'id': 's1', 'kind': 'settle'},
            {'id': 'r2', 'kind': 'receipt', 'amount': 10, 'eligible': ['A']},
            {'id': 't1', 'kind': 'return', 'receipt_id': 'r1', 'amount': 10},
            {'id': 's2', 'kind': 'settle'},
        ]
        res = cash.reconcile(spec_of(refs, events), resolve_fake(refs))
        self.assertEqual(res['events'][1]['debt'], {'A': 0})   # paid off
        self.assertEqual(res['events'][3]['debt'], {'A': 10})  # reopened
        self.assertEqual(res['events'][3]['cash'], {'r1': 0, 'r2': 10})
        s2 = res['settles'][1]
        self.assertEqual(s2['matrix'], [{'receipt': 'r2', 'invoice': 'A', 'amount': 10}])
        sl = res['slices'][0]
        self.assertEqual((sl['amount'], sl['revoked'], sl['effective']), (10, 10, 0))
        fi = res['final']['invoices'][0]
        self.assertEqual((fi['paid'], fi['debt']), (10, 0))


class PrefixProperty(unittest.TestCase):
    def test_event_prefix_unaffected_by_later_events(self):
        refs = [ref('i1', 10, 0), ref('i2', 20, 1), ref('i3', 30, 2)]
        events = [
            {'id': 'r1', 'kind': 'receipt', 'amount': 25,
             'eligible': ['i1', 'i2', 'i3']},
            {'id': 's1', 'kind': 'settle'},
            {'id': 'r2', 'kind': 'receipt', 'amount': 40, 'eligible': ['i2', 'i3']},
            {'id': 't1', 'kind': 'return', 'receipt_id': 'r1', 'amount': 4},
            {'id': 's2', 'kind': 'settle'},
            {'id': 't2', 'kind': 'return', 'receipt_id': 'r2', 'amount': 9},
        ]
        full = cash.reconcile(spec_of(refs, events), resolve_fake(refs))
        for k in range(1, len(events) + 1):
            part = cash.reconcile(spec_of(refs, events[:k]), resolve_fake(refs))
            self.assertEqual(part['events'], full['events'][:k])
            n_settles = sum(1 for e in events[:k] if e['kind'] == 'settle')
            self.assertEqual(part['settles'], full['settles'][:n_settles])
            snap = full['events'][k-1]
            fin = {r['id']: r for r in part['final']['receipts']}
            for rid, r in fin.items():
                self.assertEqual(r['cash'], snap['cash'][rid])
                self.assertEqual(r['returned'], snap['returned'][rid])
            for i in part['final']['invoices']:
                self.assertEqual(i['debt'], snap['debt'][i['id']])
                self.assertEqual(i['paid'], snap['paid'][i['id']])


class Validation(unittest.TestCase):
    def spec(self, **kw):
        base = {'currency': 'TEST', 'invoice_refs': [], 'events': []}
        base.update(kw)
        return base

    def bad(self, data):
        with self.assertRaises((ValueError, TypeError)):
            cash.validate_input(data)

    def test_top_level_fields_exact(self):
        cash.validate_input(self.spec())  # empty refs and events are legal
        self.bad(self.spec(extra=1))
        d = self.spec()
        del d['events']
        self.bad(d)
        d = self.spec()
        del d['currency']
        self.bad(d)

    def test_ref_validation(self):
        r = {'id': 'i1', 'account': 'A', 'period': '2025-01',
             'invoice_sha256': 'a'*64, 'due_rank': 0}
        self.bad(self.spec(invoice_refs=[dict(r, unknown=1)]))
        self.bad(self.spec(invoice_refs=[r, r]))  # duplicate id
        self.bad(self.spec(invoice_refs=[dict(r, due_rank=True)]))  # boolean
        self.bad(self.spec(invoice_refs=[dict(r, due_rank=10**9 + 1)]))
        self.bad(self.spec(invoice_refs=[dict(r, due_rank=-1)]))
        self.bad(self.spec(invoice_refs=[dict(r, invoice_sha256='a'*63)]))
        self.bad(self.spec(invoice_refs=[dict(r, invoice_sha256='A'*64)]))
        self.bad(self.spec(invoice_refs=[dict(r, period=True)]))
        self.bad(self.spec(invoice_refs=[dict(r, period='')]))
        self.bad(self.spec(invoice_refs=[dict(r, id='i%d' % k) for k in range(61)]))

    def test_event_validation(self):
        r = {'id': 'i1', 'account': 'A', 'period': '2025-01',
             'invoice_sha256': 'a'*64, 'due_rank': 0}
        receipt = {'id': 'r1', 'kind': 'receipt', 'amount': 5, 'eligible': ['i1']}
        ok = self.spec(invoice_refs=[r], events=[receipt])
        cash.validate_input(ok)
        self.bad(self.spec(invoice_refs=[r], events=[dict(receipt, extra=1)]))
        self.bad(self.spec(invoice_refs=[r], events=[dict(receipt, amount=0)]))
        self.bad(self.spec(invoice_refs=[r], events=[dict(receipt, amount=10**18 + 1)]))
        self.bad(self.spec(invoice_refs=[r], events=[dict(receipt, amount=True)]))
        self.bad(self.spec(invoice_refs=[r], events=[dict(receipt, eligible=['i1', 'i1'])]))
        self.bad(self.spec(invoice_refs=[r], events=[dict(receipt, eligible=['ghost'])]))
        self.bad(self.spec(invoice_refs=[r], events=[receipt, receipt]))  # dup id
        self.bad(self.spec(invoice_refs=[r],
                           events=[dict(receipt, id='i1')]))  # collides with ref id
        self.bad(self.spec(invoice_refs=[r], events=[{'id': 'x', 'kind': 'weird'}]))
        # returns
        ret = {'id': 't1', 'kind': 'return', 'receipt_id': 'r1', 'amount': 1}
        self.bad(self.spec(invoice_refs=[r], events=[ret]))  # no prior receipt
        fwd = [ret, receipt]
        self.bad(self.spec(invoice_refs=[r], events=fwd))    # forward reference
        self.bad(self.spec(invoice_refs=[r],
                           events=[receipt, dict(ret, receipt_id='t1')]))
        self.bad(self.spec(invoice_refs=[r],
                           events=[receipt, dict(ret, amount=0)]))
        # settle limits
        settles = [{'id': 's%d' % k, 'kind': 'settle'} for k in range(11)]
        self.bad(self.spec(invoice_refs=[r], events=settles))
        # receipt count limit
        receipts = [{'id': 'r%d' % k, 'kind': 'receipt', 'amount': 1, 'eligible': []}
                    for k in range(61)]
        self.bad(self.spec(invoice_refs=[r], events=receipts))
        # eligible total limit
        many = [{'id': 'r%d' % k, 'kind': 'receipt', 'amount': 1,
                 'eligible': ['i1']*10} for k in range(61)]
        self.bad(self.spec(invoice_refs=[r], events=many))
        # events count limit
        lots = [{'id': 'r%d' % k, 'kind': 'receipt', 'amount': 1, 'eligible': []}
                for k in range(60)] + \
               [{'id': 't%d' % k, 'kind': 'return', 'receipt_id': 'r0', 'amount': 1}
                for k in range(60)] + \
               [{'id': 's%d' % k, 'kind': 'settle'} for k in range(10)]
        self.assertEqual(len(lots), 130)
        cash.validate_input(self.spec(invoice_refs=[r], events=lots[:60]))
        events_201 = [{'id': 'e%d' % k, 'kind': 'receipt', 'amount': 1, 'eligible': []}
                      for k in range(60)]
        events_201 += [{'id': 't%d' % k, 'kind': 'return', 'receipt_id': 'e0',
                        'amount': 1} for k in range(141)]
        self.bad(self.spec(invoice_refs=[r], events=events_201))

    def test_return_exceeds_outstanding_principal_rejected(self):
        refs = [ref('i1', 10, 0)]
        events = [
            {'id': 'r1', 'kind': 'receipt', 'amount': 10, 'eligible': ['i1']},
            {'id': 't1', 'kind': 'return', 'receipt_id': 'r1', 'amount': 6},
            {'id': 't2', 'kind': 'return', 'receipt_id': 'r1', 'amount': 5},
        ]
        with self.assertRaises(ValueError):
            cash.reconcile(spec_of(refs, events), resolve_fake(refs))

    def test_resolve_duplicate_invoice_and_total_cap(self):
        state = fake_state([('A', '2025-01', 100), ('A', '2025-02', 50)])
        sha1 = state['closed']['A#2025-01']['invoice']['invoice_sha256']
        sha2 = state['closed']['A#2025-02']['invoice']['invoice_sha256']
        r1 = {'id': 'x', 'account': 'A', 'period': '2025-01',
              'invoice_sha256': sha1, 'due_rank': 0}
        r2 = {'id': 'y', 'account': 'A', 'period': '2025-01',
              'invoice_sha256': sha1, 'due_rank': 1}
        with self.assertRaises(ValueError):  # same invoice under a second id
            cash.resolve(state, {'currency': 'TEST', 'refs': [r1, r2]})
        ok = cash.resolve(state, {'currency': 'TEST', 'refs': [
            r1, dict(r2, period='2025-02', invoice_sha256=sha2)]})
        self.assertEqual(ok['x']['receivable'], 100)
        self.assertEqual(ok['y']['receivable'], 50)
        # total cap 10^24
        big = fake_state([('A', '2025-01', 10**24), ('A', '2025-02', 1)])
        b1 = big['closed']['A#2025-01']['invoice']['invoice_sha256']
        b2 = big['closed']['A#2025-02']['invoice']['invoice_sha256']
        with self.assertRaises(ValueError):
            cash.resolve(big, {'currency': 'TEST', 'refs': [
                dict(r1, invoice_sha256=b1),
                dict(r2, period='2025-02', invoice_sha256=b2)]})
        # negative receivable rejected
        neg = fake_state([('A', '2025-01', -5)])
        nsha = neg['closed']['A#2025-01']['invoice']['invoice_sha256']
        with self.assertRaises(ValueError):
            cash.resolve(neg, {'currency': 'TEST',
                               'refs': [dict(r1, invoice_sha256=nsha)]})


class Cli(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        td = Path(self.td.name)
        self.ledger = td / 'ledger.json'
        _, self.inv = build_adjustment_ledger(self.ledger)
        self.source = td / 'cash.json'
        self.output = td / 'report.json'
        self.input = {
            'currency': 'TEST',
            'invoice_refs': [
                {'id': 'inv-feb', 'account': 'A', 'period': '2025-02',
                 'invoice_sha256': self.inv['2025-02']['invoice_sha256'],
                 'due_rank': 0},
                {'id': 'inv-mar', 'account': 'A', 'period': '2025-03',
                 'invoice_sha256': self.inv['2025-03']['invoice_sha256'],
                 'due_rank': 1},
                {'id': 'inv-jan', 'account': 'A', 'period': '2025-01',
                 'invoice_sha256': self.inv['2025-01']['invoice_sha256'],
                 'due_rank': 2},
            ],
            'events': [
                {'id': 'rcpt-1', 'kind': 'receipt', 'amount': 700,
                 'eligible': ['inv-feb', 'inv-mar']},
                {'id': 'settle-1', 'kind': 'settle'},
                {'id': 'rcpt-2', 'kind': 'receipt', 'amount': 500,
                 'eligible': ['inv-mar', 'inv-jan']},
                {'id': 'ret-1', 'kind': 'return', 'receipt_id': 'rcpt-1',
                 'amount': 100},
                {'id': 'settle-2', 'kind': 'settle'},
            ],
        }
        self.source.write_text(json.dumps(self.input))

    def tearDown(self):
        self.td.cleanup()

    def call(self, *args):
        return subprocess.run([sys.executable, '-m', 'tariff_rating.cli',
                               *map(str, args)],
                              capture_output=True, text=True, env=ENV, cwd=ROOT)

    def run_ok(self, source=None, output=None):
        r = self.call('cash-reconcile', '--ledger', self.ledger,
                      '--source', source or self.source,
                      '--output', output or self.output)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_report_generated_and_ledger_untouched(self):
        before = self.ledger.read_bytes()
        import hashlib
        r = self.run_ok()
        report = json.loads(self.output.read_text())
        self.assertEqual(report['schema_version'], 5)
        self.assertEqual(report['ledger_sha256'],
                         hashlib.sha256(before).hexdigest())
        self.assertEqual(self.ledger.read_bytes(), before)  # ledger read-only
        self.assertEqual(json.loads(r.stdout), report)
        res = report['result']
        self.assertEqual([i['receivable'] for i in res['invoices']],
                         [650, 490, 550])
        s1, s2 = res['settles']
        self.assertEqual(s1['objectives'], [700, 650, 50, 0])
        self.assertEqual(s2['objectives'], [500, 0, 490, 10])
        # stdout equals file bytes
        self.assertEqual(json.loads(r.stdout), json.loads(self.output.read_text()))

    def test_shuffled_refs_and_eligible_same_report(self):
        reports = []
        for seed in range(3):
            rng = random.Random(seed)
            data = json.loads(json.dumps(self.input))
            rng.shuffle(data['invoice_refs'])
            for ev in data['events']:
                if ev['kind'] == 'receipt':
                    rng.shuffle(ev['eligible'])
            src = Path(self.td.name) / ('in%d.json' % seed)
            out = Path(self.td.name) / ('out%d.json' % seed)
            src.write_text(json.dumps(data))
            self.run_ok(source=src, output=out)
            reports.append(out.read_bytes())
        self.assertEqual(len(set(reports)), 1)

    def test_failure_exit_two_preserves_old_report(self):
        self.output.write_text('PRIOR')
        bad = Path(self.td.name) / 'bad.json'
        bodies = ['{}', '{"a":1,"a":2}', 'NaN']
        for body in bodies:
            bad.write_text(body)
            r = self.call('cash-reconcile', '--ledger', self.ledger,
                          '--source', bad, '--output', self.output)
            self.assertEqual(r.returncode, 2)
            self.assertEqual(self.output.read_text(), 'PRIOR')
        # semantic failures
        for mutate in (
                lambda d: d.update(unknown=1),
                lambda d: d['invoice_refs'][0].update(due_rank=True),
                lambda d: d['invoice_refs'][0].update(invoice_sha256='0'*64),
                lambda d: d['events'][3].update(amount=10**9),
                lambda d: d['events'].append({'id': 'x', 'kind': 'weird'}),
                lambda d: d.update(currency='EUR')):
            data = json.loads(json.dumps(self.input))
            mutate(data)
            bad.write_text(json.dumps(data))
            r = self.call('cash-reconcile', '--ledger', self.ledger,
                          '--source', bad, '--output', self.output)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertEqual(self.output.read_text(), 'PRIOR')

    def test_tampered_ledger_invoice_rejected(self):
        self.run_ok()
        first = self.output.read_bytes()
        state = json.loads(self.ledger.read_text())
        state['closed']['A#2025-02']['invoice']['total'] = 999
        self.ledger.write_text(json.dumps(state))
        r = self.call('cash-reconcile', '--ledger', self.ledger,
                      '--source', self.source, '--output', self.output)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.output.read_bytes(), first)

    def test_output_must_not_overwrite_source_or_ledger(self):
        r = self.call('cash-reconcile', '--ledger', self.ledger,
                      '--source', self.source, '--output', self.source)
        self.assertEqual(r.returncode, 2)
        r = self.call('cash-reconcile', '--ledger', self.ledger,
                      '--source', self.source, '--output', self.ledger)
        self.assertEqual(r.returncode, 2)
        link = Path(self.td.name) / 'link.json'
        link.symlink_to(self.source)
        r = self.call('cash-reconcile', '--ledger', self.ledger,
                      '--source', self.source, '--output', link)
        self.assertEqual(r.returncode, 2)

    def test_ledger_symlink_and_missing_rejected(self):
        link = Path(self.td.name) / 'ledger-link.json'
        link.symlink_to(self.ledger)
        r = self.call('cash-reconcile', '--ledger', link,
                      '--source', self.source, '--output', self.output)
        self.assertEqual(r.returncode, 2)
        r = self.call('cash-reconcile', '--ledger', Path(self.td.name) / 'none.json',
                      '--source', self.source, '--output', self.output)
        self.assertEqual(r.returncode, 2)

    def test_size_limits(self):
        big_source = Path(self.td.name) / 'big-source.json'
        big_source.write_bytes(b' ' * (8_000_001))
        r = self.call('cash-reconcile', '--ledger', self.ledger,
                      '--source', big_source, '--output', self.output)
        self.assertEqual(r.returncode, 2)
        big_ledger = Path(self.td.name) / 'big-ledger.json'
        big_ledger.write_bytes(b' ' * (64_000_001))
        r = self.call('cash-reconcile', '--ledger', big_ledger,
                      '--source', self.source, '--output', self.output)
        self.assertEqual(r.returncode, 2)


class FixtureCli(unittest.TestCase):
    """The committed fixture ledger and input must stay in sync and produce a
    report through the real CLI."""

    def call(self, *args):
        return subprocess.run([sys.executable, '-m', 'tariff_rating.cli',
                               *map(str, args)],
                              capture_output=True, text=True, env=ENV, cwd=ROOT)

    def test_fixture_report(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'report.json'
            r = self.call('cash-reconcile', '--ledger', ROOT/'fixtures/cash_ledger.json',
                          '--source', ROOT/'fixtures/cash.json', '--output', out)
            self.assertEqual(r.returncode, 0, r.stderr)
            report = json.loads(out.read_text())
            res = report['result']
            self.assertEqual(report['schema_version'], 5)
            self.assertEqual([i['id'] for i in res['invoices']],
                             ['inv-feb', 'inv-mar', 'inv-jan'])
            self.assertEqual([i['receivable'] for i in res['invoices']],
                             [650, 490, 550])
            s1, s2 = res['settles']
            self.assertEqual(s1['objectives'], [700, 650, 50, 0])
            self.assertEqual(s2['objectives'], [500, 0, 490, 10])
            # the return revoked inv-mar first (invoice id descending), then inv-feb
            revoked = {sl['invoice']: sl['revoked'] for sl in res['slices']
                       if sl['receipt'] == 'rcpt-1'}
            self.assertEqual(revoked, {'inv-feb': 50, 'inv-mar': 50})
            fi = {i['id']: i for i in res['final']['invoices']}
            self.assertEqual(fi['inv-feb']['debt'], 50)
            self.assertEqual(fi['inv-mar']['debt'], 0)
            self.assertEqual(fi['inv-jan']['debt'], 540)


class Performance(unittest.TestCase):
    def test_upper_limits_complete(self):
        rng = random.Random(11)
        refs = []
        resolved = {}
        for i in range(60):
            iid = 'inv%02d' % i
            refs.append({'id': iid, 'account': 'A', 'period': '2025-01',
                         'invoice_sha256': 'a'*64, 'due_rank': 0})
            resolved[iid] = ref(iid, rng.randrange(1, 10**16))
        ids = [r['id'] for r in refs]
        events = []
        for k in range(60):
            events.append({'id': 'r%02d' % k, 'kind': 'receipt',
                           'amount': rng.randrange(1, 10**18),
                           'eligible': rng.sample(ids, 10)})
        returned = {e['id']: 0 for e in events}
        principals = {e['id']: e['amount'] for e in events}
        for s in range(10):
            events.append({'id': 'settle%d' % s, 'kind': 'settle'})
            for t in range(13):
                rid = 'r%02d' % ((s*13+t) % 60)
                amount = min(rng.randrange(1, 10**12),
                             principals[rid] - returned[rid])
                if amount < 1:
                    continue
                returned[rid] += amount
                events.append({'id': 'ret%d-%d' % (s, t), 'kind': 'return',
                               'receipt_id': rid, 'amount': amount})
        self.assertLessEqual(len(events), 200)
        import time
        start = time.time()
        res = cash.reconcile(spec_of(refs, events), resolved)
        self.assertLess(time.time() - start, 60)
        for r in res['final']['receipts']:
            self.assertEqual(r['principal'],
                             r['returned'] + r['cash'] + r['reconciled'])
        for i in res['final']['invoices']:
            self.assertEqual(i['receivable'], i['paid'] + i['debt'])
        self.assertEqual(sum(1 for e in events if e['kind'] == 'settle'), 10)
        self.assertEqual(len(res['events']), len(events))


if __name__ == '__main__':
    unittest.main()
