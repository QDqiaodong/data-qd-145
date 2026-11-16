"""Storage-plan tests: prefix reconstruction, robust suffix, CLI, linkage."""
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tariff_rating import storage

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
ENV = {**os.environ, 'PYTHONPATH': str(SRC)}


# --------------------------------------------------------------------------
# independent reference oracle: exhaustive enumeration of short domains
# --------------------------------------------------------------------------

def _options(snap, position):
    battery = snap['battery']
    slot = snap['slots'][position]
    out = []
    for action in range(-battery['discharge_limit'], battery['charge_limit'] + 1):
        drawn = slot['load'] + 2 * action if action > 0 else slot['load'] + action
        if 0 <= drawn <= snap['grid_limit']:
            out.append((drawn, action))
    return out


def _energy(snap, position, drawn):
    tiers = next(t['tiers'] for t in snap['tariffs']
                 if t['id'] == snap['slots'][position]['tariff_id'])
    total = 0
    lower = 0
    for tier in tiers:
        upper = tier['up_to']
        taken = max(0, (drawn if upper is None else min(drawn, upper)) - lower)
        total += taken * tier['rate']
        if upper is None:
            break
        lower = upper
    return total


def prefix_histories(snap):
    """Every legal prefix: (actions, imports, cost, peak, end_soc)."""
    battery = snap['battery']
    capacity = battery['capacity']
    k = len(snap['prefix'])
    out = []

    def walk(position, soc, actions, imports, cost, peak):
        if position == k:
            out.append((list(actions), list(imports), cost, peak, soc))
            return
        record = snap['prefix'][position]
        for drawn, action in _options(snap, position):
            if not record['import_lo'] <= drawn <= record['import_hi']:
                continue
            after = soc + action
            if not 0 <= after <= capacity:
                continue
            if record['soc_after'] is not None and after != record['soc_after']:
                continue
            pair = (imports[-1] if imports else 0) + drawn
            walk(position + 1, after, actions + [action], imports + [drawn],
                 cost + _energy(snap, position, drawn)
                 + snap['wear_rate'] * abs(action),
                 peak if peak >= pair else pair)
    walk(0, battery['initial_soc'], [], [], 0, 0)
    return out


def all_suffixes(snap):
    """Every legal suffix action tuple (independent of any prefix)."""
    battery = snap['battery']
    capacity = battery['capacity']
    k = len(snap['prefix'])
    out = []

    def walk(position, soc, actions):
        if position == len(snap['slots']):
            out.append(tuple(actions))
            return
        for _drawn, action in _options(snap, position):
            after = soc + action
            if 0 <= after <= capacity:
                walk(position + 1, after, actions + [action])
    # Enumerate from every possible pre-suffix SOC so the suffix list itself is
    # independent; safety against a prefix is checked separately.
    for soc in range(capacity + 1):
        walk(k, soc, [])
    return out


def suffix_cost(snap, prefix, suffix):
    """Full physical run of one prefix history plus the shared suffix.

    Returns ``(total, end_soc, peak)`` for the concatenated time series, so the
    demand pair crossing the planning boundary is counted once.
    """
    imports = list(prefix[1])
    soc = prefix[4]
    energy = prefix[2]   # prefix energy + wear, accumulated by prefix_histories
    k = len(prefix[0])
    for offset, action in enumerate(suffix):
        position = k + offset
        drawn = snap['slots'][position]['load'] + (2 * action if action > 0
                                                   else action)
        soc += action
        imports.append(drawn)
        energy += _energy(snap, position, drawn) + snap['wear_rate'] * abs(action)
    peak = 0
    for index in range(len(imports)):
        pair = (imports[index - 1] if index else 0) + imports[index]
        peak = max(peak, pair)
    return (energy + snap['demand_rate'] * peak + snap['fixed_charge']), soc, peak


def brute(snap):
    """Exhaustive oracle: safety, three-level objective, worst witness."""
    histories = prefix_histories(snap)
    if not histories:
        return {'business_status': 'infeasible', 'reason': 'inconsistent_prefix'}
    best = None
    for suffix in all_suffixes(snap):
        worst = None
        safe = True
        for history in histories:
            total, end_soc, peak = suffix_cost(snap, history, suffix)
            # suffix_cost recomputes the full physical run; reject if any
            # intermediate SOC was impossible (it returns end_soc only, so we
            # re-check explicitly).
            soc = history[4]
            ok = True
            for action in suffix:
                soc += action
                if not 0 <= soc <= snap['battery']['capacity']:
                    ok = False
                    break
            if not ok or soc < snap['battery']['reserve']:
                safe = False
                break
            if worst is None or total > worst[0] or \
                    (total == worst[0] and history[0] < worst[1]):
                worst = (total, history[0], peak)
        if not safe:
            continue
        key = (worst[0], sum(abs(a) for a in suffix), list(suffix))
        if best is None or key < best[0]:
            best = (key, worst[1])
    if best is None:
        return {'business_status': 'infeasible', 'reason': 'no_safe_schedule'}
    return {'business_status': 'feasible', 'objectives': best[0],
            'prefix_actions': best[1]}


