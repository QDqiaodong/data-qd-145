"""Aid-plan tests: relief curves, district-guarantee allocation, CLI contract."""
import itertools, json, os, random, subprocess, sys, tempfile, unittest
from pathlib import Path
from tariff_rating import aid
from tariff_rating.rating import rate

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
ENV = {**os.environ, 'PYTHONPATH': str(SRC)}


def bill(tiers, fixed, q):
    return rate([{'id': '_', 'quantity': q}], tiers, fixed)['bills'][0]['total']


def fixture():
    return json.loads((ROOT / 'fixtures/aid.json').read_text())


def snapshot(**kw):
    base = {'currency': 'T', 'fixed_charge': 0,
            'tiers': [{'up_to': None, 'rate': 1}],
            'credit_budget': 0, 'districts': [], 'households': []}
    base.update(kw)
    return base


class Curve(unittest.TestCase):
    def assert_curve_matches_rate(self, tiers, fixed, q, cap):
        segs = aid.curve_segments(tiers, q, cap)
        self.assertEqual(segs[0]['lo'], 0)
        self.assertEqual(segs[-1]['hi'], cap)
        for left, right in zip(segs, segs[1:]):
            self.assertEqual(left['hi'] + 1, right['lo'])   # no gap, no overlap
            self.assertNotEqual(left['a'], right['a'])      # equal slopes merged
        for c in range(cap + 1):
            want = bill(tiers, fixed, q) - bill(tiers, fixed, q - c)
            seg = next(s for s in segs if s['lo'] <= c <= s['hi'])
            self.assertEqual(seg['a'] * c + seg['b'], want, 'c=%d' % c)
        return segs

    def test_decreasing_rates(self):
        tiers = [{'up_to': 5, 'rate': 9}, {'up_to': 10, 'rate': 4},
                 {'up_to': None, 'rate': 1}]
        self.assert_curve_matches_rate(tiers, 3, 12, 12)

    def test_zero_rate_plateau(self):
        tiers = [{'up_to': 10, 'rate': 5}, {'up_to': 20, 'rate': 0},
                 {'up_to': None, 'rate': 3}]
        segs = self.assert_curve_matches_rate(tiers, 7, 25, 25)
        flat = [s for s in segs if s['a'] == 0]
        self.assertTrue(flat)  # the plateau appears as a zero-slope segment
        # relief never exceeds the variable charge: fixed charge is not relieved
        for c in range(26):
            self.assertEqual(aid.min_need(segs, c + 1) is not None,
                             bill(tiers, 0, 25) - bill(tiers, 0, 25 - c) >= 0)

    def test_q_zero_single_point(self):
        tiers = [{'up_to': 10, 'rate': 5}, {'up_to': None, 'rate': 3}]
        segs = aid.curve_segments(tiers, 0, 0)
        self.assertEqual(segs, [{'lo': 0, 'hi': 0, 'a': 0, 'b': 0}])
        self.assertIsNone(aid.min_need(segs, 1))  # nothing to relieve

    def test_single_unit_tier_gives_singleton_segment(self):
        tiers = [{'up_to': 10, 'rate': 2}, {'up_to': 11, 'rate': 7},
                 {'up_to': None, 'rate': 4}]
        segs = self.assert_curve_matches_rate(tiers, 0, 12, 12)
        self.assertIn({'lo': 1, 'hi': 1, 'a': 7, 'b': -3}, segs)

    def test_small_domain_full_enumeration(self):
        rng = random.Random(2026)
        for _ in range(60):
            n = rng.randint(1, 4)
            tiers, bound = [], 0
            for _ in range(n - 1):
                bound += rng.randint(1, 6)
                tiers.append({'up_to': bound, 'rate': rng.choice([0, 1, 2, 3, 5])})
            tiers.append({'up_to': None, 'rate': rng.choice([0, 1, 2, 3, 5])})
            fixed = rng.randint(0, 8)
            q = rng.randint(0, 30)
            cap = min(q, rng.randint(0, 30))
            segs = self.assert_curve_matches_rate(tiers, fixed, q, cap)
            relief = [bill(tiers, fixed, q) - bill(tiers, fixed, q - c)
                      for c in range(cap + 1)]
            for target in range(1, max(relief) + 3):
                got = aid.min_need(segs, target)
                want = next((c for c in range(cap + 1) if relief[c] >= target), None)
                if want is None:
                    self.assertIsNone(got)
                else:
                    self.assertEqual(got, (want, relief[want]))

    def test_huge_quantity_not_enumerated(self):
        tiers = [{'up_to': 10**6, 'rate': 5}, {'up_to': 10**9, 'rate': 0},
                 {'up_to': 10**11, 'rate': 9}, {'up_to': None, 'rate': 2}]
        q = 10**12
        segs = aid.curve_segments(tiers, q, q)
        self.assertLessEqual(len(segs), 4)  # segments only at tier boundaries
        for s in segs:
            for c in (s['lo'], s['hi']):
                self.assertEqual(s['a'] * c + s['b'],
                                 bill(tiers, 11, q) - bill(tiers, 11, q - c))
        self.assertIsNone(aid.min_need(segs, 10**24))  # beyond reachable relief


