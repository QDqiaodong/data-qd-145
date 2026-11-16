"""Public usage-aid plan: relief curves and district-guarantee voucher allocation.

This module is independent of the meter ledger: it consumes one standalone JSON
snapshot of confirmed per-household quantities and produces one allocation round.
No reading revisions, period closing or invoice adjustment happen here.

Responsibilities:
  * validate the aid snapshot (v1 tariff semantics plus households/districts/budget),
  * build the exact piecewise-affine relief curve S(c) = B(q) - B(q-c) per household
    over the whole legal integer credit domain, in O(tiers) -- never per unit,
  * allocate indivisible vouchers under a global budget and per-district minimums
    with an exact lexicographic objective (count, relief, vouchers, id list).

Resource contract: state size depends only on household/district/tier counts and
the 0..10000 credit budget -- never on quantity, target_relief or money magnitudes.
"""
from .common import require, ident, number, rows
from .rating import rate

MAX_HOUSEHOLDS = 300
MAX_DISTRICTS = 30
MAX_TIERS = 100
MAX_BUDGET = 10000

# Bit layout of the folded DP value: (count << SHIFT_C) | (relief << SHIFT_B) | mask.
# relief sum is bounded by 300 households * 10^12 quantity * 10^9 rate < 2^80.
_RELIEF_BITS = 80


def _validate_tiers(raw):
    """v1 tier contract: strictly increasing finite bounds, unbounded last."""
    tiers = []
    previous = 0
    for t in rows(raw, MAX_TIERS):
        require(t, 'up_to rate')
        upper = t['up_to']
        price = number(t['rate'], 0, 10**9)
        if previous is None:
            raise ValueError('unbounded tier must be last')
        if upper is not None:
            number(upper, 1, 10**12)
            if upper <= previous:
                raise ValueError('tier boundaries must increase')
        tiers.append({'up_to': upper, 'rate': price})
        previous = upper
    if not tiers or tiers[-1]['up_to'] is not None:
        raise ValueError('last tier must be unbounded')
    return tiers


def validate_snapshot(data):
    """Structural validation of one aid-plan snapshot. Raises ValueError."""
    require(data, 'currency fixed_charge tiers households districts credit_budget')
    currency = ident(data['currency'])
    fixed = number(data['fixed_charge'], 0, 10**9)
    tiers = _validate_tiers(data['tiers'])
    budget = number(data['credit_budget'], 0, MAX_BUDGET)

    districts = []
    dseen = set()
    for d in rows(data['districts'], MAX_DISTRICTS):
        require(d, 'id min_served')
        did = ident(d['id'])
        if did in dseen:
            raise ValueError('duplicate district id: ' + did)
        dseen.add(did)
        ms = d['min_served']
        if type(ms) is not int or ms < 0:
            raise ValueError('min_served must be a non-negative integer')
        districts.append({'id': did, 'min_served': ms})

    households = []
    hseen = set()
    for h in rows(data['households'], MAX_HOUSEHOLDS):
        require(h, 'id district quantity credit_cap target_relief')
        hid = ident(h['id'])
        if hid in hseen:
            raise ValueError('duplicate household id: ' + hid)
        hseen.add(hid)
        district = ident(h['district'])
        if district not in dseen:
            raise ValueError('household %s references unknown district: %s' % (hid, district))
        households.append({
            'id': hid,
            'district': district,
            'quantity': number(h['quantity'], 0, 10**12),
            'credit_cap': number(h['credit_cap'], 0, 10**12),
            'target_relief': number(h['target_relief'], 1, 10**24),
        })
    return {'currency': currency, 'fixed_charge': fixed, 'tiers': tiers,
            'credit_budget': budget, 'districts': districts, 'households': households}


def bill(tiers, fixed, q):
    """Total bill B(q) = fixed charge + tiered variable charge (v1 semantics)."""
    return rate([{'id': '_', 'quantity': q}], tiers, fixed)['bills'][0]['total']