def random_snapshot(rng):
    slots_count = rng.randint(1, 5)
    capacity = rng.randint(1, 4)
    grid = rng.randint(0, 6)
    charge = rng.randint(0, capacity)
    discharge = rng.randint(0, capacity)
    tariff_ids = [chr(ord('A') + i) for i in range(rng.randint(1, 2))]
    tariffs = []
    for tariff_id in tariff_ids:
        tiers = []
        bound = 0
        for _ in range(rng.randint(0, 1)):
            bound += rng.randint(1, 4)
            tiers.append({'up_to': bound, 'rate': rng.choice([0, 1, 2, 5])})
        tiers.append({'up_to': None, 'rate': rng.choice([0, 1, 3])})
        tariffs.append({'id': tariff_id, 'tiers': tiers})
    slots = [{'id': 'S%02d' % i,
              'load': rng.randint(0, min(4, grid)),
              'tariff_id': rng.choice(tariff_ids)}
             for i in range(slots_count)]
    k = rng.randint(0, min(8, slots_count))
    prefix = []
    for i in range(k):
        lo = rng.randint(0, grid)
        hi = rng.randint(lo, grid)
        anchor = rng.randint(0, capacity) if rng.random() < 0.4 else None
        prefix.append({'slot_id': slots[i]['id'], 'import_lo': lo,
                       'import_hi': hi, 'soc_after': anchor})
    return {
        'currency': 'T',
        'fixed_charge': rng.randint(0, 3),
        'tariffs': tariffs,
        'slots': slots,
        'battery': {'capacity': capacity,
                    'initial_soc': rng.randint(0, capacity),
                    'reserve': rng.randint(0, capacity),
                    'charge_limit': charge, 'discharge_limit': discharge},
        'grid_limit': grid,
        'wear_rate': rng.randint(0, 2),
        'demand_rate': rng.randint(0, 2),
        'prefix': prefix,
    }


class Oracle(unittest.TestCase):
    def test_random_against_brute_force(self):
        rng = random.Random(20261)
        seen = set()
        for _ in range(500):
            snap = random_snapshot(rng)
            got = storage.analyze(snap)
            want = brute(snap)
            self.assertEqual(got['business_status'], want['business_status'])
            if want['business_status'] == 'infeasible':
                self.assertEqual(got['reason'], want['reason'])
                seen.add(want['reason'])
            else:
                self.assertEqual(list(got['objectives']),
                                 list(want['objectives']))
                if snap['prefix']:
                    self.assertEqual(got['witness']['prefix_actions'],
                                     want['prefix_actions'])
                seen.add('feasible')
        self.assertEqual(seen, {'inconsistent_prefix', 'no_safe_schedule',
                                'feasible'})

    def test_report_witness_realises_reported_bill(self):
        rng = random.Random(7)
        for _ in range(300):
            snap = random_snapshot(rng)
            result = storage.analyze(snap)
            if result['business_status'] != 'feasible':
                continue
            witness = result['witness']
            actions = witness['prefix_actions'] + witness['suffix_actions']
            battery = snap['battery']
            soc = battery['initial_soc']
            imports = []
            energy = 0
            wear = 0
            k = len(snap['prefix'])
            for position, action in enumerate(actions):
                self.assertGreaterEqual(action, -battery['discharge_limit'])
                self.assertLessEqual(action, battery['charge_limit'])
                drawn = storage._import_of(snap['slots'][position]['load'], action)
                self.assertTrue(0 <= drawn <= snap['grid_limit'])
                soc += action
                self.assertTrue(0 <= soc <= battery['capacity'])
                if position < k:
                    record = snap['prefix'][position]
                    self.assertTrue(record['import_lo'] <= drawn <= record['import_hi'])
                    if record['soc_after'] is not None:
                        self.assertEqual(soc, record['soc_after'])
                imports.append(drawn)
                energy += _energy(snap, position, drawn)
                wear += snap['wear_rate'] * abs(action)
            self.assertGreaterEqual(soc, battery['reserve'])
            peak = max((imports[i - 1] if i else 0) + imports[i]
                       for i in range(len(imports)))
            bill = result['worst_bill']
            self.assertEqual(bill['energy_charge'], energy)
            self.assertEqual(bill['wear_charge'], wear)
            self.assertEqual(bill['peak'], peak)
            self.assertEqual(bill['demand_charge'], snap['demand_rate'] * peak)
            self.assertEqual(bill['total'],
                             energy + wear + snap['demand_rate'] * peak
                             + snap['fixed_charge'])