class Allocation(unittest.TestCase):
    def plan(self, households, districts, budget, tiers=None, fixed=0):
        snap = snapshot(
            households=households, districts=districts, credit_budget=budget,
            tiers=tiers or [{'up_to': None, 'rate': 1}], fixed_charge=fixed)
        return aid.analyze(snap)

    def hh(self, hid, district, quantity, cap, target):
        return {'id': hid, 'district': district, 'quantity': quantity,
                'credit_cap': cap, 'target_relief': target}

    def test_greedy_by_ratio_fails(self):
        # tiers make A the best relief-per-voucher item but block the pair.
        # A: q=6 on rate-10 band -> need 6, relief 60 (ratio 10).
        # B,C: q=11 -> first 5 credited units sit on the rate-9 band (need 5,
        # relief 45, ratio 9).  Budget 10: taking A leaves room for nothing,
        # while B+C fits and yields more selected households and relief.
        tiers = [{'up_to': 6, 'rate': 10}, {'up_to': None, 'rate': 9}]
        households = [self.hh('A', 'D', 6, 6, 60),
                      self.hh('B', 'D', 11, 11, 45),
                      self.hh('C', 'D', 11, 11, 45)]
        r = self.plan(households, [{'id': 'D', 'min_served': 0}], 10, tiers)
        self.assertEqual(r['business_status'], 'feasible')
        self.assertEqual(r['objectives'], [2, 90, 10])
        self.assertEqual(r['selected_ids'], ['B', 'C'])

    def test_district_minimum_crowds_out_budget(self):
        # rate 1: need == relief == target.
        households = [self.hh('H1', 'D1', 5, 5, 5), self.hh('H2', 'D1', 5, 5, 5),
                      self.hh('H3', 'D2', 1, 1, 1)]
        districts = [{'id': 'D1', 'min_served': 2}, {'id': 'D2', 'min_served': 0}]
        r = self.plan(households, districts, 10)
        self.assertEqual(r['selected_ids'], ['H1', 'H2'])  # guarantee wins over relief
        served = {d['id']: d['served'] for d in r['districts']}
        self.assertEqual(served, {'D1': 2, 'D2': 0})
        # one voucher fewer makes the guarantee itself infeasible
        r2 = self.plan(households, districts, 9)
        self.assertEqual(r2['business_status'], 'infeasible')
        self.assertEqual(r2['min_vouchers_for_guarantees'], 10)
        self.assertEqual(r2['selected_ids'], [])
        self.assertIsNone(r2['objectives'])

    def test_same_relief_fewer_vouchers_wins(self):
        # H1 reaches relief 6 in 3 vouchers (rate-2 band); H2 needs 8 vouchers
        # for the same relief 6 because it must cross a zero-rate plateau.
        tiers = [{'up_to': 3, 'rate': 2}, {'up_to': 8, 'rate': 0},
                 {'up_to': None, 'rate': 1}]
        households = [self.hh('H1', 'D', 3, 3, 6), self.hh('H2', 'D', 8, 8, 6)]
        r = self.plan(households, [{'id': 'D', 'min_served': 0}], 8, tiers)
        self.assertEqual(r['objectives'], [1, 6, 3])
        self.assertEqual(r['selected_ids'], ['H1'])

    def test_final_id_tie_uses_python_string_order(self):
        # identical need/relief; only one fits. 'A10' < 'A9' as Python strings.
        households = [self.hh('A9', 'D', 4, 4, 4), self.hh('A10', 'D', 4, 4, 4)]
        r = self.plan(households, [{'id': 'D', 'min_served': 0}], 4)
        self.assertEqual(r['selected_ids'], ['A10'])

    def test_empty_district_positive_guarantee_infeasible(self):
        households = [self.hh('H1', 'D1', 2, 2, 2)]
        districts = [{'id': 'D1', 'min_served': 0}, {'id': 'D2', 'min_served': 1}]
        r = self.plan(households, districts, 10)
        self.assertEqual(r['business_status'], 'infeasible')
        self.assertIsNone(r['min_vouchers_for_guarantees'])  # D2 has nobody
        counts = {d['id']: d['eligible_count'] for d in r['districts']}
        self.assertEqual(counts, {'D1': 1, 'D2': 0})

    def test_infeasible_report_has_no_partial_allocation(self):
        # rate 1: each household needs 3 vouchers; two districts each demand one.
        households = [self.hh('H1', 'D1', 3, 3, 3), self.hh('H2', 'D2', 3, 3, 3)]
        districts = [{'id': 'D1', 'min_served': 1}, {'id': 'D2', 'min_served': 1}]
        r = self.plan(households, districts, 4)  # 3+3 > 4
        self.assertEqual(r['business_status'], 'infeasible')
        self.assertEqual(r['min_vouchers_for_guarantees'], 6)
        self.assertEqual(r['selected_ids'], [])
        self.assertTrue(all(not h['selected'] for h in r['households']))
        self.assertTrue(all(h['relief'] == 0 for h in r['households']))

    def test_brute_force_crosscheck(self):
        rng = random.Random(7)
        for _ in range(300):
            n_d = rng.randint(1, 3)
            districts = [{'id': 'D%d' % i, 'min_served': rng.randint(0, 2)}
                         for i in range(n_d)]
            households = []
            for i in range(rng.randint(0, 7)):
                households.append({'id': 'H%d' % i,
                                   'district': 'D%d' % rng.randrange(n_d),
                                   'quantity': rng.randint(0, 25),
                                   'credit_cap': rng.randint(0, 25),
                                   'target_relief': rng.randint(1, 40)})
            snap = snapshot(households=households, districts=districts,
                            credit_budget=rng.randint(0, 18),
                            tiers=[{'up_to': 8, 'rate': rng.choice([0, 2, 3])},
                                   {'up_to': None, 'rate': rng.choice([1, 4])}],
                            fixed_charge=rng.randint(0, 5))
            got = aid.analyze(snap)
            want = _brute(snap)
            if want['business_status'] == 'infeasible':
                self.assertEqual(got['business_status'], 'infeasible')
                self.assertEqual(got['min_vouchers_for_guarantees'],
                                 want['min_vouchers_for_guarantees'])
            else:
                self.assertEqual(got['business_status'], 'feasible')
                self.assertEqual(got['objectives'], want['objectives'])
                self.assertEqual(got['selected_ids'], want['selected_ids'])