def curve_segments(tiers, q, cap):
    """Piecewise-affine relief curve S(c) = B(q) - B(q-c) over c in [0, cap].

    The fixed charge cancels in the difference, so only variable tier charges
    ever produce relief.  The (c -> c+1) increment is the true tier rate of
    unit q-c; rates may drop or be zero across tiers.  Returns segments
    {'lo','hi','a','b'} with S(c) = a*c + b, sorted by lo, gapless and
    non-overlapping; equal adjacent slopes are merged.  A boundary point takes
    the slope of its right increment (the domain end takes the left one; a
    domain of only c=0 yields a=b=0).  Runs in O(tiers), never per unit.
    """
    if cap == 0:
        return [{'lo': 0, 'hi': 0, 'a': 0, 'b': 0}]
    # Edge ranges: maximal c-intervals of [0, cap-1] whose increment p(c) is one
    # tier rate.  Credited units are (q-cap, q]; unit u maps to c = q - u.
    edges = []
    lower = 0
    for t in tiers:
        upper = t['up_to']
        lo_unit = max(lower, q - cap)          # exclusive unit lower bound
        hi_unit = q if upper is None else min(upper, q)
        if lo_unit < hi_unit:
            edges.append((q - hi_unit, q - lo_unit - 1, t['rate']))
        if upper is None or upper >= q:
            break
        lower = upper
    edges.reverse()  # tiers were swept from low units (high c) upward
    # Merge adjacent edge ranges sharing one slope.
    merged = []
    for c_lo, c_hi, price in edges:
        if merged and merged[-1][2] == price:
            merged[-1] = (merged[-1][0], c_hi, price)
        else:
            merged.append((c_lo, c_hi, price))
    segments = []
    relief_at_lo = 0
    for j, (c_lo, c_hi, price) in enumerate(merged):
        hi_point = cap if j == len(merged) - 1 else c_hi
        segments.append({'lo': c_lo, 'hi': hi_point, 'a': price,
                         'b': relief_at_lo - price * c_lo})
        relief_at_lo += price * (c_hi - c_lo + 1)
    return segments


def min_need(segments, target):
    """Minimal integer c with S(c) >= target, plus the actual relief S(c).

    Returns (need, relief) or None when the target is unreachable within the
    legal domain (zero-rate plateaus and the credit cap included).
    """
    for seg in segments:
        a, b, lo, hi = seg['a'], seg['b'], seg['lo'], seg['hi']
        if a * hi + b < target:
            continue
        short = target - (a * lo + b)
        need = lo if short <= 0 else lo + (short + a - 1) // a
        return need, a * need + b
    return None


def _fold(count, relief, mask, shift_b, shift_c):
    return (count << shift_c) | (relief << shift_b) | mask


def _scan(F, budget, shift_b, shift_c):
    """Best (count, relief, vouchers, mask) over a voucher-indexed DP array."""
    best = None
    for v in range(budget + 1):
        val = F[v]
        if val < 0:
            continue
        key = (val >> shift_c, (val >> shift_b) & ((1 << _RELIEF_BITS) - 1))
        if best is None or key > (best[0], best[1]) or \
                (key == (best[0], best[1]) and v < best[2]):
            best = (key[0], key[1], v, val & ((1 << shift_b) - 1))
    return best