class Reconstruction(unittest.TestCase):
    """Reachable triples, per-triple max energy+wear and lexicographic witness."""

    def test_boundary_triples_and_witnesses(self):
        rng = random.Random(99)
        for _ in range(200):
            snap = random_snapshot(rng)
            result = storage.analyze(snap)
            histories = prefix_histories(snap)
            if not histories:
                self.assertEqual(result['business_status'], 'infeasible')
                self.assertEqual(result['reason'], 'inconsistent_prefix')
                continue
            expected = {}
            for actions, imports, cost, peak, soc in histories:
                key = (soc, imports[-1] if imports else 0, peak)
                candidate = (cost, actions)
                if key not in expected or candidate[0] > expected[key][0] or \
                        (candidate[0] == expected[key][0]
                         and candidate[1] < expected[key][1]):
                    expected[key] = candidate
            reachable = result['prefix']['reachable']
            self.assertEqual(len(reachable), len(expected))
            for entry in reachable:
                key = (entry['soc'], entry['last_import'], entry['peak'])
                cost, actions = expected[key]
                self.assertEqual(entry['max_energy_wear'], cost)
                self.assertEqual(entry['actions'], actions)
            socs = [key[0] for key in expected]
            self.assertEqual(result['prefix']['soc_range'], [min(socs), max(socs)])

    def test_anchor_excludes_histories(self):
        snap = {
            'currency': 'T', 'fixed_charge': 0,
            'tariffs': [{'id': 'A', 'tiers': [{'up_to': None, 'rate': 1}]}],
            'slots': [{'id': 'S0', 'load': 2, 'tariff_id': 'A'}],
            'battery': {'capacity': 4, 'initial_soc': 0, 'reserve': 0,
                        'charge_limit': 2, 'discharge_limit': 2},
            'grid_limit': 6, 'wear_rate': 0, 'demand_rate': 0,
            'prefix': [{'slot_id': 'S0', 'import_lo': 0, 'import_hi': 6,
                        'soc_after': 2}],
        }
        result = storage.analyze(snap)
        self.assertEqual(result['business_status'], 'feasible')
        self.assertEqual(result['prefix']['soc_range'], [2, 2])
        self.assertEqual([e['soc'] for e in result['prefix']['reachable']], [2])
        # no legal action reaches the pinned anchor
        snap['prefix'][0]['soc_after'] = 4
        snap['battery']['charge_limit'] = 1
        impossible = storage.analyze(snap)
        self.assertEqual(impossible['business_status'], 'infeasible')
        self.assertEqual(impossible['reason'], 'inconsistent_prefix')

    def test_empty_prefix_is_one_empty_triple(self):
        snap = random_snapshot(random.Random(1))
        snap['prefix'] = []
        snap['battery']['initial_soc'] = snap['battery']['capacity']
        result = storage.analyze(snap)
        if result['business_status'] == 'feasible':
            self.assertEqual(len(result['prefix']['reachable']), 1)
            entry = result['prefix']['reachable'][0]
            self.assertEqual(entry['soc'], snap['battery']['capacity'])
            self.assertEqual(entry['last_import'], 0)
            self.assertEqual(entry['peak'], 0)
            self.assertEqual(entry['actions'], [])

    def test_full_prefix_still_checks_terminal_reserve(self):
        snap = {
            'currency': 'T', 'fixed_charge': 0,
            'tariffs': [{'id': 'A', 'tiers': [{'up_to': None, 'rate': 1}]}],
            'slots': [{'id': 'S0', 'load': 2, 'tariff_id': 'A'}],
            'battery': {'capacity': 4, 'initial_soc': 0, 'reserve': 3,
                        'charge_limit': 2, 'discharge_limit': 2},
            'grid_limit': 6, 'wear_rate': 0, 'demand_rate': 0,
            'prefix': [{'slot_id': 'S0', 'import_lo': 0, 'import_hi': 6,
                        'soc_after': 0}],
        }
        result = storage.analyze(snap)
        self.assertEqual(result['business_status'], 'infeasible')
        self.assertEqual(result['reason'], 'no_safe_schedule')
        # relaxing the reserve makes the exact k=T path feasible
        snap['battery']['reserve'] = 0
        result = storage.analyze(snap)
        self.assertEqual(result['business_status'], 'feasible')
        self.assertEqual(result['suffix']['actions'], [])
        self.assertEqual(result['objectives'], [result['worst_bill']['total'], 0, []])

    def test_zero_grid_and_zero_battery_limits(self):
        snap = {
            'currency': 'T', 'fixed_charge': 1,
            'tariffs': [{'id': 'A', 'tiers': [{'up_to': None, 'rate': 2}]}],
            'slots': [{'id': 'S0', 'load': 0, 'tariff_id': 'A'}],
            'battery': {'capacity': 3, 'initial_soc': 0, 'reserve': 0,
                        'charge_limit': 0, 'discharge_limit': 0},
            'grid_limit': 0, 'wear_rate': 0, 'demand_rate': 5,
            'prefix': [],
        }
        result = storage.analyze(snap)
        self.assertEqual(result['business_status'], 'feasible')
        self.assertEqual(result['suffix']['actions'], [0])
        self.assertEqual(result['worst_bill']['total'], 1)