def _brute(snap):
    """Independent exhaustive oracle over all household subsets."""
    s = aid.validate_snapshot(snap)
    tiers, fixed, budget = s['tiers'], s['fixed_charge'], s['credit_budget']
    district_ids = sorted(d['id'] for d in s['districts'])
    min_served = {d['id']: d['min_served'] for d in s['districts']}
    households = sorted(s['households'], key=lambda h: h['id'])
    need_of = {}
    for h in households:
        cap = min(h['quantity'], h['credit_cap'])
        need_of[h['id']] = aid.min_need(aid.curve_segments(tiers, h['quantity'], cap),
                                        h['target_relief'])
    eligible = {d: 0 for d in district_ids}
    for h in households:
        if need_of[h['id']] is not None:
            eligible[h['district']] += 1
    if any(eligible[d] < min_served[d] for d in district_ids):
        return {'business_status': 'infeasible', 'min_vouchers_for_guarantees': None}
    guarantee = sum(sum(sorted(need_of[h['id']][0] for h in households
                               if h['district'] == d and need_of[h['id']] is not None)
                        [:min_served[d]]) for d in district_ids)
    if guarantee > budget:
        return {'business_status': 'infeasible',
                'min_vouchers_for_guarantees': guarantee}
    pool = [h for h in households if need_of[h['id']] is not None]
    best = None
    for r in range(len(pool) + 1):
        for combo in itertools.combinations(pool, r):
            vouchers = sum(need_of[h['id']][0] for h in combo)
            if vouchers > budget:
                continue
            counts = {d: 0 for d in district_ids}
            for h in combo:
                counts[h['district']] += 1
            if any(counts[d] < min_served[d] for d in district_ids):
                continue
            relief = sum(need_of[h['id']][1] for h in combo)
            ids = sorted(h['id'] for h in combo)
            key = (len(combo), relief, -vouchers)
            if best is None or key > best[0] or (key == best[0] and ids < best[1]):
                best = (key, ids, vouchers, relief)
    key, ids, vouchers, relief = best
    return {'business_status': 'feasible',
            'objectives': [key[0], key[1], vouchers], 'selected_ids': ids}