def _allocate(districts, budget, shift_b):
    """Exact lexicographic optimum over feasible voucher allocations.

    ``districts`` is a list of (min_served, items) where each item is
    (need, relief, bit) for one selectable household (eligible, need <= budget).
    ``shift_b`` is the mask width in bits (one per household, candidates or not).
    Objectives in order: most selected, most total relief, fewest vouchers,
    lexicographically smallest sorted id list (the bit weights are powers of
    two by id rank, so the maximum mask is the lex-smallest list and decodes
    back to the exact selection).

    Returns (count, relief, vouchers, mask) or None when infeasible.
    """
    shift_c = shift_b + _RELIEF_BITS
    neg = -1
    per_district = [
        (m, [(need, _fold(1, relief, bit, shift_b, shift_c))
             for need, relief, bit in items])
        for m, items in districts]

    # Fast path: one unconstrained knapsack over all candidates.  When the
    # count-maximizing optimum already meets every district minimum it is the
    # global optimum; only binding minimums need the constrained DP below.
    if any(m > 0 for m, _ in per_district):
        F = [neg] * (budget + 1)
        F[0] = 0
        reach = 0
        for m, items in per_district:
            for need, delta in items:
                top = min(reach, budget - need)
                for v in range(top, -1, -1):
                    s = F[v]
                    if s >= 0:
                        cand = s + delta
                        if cand > F[v + need]:
                            F[v + need] = cand
                reach = min(budget, reach + need)
        fast = _scan(F, budget, shift_b, shift_c)
        if fast is not None:
            mask = fast[3]
            if all(sum(1 for _, _, bit in items if mask & bit) >= m
                   for m, items in districts):
                return fast

    F = [neg] * (budget + 1)
    F[0] = 0
    reach = 0
    for m, items in per_district:
        eligible = len(items)
        if m > eligible:
            return None
        if m == 0:
            for need, delta in items:
                top = min(reach, budget - need)
                for v in range(top, -1, -1):
                    s = F[v]
                    if s >= 0:
                        cand = s + delta
                        if cand > F[v + need]:
                            F[v + need] = cand
                reach = min(budget, reach + need)
        elif m <= eligible - m:
            # Inclusion framing: row x = selected count in this district, capped at m.
            rows_ = [[neg] * (budget + 1) for _ in range(m + 1)]
            rows_[0] = F
            for need, delta in items:
                top = min(reach, budget - need)
                row_m = rows_[m]
                for v in range(top, -1, -1):  # self-include inside the capped row
                    s = row_m[v]
                    if s >= 0:
                        cand = s + delta
                        if cand > row_m[v + need]:
                            row_m[v + need] = cand
                for x in range(m, 0, -1):     # include from row x-1 into row x
                    src, dst = rows_[x - 1], rows_[x]
                    for v in range(top, -1, -1):
                        s = src[v]
                        if s >= 0:
                            cand = s + delta
                            if cand > dst[v + need]:
                                dst[v + need] = cand
                reach = min(budget, reach + need)
            F = rows_[m]
        else:
            # Exclusion framing: row x = excluded count, capped at eligible - m.
            # Every item is included (stays in row, adds vouchers) or excluded
            # (advances a row, keeps vouchers); there is no free skip.
            cap_x = eligible - m
            rows_ = [[neg] * (budget + 1) for _ in range(cap_x + 1)]
            rows_[0] = F
            for need, delta in items:
                vmax = min(reach + need, budget)
                for x in range(cap_x, -1, -1):
                    row_x = rows_[x]
                    src_excl = rows_[x - 1] if x > 0 else None
                    for v in range(vmax, -1, -1):
                        best = neg
                        if v >= need:
                            s = row_x[v - need]
                            if s >= 0:
                                best = s + delta
                        if src_excl is not None:
                            s2 = src_excl[v]
                            if s2 >= 0 and s2 > best:
                                best = s2
                        row_x[v] = best
                reach = vmax
            F = rows_[0]
            for x in range(1, cap_x + 1):
                row_x = rows_[x]
                for v in range(reach + 1):
                    s = row_x[v]
                    if s > F[v]:
                        F[v] = s

    return _scan(F, budget, shift_b, shift_c)


def normalized(data):
    """Canonical snapshot: validated fields with households/districts by id.

    The aid-plan report fingerprints this normalized form, so permuting the
    household or district arrays (or JSON key order) yields the identical
    report; the raw byte hash used by v1 would not.
    """
    snap = validate_snapshot(data)
    return {
        'currency': snap['currency'],
        'fixed_charge': snap['fixed_charge'],
        'tiers': snap['tiers'],
        'credit_budget': snap['credit_budget'],
        'districts': sorted(snap['districts'], key=lambda d: d['id']),
        'households': sorted(snap['households'], key=lambda h: h['id']),
    }