class Planning(unittest.TestCase):
    def plan(self, snap):
        return storage.analyze(snap)

    def test_cheap_first_greedy_fails(self):
        # Free electricity in slot 0 invites greedy charging to serve slot 1.
        # But storing draws 2 units per stored unit and the two-slot demand
        # pair then explodes: the optimum refuses to charge at all.
        snap = {
            'currency': 'T', 'fixed_charge': 0,
            'tariffs': [{'id': 'C', 'tiers': [{'up_to': None, 'rate': 0}]},
                        {'id': 'P', 'tiers': [{'up_to': None, 'rate': 5}]}],
            'slots': [{'id': 'S0', 'load': 0, 'tariff_id': 'C'},
                      {'id': 'S1', 'load': 2, 'tariff_id': 'P'}],
            'battery': {'capacity': 2, 'initial_soc': 0, 'reserve': 0,
                        'charge_limit': 1, 'discharge_limit': 1},
            'grid_limit': 8, 'wear_rate': 0, 'demand_rate': 10,
            'prefix': [],
        }
        result = self.plan(snap)
        self.assertEqual(result['business_status'], 'feasible')
        # Greedy: store 1 in slot 0 (import 2), discharge it in slot 1
        # (import 1) -> energy 5 + demand 10*(2+1) = 35.
        # Optimum: no storage -> energy 10 + demand 10*(0+2) = 30.
        self.assertEqual(result['suffix']['actions'], [0, 0])
        self.assertEqual(result['objectives'], [30, 0, [0, 0]])

    def test_decreasing_tier_prices(self):
        snap = {
            'currency': 'T', 'fixed_charge': 0,
            'tariffs': [{'id': 'A', 'tiers': [{'up_to': 4, 'rate': 9},
                                              {'up_to': 8, 'rate': 2},
                                              {'up_to': None, 'rate': 1}]}],
            'slots': [{'id': 'S0', 'load': 2, 'tariff_id': 'A'},
                      {'id': 'S1', 'load': 2, 'tariff_id': 'A'}],
            'battery': {'capacity': 2, 'initial_soc': 0, 'reserve': 0,
                        'charge_limit': 2, 'discharge_limit': 2},
            'grid_limit': 8, 'wear_rate': 0, 'demand_rate': 0,
            'prefix': [],
        }
        result = self.plan(snap)
        # Only variable energy; grid import is available at decreasing prices,
        # so the cheapest plan never stores and just serves the load.
        self.assertEqual(result['suffix']['actions'], [0, 0])
        self.assertEqual(result['worst_bill']['energy_charge'], 18 + 18)

    def test_terminal_reserve_forces_charging(self):
        snap = {
            'currency': 'T', 'fixed_charge': 0,
            'tariffs': [{'id': 'A', 'tiers': [{'up_to': None, 'rate': 1}]}],
            'slots': [{'id': 'S0', 'load': 0, 'tariff_id': 'A'},
                      {'id': 'S1', 'load': 0, 'tariff_id': 'A'}],
            'battery': {'capacity': 4, 'initial_soc': 0, 'reserve': 2,
                        'charge_limit': 1, 'discharge_limit': 1},
            'grid_limit': 6, 'wear_rate': 0, 'demand_rate': 0,
            'prefix': [],
        }
        result = self.plan(snap)
        self.assertEqual(result['business_status'], 'feasible')
        self.assertEqual(sum(result['suffix']['actions']) >= 2, True)
        self.assertGreaterEqual(result['suffix']['soc_range_after'][0], 2)

    def test_action_tie_breaks_to_lexicographic(self):
        # Zero energy, wear and demand: any safe suffix is free; the tie must
        # minimise sum(abs) then the action list, choosing the most negative
        # (never storing) schedule.
        snap = {
            'currency': 'T', 'fixed_charge': 0,
            'tariffs': [{'id': 'A', 'tiers': [{'up_to': None, 'rate': 0}]}],
            'slots': [{'id': 'S0', 'load': 2, 'tariff_id': 'A'}],
            'battery': {'capacity': 2, 'initial_soc': 2, 'reserve': 0,
                        'charge_limit': 2, 'discharge_limit': 2},
            'grid_limit': 6, 'wear_rate': 0, 'demand_rate': 0,
            'prefix': [],
        }
        result = self.plan(snap)
        self.assertEqual(result['suffix']['actions'], [0])
        self.assertEqual(result['objectives'], [0, 0, [0]])

    def test_prefix_soc_changes_common_suffix(self):
        base = {
            'currency': 'T', 'fixed_charge': 0,
            'tariffs': [{'id': 'A', 'tiers': [{'up_to': None, 'rate': 1}]}],
            'slots': [{'id': 'S0', 'load': 2, 'tariff_id': 'A'},
                      {'id': 'S1', 'load': 2, 'tariff_id': 'A'},
                      {'id': 'S2', 'load': 2, 'tariff_id': 'A'}],
            'battery': {'capacity': 4, 'initial_soc': 0, 'reserve': 1,
                        'charge_limit': 1, 'discharge_limit': 1},
            'grid_limit': 6, 'wear_rate': 0, 'demand_rate': 0,
            'prefix': [{'slot_id': 'S0', 'import_lo': 0, 'import_hi': 6,
                        'soc_after': None}],
        }
        wide = self.plan(base)
        pinned = json.loads(json.dumps(base))
        pinned['prefix'][0]['soc_after'] = 1
        tight = self.plan(pinned)
        self.assertEqual(wide['business_status'], 'feasible')
        self.assertEqual(tight['business_status'], 'feasible')
        self.assertEqual(wide['prefix']['soc_range'], [0, 1])
        self.assertEqual(tight['prefix']['soc_range'], [1, 1])
        # The shared suffix is safe against the whole wide SOC window: the
        # feasible suffix corpus depends on the anchor's reachable range.
        for history in prefix_histories(base) + prefix_histories(pinned):
            soc = history[4]
            for action in wide['suffix']['actions']:
                soc += action
                self.assertTrue(0 <= soc <= 4)
            self.assertGreaterEqual(soc, 1)