class Linkage(unittest.TestCase):
    """Same two-district sample; only H1's target_relief moves across the
    zero-rate plateau and beyond reach.  need, the selected list and the
    feasibility status must all follow the exact curve, never the raw target."""

    def setUp(self):
        self.base = fixture()

    def with_target(self, target):
        snap = json.loads(json.dumps(self.base))
        for h in snap['households']:
            if h['id'] == 'H1':
                h['target_relief'] = target
        return snap

    def test_phase_before_plateau(self):
        r = aid.analyze(self.with_target(12))
        h1 = next(h for h in r['households'] if h['id'] == 'H1')
        self.assertEqual(h1['need'], 4)
        self.assertEqual(h1['relief'], 12)
        self.assertEqual(r['business_status'], 'feasible')
        self.assertEqual(r['selected_ids'], ['H1', 'H2', 'H3'])
        self.assertEqual(r['objectives'], [3, 52, 17])

    def test_phase_after_plateau_need_jumps(self):
        r = aid.analyze(self.with_target(16))
        h1 = next(h for h in r['households'] if h['id'] == 'H1')
        # target grew 12 -> 16 but the zero plateau forces need 4 -> 16;
        # the allocator must not treat target_relief as a voucher count.
        self.assertEqual(h1['need'], 16)
        self.assertEqual(h1['relief'], 20)
        self.assertEqual(r['business_status'], 'feasible')
        self.assertEqual(r['selected_ids'], ['H1', 'H2'])  # H3 priced out
        self.assertEqual(r['objectives'], [2, 30, 18])

    def test_phase_unreachable_turns_infeasible(self):
        r = aid.analyze(self.with_target(66))  # max relief is 65
        h1 = next(h for h in r['households'] if h['id'] == 'H1')
        self.assertFalse(h1['eligible'])
        self.assertIsNone(h1['need'])
        self.assertEqual(r['business_status'], 'infeasible')
        self.assertIsNone(r['min_vouchers_for_guarantees'])  # D1 has nobody
        self.assertEqual(r['selected_ids'], [])
        self.assertIsNone(r['objectives'])

    def test_fixed_charge_never_relieved(self):
        # fixed_charge is 7 in the fixture; relief must come from variable only.
        for target in (12, 16, 66):
            r = aid.analyze(self.with_target(target))
            for h in r['households']:
                q = h['quantity']
                cap = min(q, h['credit_cap'])
                tiers = self.base['tiers']
                variable = bill(tiers, 0, q)
                top = h['curve'][-1]
                self.assertEqual(top['a'] * cap + top['b'], variable)  # S(cap)=V(q)
                self.assertEqual(h['payable_after'] >= self.base['fixed_charge'], True)
                if h['selected']:
                    self.assertEqual(h['payable_after'],
                                     bill(tiers, self.base['fixed_charge'], q - h['need']))