def analyze(data):
    """Full aid-plan result for one validated snapshot."""
    snap = validate_snapshot(data)
    tiers = snap['tiers']
    fixed = snap['fixed_charge']
    budget = snap['credit_budget']
    district_ids = sorted(d['id'] for d in snap['districts'])
    min_served = {d['id']: d['min_served'] for d in snap['districts']}
    households = sorted(snap['households'], key=lambda h: h['id'])

    # ---- relief curves, eligibility and minimal need per household ----
    curves = {}
    info = {}
    for h in households:
        q = h['quantity']
        cap = min(q, h['credit_cap'])
        segments = curve_segments(tiers, q, cap)
        curves[h['id']] = segments
        found = min_need(segments, h['target_relief'])
        need, relief = found if found else (None, None)
        info[h['id']] = {
            'household': h, 'cap': cap, 'need': need, 'relief': relief,
            'bill': bill(tiers, fixed, q),
        }

    # ---- candidates: eligible households whose need fits the global budget ----
    rank = {h['id']: i for i, h in enumerate(households)}
    total = len(households)
    candidates = {d: [] for d in district_ids}
    eligible_count = {d: 0 for d in district_ids}
    guarantee_needs = {d: [] for d in district_ids}
    for h in households:
        entry = info[h['id']]
        d = h['district']
        if entry['need'] is None:
            continue
        eligible_count[d] += 1
        guarantee_needs[d].append(entry['need'])
        if entry['need'] <= budget:
            bit = 1 << (total - 1 - rank[h['id']])
            candidates[d].append((entry['need'], entry['relief'], bit))

    # ---- feasibility: district headcounts and the cheapest guarantee total ----
    short = any(eligible_count[d] < min_served[d] for d in district_ids)
    guarantee_total = None
    if not short:
        guarantee_total = sum(
            sum(sorted(guarantee_needs[d])[:min_served[d]]) for d in district_ids)
    feasible = not short and guarantee_total <= budget

    result = {
        'business_status': 'feasible' if feasible else 'infeasible',
        'currency': snap['currency'],
        'credit_budget': budget,
    }

    if feasible:
        plan = [(min_served[d], candidates[d]) for d in district_ids]
        count, relief_total, vouchers, mask = _allocate(plan, budget, max(1, total))
        selected = {h['id'] for h in households
                    if mask & (1 << (total - 1 - rank[h['id']]))}
        served = {d: 0 for d in district_ids}
        for h in households:
            if h['id'] in selected:
                served[h['district']] += 1
        result['objectives'] = [count, relief_total, vouchers]
        result['selected_ids'] = sorted(selected)
        result['totals'] = {'selected_count': count, 'total_relief': relief_total,
                            'total_vouchers': vouchers}
        result['districts'] = [
            {'id': d, 'min_served': min_served[d], 'eligible_count': eligible_count[d],
             'served': served[d]} for d in district_ids]
    else:
        selected = set()
        result['objectives'] = None
        result['selected_ids'] = []
        result['min_vouchers_for_guarantees'] = guarantee_total
        result['districts'] = [
            {'id': d, 'min_served': min_served[d], 'eligible_count': eligible_count[d]}
            for d in district_ids]

    result['households'] = [
        {'id': h['id'], 'district': h['district'], 'quantity': h['quantity'],
         'credit_cap': h['credit_cap'], 'target_relief': h['target_relief'],
         'eligible': info[h['id']]['need'] is not None,
         'need': info[h['id']]['need'],
         'relief': info[h['id']]['relief'] if h['id'] in selected else 0,
         'selected': h['id'] in selected,
         'payable_after': info[h['id']]['bill'] - (info[h['id']]['relief'] if h['id'] in selected else 0),
         'curve': curves[h['id']]}
        for h in households]
    return result