class Linkage(unittest.TestCase):
    """The anchor drives the common allowed actions and the worst bill."""

    def base(self):
        return json.loads((ROOT / 'fixtures/storage.json').read_text())

    def test_anchor_removal_changes_safety(self):
        # Search a small deterministic family for the documented linkage: the
        # optimal instruction for the anchored snapshot is unsafe once one
        # anchor is withdrawn, forcing a different safe suffix.
        rng = random.Random(3)
        found = None
        for _ in range(60000):
            snap = random_snapshot(rng)
            if not snap['prefix']:
                continue
            result = storage.analyze(snap)
            if result['business_status'] != 'feasible':
                continue
            anchors = [i for i, p in enumerate(snap['prefix'])
                       if p['soc_after'] is not None]
            if not anchors:
                continue
            relaxed = json.loads(json.dumps(snap))
            relaxed['prefix'][anchors[-1]]['soc_after'] = None
            after = storage.analyze(relaxed)
            unsafe = False
            for history in prefix_histories(relaxed):
                soc = history[4]
                ok = True
                for action in result['suffix']['actions']:
                    soc += action
                    if not 0 <= soc <= snap['battery']['capacity']:
                        ok = False
                        break
                if not ok or soc < snap['battery']['reserve']:
                    unsafe = True
                    break
            if not unsafe:
                continue
            if after['business_status'] == 'infeasible':
                found = (snap, result, relaxed, None)
                break
            if after['suffix']['actions'] != result['suffix']['actions']:
                found = (snap, result, relaxed, after)
                break
        self.assertIsNotNone(found, 'no anchor-removal linkage example found')
        snap, result, relaxed, after = found
        self.assertEqual(result['business_status'], 'feasible')
        # The relaxed problem either has no common safe suffix, or a strictly
        # different (still safe) instruction.
        if after is not None:
            self.assertNotEqual(after['suffix']['actions'],
                                result['suffix']['actions'])
            for history in prefix_histories(relaxed):
                soc = history[4]
                for action in after['suffix']['actions']:
                    soc += action
                    self.assertTrue(0 <= soc <= snap['battery']['capacity'])
                self.assertGreaterEqual(soc, snap['battery']['reserve'])
        else:
            self.assertEqual(after, None)
            self.assertEqual(storage.analyze(relaxed)['reason'], 'no_safe_schedule')

    def test_split_maxima_are_not_combined(self):
        # Prefix energy+wear peaks in one history, demand peak in another.  The
        # reported bill must come from a single realisable history whose own
        # energy, wear and peak are all reproduced by the witness.
        rng = random.Random(11)
        found = None
        for _ in range(30000):
            snap = random_snapshot(rng)
            histories = prefix_histories(snap)
            if len(histories) < 3:
                continue
            max_energy = max(histories, key=lambda h: h[2])
            max_peak = max(histories, key=lambda h: h[3])
            if max_energy[2] == max_peak[2] or max_energy[3] == max_peak[3]:
                continue
            result = storage.analyze(snap)
            if result['business_status'] != 'feasible':
                continue
            worst = max(histories,
                        key=lambda h: (h[2] + snap['demand_rate'] * h[3], -1))
            if result['witness']['prefix_actions'] != worst[0]:
                continue
            found = (snap, result, max_energy, max_peak)
            break
        self.assertIsNotNone(found, 'no split-maxima example found')
        snap, result, max_energy, max_peak = found
        witness = result['witness']
        actions = witness['prefix_actions'] + witness['suffix_actions']
        imports = witness['prefix_imports'] + witness['suffix_imports']
        soc = snap['battery']['initial_soc']
        energy = wear = 0
        for position, action in enumerate(actions):
            soc += action
            self.assertTrue(0 <= soc <= snap['battery']['capacity'])
            drawn = imports[position]
            self.assertEqual(drawn, storage._import_of(
                snap['slots'][position]['load'], action))
            energy += _energy(snap, position, drawn)
            wear += snap['wear_rate'] * abs(action)
        self.assertGreaterEqual(soc, snap['battery']['reserve'])
        peak = max((imports[i - 1] if i else 0) + imports[i]
                   for i in range(len(imports)))
        bill = result['worst_bill']
        # The witness itself, in one physical timeline, reproduces every fee.
        self.assertEqual(bill['energy_charge'], energy)
        self.assertEqual(bill['wear_charge'], wear)
        self.assertEqual(bill['peak'], peak)
        self.assertEqual(bill['demand_charge'], snap['demand_rate'] * peak)
        self.assertEqual(bill['total'], energy + wear
                         + snap['demand_rate'] * peak + snap['fixed_charge'])
        # prefix_energy_wear is exactly the prefix portion of that timeline.
        k = len(witness['prefix_actions'])
        prefix_energy = sum(_energy(snap, i, imports[i]) for i in range(k))
        prefix_wear = snap['wear_rate'] * sum(abs(a) for a in witness['prefix_actions'])
        self.assertEqual(witness['prefix_energy_wear'], prefix_energy + prefix_wear)
        # The prefix energy+wear maximum and the prefix peak maximum come from
        # different histories, yet one realisable witness realises the bill.
        self.assertNotEqual(max_energy[:2], max_peak[:2])

    def test_boundary_peak_bridges_prefix_and_suffix(self):
        # The demand fee uses grid[t-1]+grid[t]; the boundary pair joins the
        # prefix's last import with the suffix's first import.  Resetting the
        # peak at the planning boundary would understate the bill.
        snap = {
            'currency': 'T', 'fixed_charge': 0,
            'tariffs': [{'id': 'A', 'tiers': [{'up_to': None, 'rate': 0}]}],
            'slots': [{'id': 'S0', 'load': 4, 'tariff_id': 'A'},
                      {'id': 'S1', 'load': 4, 'tariff_id': 'A'}],
            'battery': {'capacity': 2, 'initial_soc': 1, 'reserve': 0,
                        'charge_limit': 0, 'discharge_limit': 0},
            'grid_limit': 8, 'wear_rate': 0, 'demand_rate': 3,
            'prefix': [{'slot_id': 'S0', 'import_lo': 4, 'import_hi': 4,
                        'soc_after': 1}],
        }
        result = storage.analyze(snap)
        self.assertEqual(result['business_status'], 'feasible')
        # Both imports are 4; the peak is 4+4=8, not just the suffix's 4.
        self.assertEqual(result['worst_bill']['peak'], 8)
        self.assertEqual(result['worst_bill']['demand_charge'], 24)
        self.assertEqual(result['worst_bill']['total'], 24)