class Validation(unittest.TestCase):
    def bad(self, mutate):
        snap = fixture()
        mutate(snap)
        with self.assertRaises((ValueError, TypeError)):
            aid.analyze(snap)

    def test_unknown_top_field(self):
        self.bad(lambda s: s.update(extra=1))

    def test_missing_field(self):
        self.bad(lambda s: s.pop('credit_budget'))

    def test_duplicate_household_id(self):
        self.bad(lambda s: s['households'].append(dict(s['households'][0])))

    def test_duplicate_district_id(self):
        self.bad(lambda s: s['districts'].append(dict(s['districts'][0])))

    def test_unknown_district_reference(self):
        self.bad(lambda s: s['households'][0].update(district='GHOST'))

    def test_unknown_household_field(self):
        self.bad(lambda s: s['households'][0].update(nickname='x'))

    def test_boolean_pseudo_integer(self):
        for field in ('quantity', 'credit_cap', 'target_relief'):
            self.bad(lambda s: s['households'][0].update({field: True}))
        self.bad(lambda s: s.update(credit_budget=False))
        self.bad(lambda s: s['districts'][0].update(min_served=True))

    def test_bounds(self):
        self.bad(lambda s: s['households'][0].update(quantity=10**12 + 1))
        self.bad(lambda s: s['households'][0].update(credit_cap=-1))
        self.bad(lambda s: s['households'][0].update(target_relief=0))
        self.bad(lambda s: s['households'][0].update(target_relief=10**24 + 1))
        self.bad(lambda s: s.update(credit_budget=10001))
        self.bad(lambda s: s.update(credit_budget=-1))

    def test_cardinality_caps(self):
        self.bad(lambda s: s.update(households=s['households'] * 76))  # 304 > 300
        self.bad(lambda s: s.update(districts=s['districts'] * 16))    # 32 > 30
        self.bad(lambda s: s.update(tiers=s['tiers'] * 34))            # 102 > 100

    def test_tier_contract_inherited(self):
        self.bad(lambda s: s.update(tiers=[{'up_to': 5, 'rate': 1}]))  # no unbounded
        self.bad(lambda s: s['tiers'].insert(0, {'up_to': None, 'rate': 1}))
        self.bad(lambda s: s['tiers'][0].update(rate=10**9 + 1))