class Inputs(unittest.TestCase):
    def fixture(self):
        return json.loads((ROOT / 'fixtures/storage.json').read_text())

    def bad(self, mutate):
        snap = self.fixture()
        mutate(snap)
        with self.assertRaises((ValueError, TypeError)):
            storage.analyze(snap)

    def test_unknown_and_missing_fields(self):
        self.bad(lambda s: s.update(extra=1))
        self.bad(lambda s: s.pop('prefix'))
        self.bad(lambda s: s['slots'][0].update(extra=0))
        self.bad(lambda s: s['battery'].update(extra=0))
        self.bad(lambda s: s['prefix'][0].update(extra=0))

    def test_duplicate_ids(self):
        self.bad(lambda s: s['tariffs'].append(dict(s['tariffs'][0])))
        self.bad(lambda s: s['slots'].append(dict(s['slots'][0])))
        self.bad(lambda s: s['prefix'].append(dict(s['prefix'][0])))

    def test_unknown_references(self):
        self.bad(lambda s: s['slots'][0].update(tariff_id='GHOST'))
        self.bad(lambda s: s['prefix'][0].update(slot_id='GHOST'))

    def test_boolean_pseudo_integers(self):
        for field in ('fixed_charge', 'grid_limit', 'wear_rate', 'demand_rate'):
            self.bad(lambda s, f=field: s.update({f: True}))
        for field in ('capacity', 'initial_soc', 'reserve',
                      'charge_limit', 'discharge_limit'):
            self.bad(lambda s, f=field: s['battery'].update({f: False}))
        self.bad(lambda s: s['slots'][0].update(load=True))
        self.bad(lambda s: s['prefix'][0].update(import_lo=False))

    def test_bounds(self):
        self.bad(lambda s: s['battery'].update(capacity=0))
        self.bad(lambda s: s['battery'].update(capacity=13))
        self.bad(lambda s: s['battery'].update(initial_soc=7))   # > capacity
        self.bad(lambda s: s['battery'].update(reserve=7))
        self.bad(lambda s: s['battery'].update(charge_limit=7))
        self.bad(lambda s: s['battery'].update(discharge_limit=7))
        self.bad(lambda s: s.update(grid_limit=25))
        self.bad(lambda s: s.update(wear_rate=-1))
        self.bad(lambda s: s.update(demand_rate=10**9 + 1))
        self.bad(lambda s: s['slots'][0].update(load=25))
        self.bad(lambda s: s['prefix'][0].update(import_lo=11))
        self.bad(lambda s: s['prefix'][0].update(import_hi=-1))
        self.bad(lambda s: s['prefix'][0].update(soc_after=7))

    def test_prefix_contract(self):
        # non-contiguous leading coverage
        self.bad(lambda s: s.update(prefix=[s['prefix'][1]]))
        self.bad(lambda s: s.update(prefix=[s['prefix'][0],
                                            {'slot_id': 'S03',
                                             'import_lo': 0, 'import_hi': 5,
                                             'soc_after': None}]))
        # lo > hi
        self.bad(lambda s: s['prefix'][0].update(import_lo=5, import_hi=4))
        # longer than the first min(8, slots) records
        self.bad(lambda s: s.update(prefix=[
            {'slot_id': s['slots'][i % len(s['slots'])]['id'], 'import_lo': 0,
             'import_hi': 5, 'soc_after': None} for i in range(5)]))

    def test_cardinality_caps(self):
        self.bad(lambda s: s.update(tariffs=s['tariffs'] * 13))     # 26 > 24
        self.bad(lambda s: s['tariffs'][0].update(
            tiers=[{'up_to': None, 'rate': 0}] * 11))                # 11 > 10
        slots = [{'id': 'X%02d' % i, 'load': 0, 'tariff_id': 'PEAK'}
                 for i in range(25)]
        self.bad(lambda s: s.update(slots=slots))                   # 25 > 24

    def test_tier_contract_inherited(self):
        self.bad(lambda s: s['tariffs'][0].update(
            tiers=[{'up_to': 5, 'rate': 1}]))                       # no unbounded
        self.bad(lambda s: s['tariffs'][0]['tiers'].insert(
            0, {'up_to': None, 'rate': 1}))
        self.bad(lambda s: s['tariffs'][0]['tiers'][0].update(rate=10**9 + 1))
        self.bad(lambda s: s['tariffs'][0]['tiers'][0].update(up_to=0))

    def test_tariff_and_prefix_order_do_not_matter(self):
        snap = self.fixture()
        base = storage.normalized(snap)
        shuffled = json.loads(json.dumps(snap))
        shuffled['tariffs'].reverse()
        shuffled['prefix'].reverse()
        self.assertEqual(storage.normalized(shuffled), base)


class Cli(unittest.TestCase):
    def call(self, *args):
        return subprocess.run([sys.executable, '-m', 'tariff_rating.cli',
                               *map(str, args)],
                              capture_output=True, text=True, env=ENV, cwd=ROOT)

    def test_report_written_with_schema_and_sha(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'report.json'
            source = ROOT / 'fixtures/storage.json'
            result = self.call('storage-plan', '--source', source, '--output', out)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(out.read_text())
            self.assertEqual(report['schema_version'], 4)
            self.assertEqual(report['result']['business_status'], 'feasible')
            snap = json.loads(source.read_text())
            from tariff_rating.common import sha256_hex
            self.assertEqual(report['source_sha256'],
                             sha256_hex(storage.normalized(snap)))

    def test_shuffled_input_same_report_and_sha(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            base = json.loads((ROOT / 'fixtures/storage.json').read_text())
            outputs = []
            for seed in range(3):
                snap = json.loads(json.dumps(base))
                rng = random.Random(seed)
                rng.shuffle(snap['tariffs'])
                rng.shuffle(snap['prefix'])
                source = td / ('in%d.json' % seed)
                out = td / ('out%d.json' % seed)
                source.write_text(json.dumps(snap))
                result = self.call('storage-plan', '--source', source,
                                   '--output', out)
                self.assertEqual(result.returncode, 0, result.stderr)
                outputs.append(out.read_bytes())
            self.assertEqual(len(set(outputs)), 1)

    def test_infeasible_exit_zero_with_reasons(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            source = td / 'in.json'
            out = td / 'out.json'

            snap = json.loads((ROOT / 'fixtures/storage.json').read_text())
            snap['battery']['capacity'] = 2
            snap['battery']['initial_soc'] = 0
            snap['battery']['charge_limit'] = 0
            snap['battery']['discharge_limit'] = 0
            snap['prefix'][0]['soc_after'] = 2
            for i in range(len(snap['prefix'])):
                snap['prefix'][i]['soc_after'] = None
            snap['prefix'][0]['soc_after'] = 2   # unreachable with limit 0
            source.write_text(json.dumps(snap))
            result = self.call('storage-plan', '--source', source, '--output', out)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(out.read_text())['result']['reason'],
                             'inconsistent_prefix')

            snap = json.loads((ROOT / 'fixtures/storage.json').read_text())
            snap['battery']['reserve'] = 6
            snap['battery']['initial_soc'] = 0
            snap['battery']['charge_limit'] = 0
            snap['prefix'] = []
            for slot in snap['slots']:
                slot['load'] = 0
            source.write_text(json.dumps(snap))
            result = self.call('storage-plan', '--source', source, '--output', out)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(out.read_text())['result']['reason'],
                             'no_safe_schedule')

    def test_failure_exit_two_preserves_old_report(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            out = td / 'report.json'
            out.write_text('PRIOR')
            bad = td / 'bad.json'
            for body in ('{}', '{"a":1,"a":1}', 'NaN'):
                bad.write_text(body)
                result = self.call('storage-plan', '--source', bad, '--output', out)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(out.read_text(), 'PRIOR')
            snap = json.loads((ROOT / 'fixtures/storage.json').read_text())
            snap['slots'][0]['tariff_id'] = 'GHOST'
            bad.write_text(json.dumps(snap))
            self.assertEqual(self.call('storage-plan', '--source', bad,
                                       '--output', out).returncode, 2)
            self.assertEqual(out.read_text(), 'PRIOR')

    def test_same_path_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            source = td / 'in.json'
            source.write_bytes((ROOT / 'fixtures/storage.json').read_bytes())
            self.assertEqual(self.call('storage-plan', '--source', source,
                                       '--output', source).returncode, 2)
            link = td / 'link.json'
            link.symlink_to(source)
            self.assertEqual(self.call('storage-plan', '--source', source,
                                       '--output', link).returncode, 2)

    def test_aid_and_legacy_entries_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            aid_out = td / 'aid.json'
            result = self.call('aid-plan', '--source', ROOT / 'fixtures/aid.json',
                               '--output', aid_out)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(aid_out.read_text())['schema_version'], 3)
            v1_out = td / 'v1.json'
            result = self.call('--source', ROOT / 'fixtures/input.json',
                               '--output', v1_out)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(v1_out.read_text())['schema_version'], 1)