class Cli(unittest.TestCase):
    def call(self, *args):
        return subprocess.run([sys.executable, '-m', 'tariff_rating.cli',
                               *map(str, args)],
                              capture_output=True, text=True, env=ENV, cwd=ROOT)

    def test_report_written_and_replayed(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'report.json'
            src = ROOT / 'fixtures/aid.json'
            r = self.call('aid-plan', '--source', src, '--output', out)
            self.assertEqual(r.returncode, 0, r.stderr)
            first = out.read_bytes()
            report = json.loads(first)
            self.assertEqual(report['schema_version'], 3)
            self.assertEqual(report['result']['business_status'], 'feasible')
            self.assertEqual(report['result']['objectives'], [3, 52, 17])
            self.assertEqual(self.call('aid-plan', '--source', src,
                                       '--output', out).returncode, 0)
            self.assertEqual(out.read_bytes(), first)  # replay is byte-identical

    def test_shuffled_input_same_report(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            base = fixture()
            reports = []
            for seed in range(3):
                snap = json.loads(json.dumps(base))
                rng = random.Random(seed)
                rng.shuffle(snap['households'])
                rng.shuffle(snap['districts'])
                src = td / ('in%d.json' % seed)
                out = td / ('out%d.json' % seed)
                src.write_text(json.dumps(snap))
                self.assertEqual(self.call('aid-plan', '--source', src,
                                           '--output', out).returncode, 0)
                reports.append(out.read_bytes())
            self.assertEqual(len(set(reports)), 1)

    def test_infeasible_is_legal_report_exit_zero(self):
        with tempfile.TemporaryDirectory() as td:
            snap = fixture()
            for h in snap['households']:
                if h['id'] == 'H1':
                    h['target_relief'] = 66
            src = Path(td) / 'in.json'
            out = Path(td) / 'out.json'
            src.write_text(json.dumps(snap))
            r = self.call('aid-plan', '--source', src, '--output', out)
            self.assertEqual(r.returncode, 0, r.stderr)
            report = json.loads(out.read_text())
            self.assertEqual(report['result']['business_status'], 'infeasible')
            self.assertEqual(report['result']['selected_ids'], [])

    def test_failure_exit_two_preserves_old_report(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            out = td / 'report.json'
            out.write_text('PRIOR')
            bad = td / 'bad.json'
            for body in ('{}', '{"a":1,"a":2}', 'NaN'):
                bad.write_text(body)
                r = self.call('aid-plan', '--source', bad, '--output', out)
                self.assertEqual(r.returncode, 2)
                self.assertEqual(out.read_text(), 'PRIOR')
            snap = fixture()
            snap['households'][0]['district'] = 'GHOST'
            bad.write_text(json.dumps(snap))
            r = self.call('aid-plan', '--source', bad, '--output', out)
            self.assertEqual(r.returncode, 2)
            self.assertEqual(out.read_text(), 'PRIOR')

    def test_same_path_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / 'in.json'
            src.write_bytes((ROOT / 'fixtures/aid.json').read_bytes())
            self.assertEqual(self.call('aid-plan', '--source', src,
                                       '--output', src).returncode, 2)
            link = td / 'link.json'
            link.symlink_to(src)
            self.assertEqual(self.call('aid-plan', '--source', src,
                                       '--output', link).returncode, 2)


class Performance(unittest.TestCase):
    def test_upper_limits_complete(self):
        rng = random.Random(11)
        tiers, bound = [], 0
        for _ in range(99):
            bound += rng.randint(1, 10**10)
            tiers.append({'up_to': bound,
                          'rate': rng.choice([0, 1, 5, 100, 10**6, 10**9])})
        tiers.append({'up_to': None, 'rate': 10**9})
        districts = [{'id': 'D%02d' % i, 'min_served': (i % 4) + 1}
                     for i in range(30)]
        households = []
        for i in range(300):
            households.append({'id': 'H%03d' % i, 'district': 'D%02d' % (i // 10),
                               'quantity': rng.randint(10**6, 10**12),
                               'credit_cap': rng.randint(10**3, 10**6),
                               'target_relief': rng.randint(1, 10**9)})
        snap = snapshot(households=households, districts=districts,
                        credit_budget=10000, tiers=tiers, fixed_charge=10**9)
        import time
        start = time.time()
        r = aid.analyze(snap)
        self.assertLess(time.time() - start, 60)
        self.assertEqual(r['business_status'], 'feasible')
        # report is a deterministic function of the snapshot, not of magnitudes
        self.assertEqual(sum(h['selected'] for h in r['households']),
                         r['objectives'][0])


if __name__ == '__main__':
    unittest.main()