class Limits(unittest.TestCase):
    def upper_limit_snapshot(self):
        loads = [2, 17, 10, 9, 9, 1, 15, 12, 9, 15, 7, 18, 6, 16, 2, 14,
                 15, 6, 9, 12, 0, 17, 3, 10]
        slots = [{'id': 'S%02d' % i, 'load': loads[i],
                  'tariff_id': 'A' if i % 2 else 'B'} for i in range(24)]
        tiers = [{'up_to': 10, 'rate': 9}, {'up_to': 20, 'rate': 1},
                 {'up_to': None, 'rate': 4}]
        return {
            'currency': 'T', 'fixed_charge': 10**9,
            'tariffs': [{'id': 'A', 'tiers': tiers},
                        {'id': 'B', 'tiers': [{'up_to': 5, 'rate': 10**9},
                                              {'up_to': None, 'rate': 0}]}],
            'slots': slots,
            'battery': {'capacity': 12, 'initial_soc': 0, 'reserve': 0,
                        'charge_limit': 12, 'discharge_limit': 12},
            'grid_limit': 24, 'wear_rate': 10**9, 'demand_rate': 10**9,
            'prefix': [{'slot_id': slots[i]['id'], 'import_lo': 0,
                        'import_hi': 24, 'soc_after': None} for i in range(8)],
        }

    def test_full_limits_complete_in_time(self):
        snap = self.upper_limit_snapshot()
        start = time.time()
        result = storage.analyze(snap)
        elapsed = time.time() - start
        self.assertLess(elapsed, 60)
        self.assertEqual(result['business_status'], 'feasible')
        self.assertEqual(result['objectives'][0], result['worst_bill']['total'])
        # All 8 prefix records were consumed and reconstructed.
        self.assertEqual(result['prefix']['slots'], 8)
        self.assertTrue(result['prefix']['reachable'])

    def test_max_tariff_catalogue_supported(self):
        tariffs = []
        for index in range(24):
            tiers = []
            upper = 0
            for tier in range(9):
                upper += 1
                tiers.append({'up_to': upper,
                              'rate': (index * 7 + tier) % 5})  # incl. zero/drops
            tiers.append({'up_to': None, 'rate': index % 3})
            tariffs.append({'id': 'T%02d' % index, 'tiers': tiers})
        slots = [{'id': 'S%02d' % i,
                  'tariff_id': 'T%02d' % (i % 24),
                  'load': (i * 5) % 13} for i in range(24)]
        snap = {'currency': 'T', 'fixed_charge': 3, 'tariffs': tariffs,
                'slots': slots,
                'battery': {'capacity': 12, 'initial_soc': 4, 'reserve': 2,
                            'charge_limit': 6, 'discharge_limit': 6},
                'grid_limit': 24, 'wear_rate': 1, 'demand_rate': 2,
                'prefix': []}
        result = storage.analyze(snap)
        self.assertEqual(result['business_status'], 'feasible')
        self.assertEqual(result['prefix']['soc_range'], [4, 4])
        # every in-range grid quantity under all 24 tariffs rates sanely
        for tariff in tariffs:
            for quantity in (0, 3, 11, 24):
                self.assertIsInstance(
                    storage._tier_charge(tariff['tiers'], quantity), int)

    def test_adversarial_layout_not_truncated(self):
        # A hill-climbed layout that maximises suffix state visits within the
        # widest achievable SOC window.  If the reachable set were truncated
        # the optimum would drift; the exact result is stable and fast.
        loads = [7, 8, 2, 21, 0, 12, 9, 0, 6, 11, 6, 5, 9, 6, 7, 9,
                 8, 21, 12, 20, 6, 15, 8, 8]
        slots = [{'id': 'S%02d' % i, 'load': loads[i], 'tariff_id': 'A'}
                 for i in range(24)]
        snap = {'currency': 'T', 'fixed_charge': 0,
                'tariffs': [{'id': 'A', 'tiers': [{'up_to': 10, 'rate': 9},
                                                  {'up_to': 20, 'rate': 1},
                                                  {'up_to': None, 'rate': 4}]}],
                'slots': slots,
                'battery': {'capacity': 12, 'initial_soc': 5, 'reserve': 0,
                            'charge_limit': 12, 'discharge_limit': 12},
                'grid_limit': 24, 'wear_rate': 2, 'demand_rate': 3,
                'prefix': []}
        start = time.time()
        result = storage.analyze(snap)
        self.assertLess(time.time() - start, 60)
        self.assertEqual(result['business_status'], 'feasible')
        self.assertEqual(result['objectives'],
                         [1627, 57, result['suffix']['actions']])
        # Repeated analysis is deterministic and byte-identical.
        self.assertEqual(storage.analyze(snap), result)

    def test_no_currency_magnitude_in_state(self):
        # Scaling money rates up must not change the number of explored states
        # or the plan; only the reported money values differ.
        snap = self.upper_limit_snapshot()
        scaled = json.loads(json.dumps(snap))
        scaled['fixed_charge'] = 10**8
        scaled['wear_rate'] = 10**8
        scaled['demand_rate'] = 10**8
        a = storage.analyze(snap)
        b = storage.analyze(scaled)
        self.assertEqual(len(a['prefix']['reachable']),
                         len(b['prefix']['reachable']))
        self.assertEqual(a['suffix']['actions'], b['suffix']['actions'])


if __name__ == '__main__':
    unittest.main()
