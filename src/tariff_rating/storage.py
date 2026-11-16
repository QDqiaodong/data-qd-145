"""Storage-plan: one robust charge/discharge schedule from a standalone snapshot.

The snapshot holds a tariff catalogue, an ordered slot list, one battery, one
grid limit, fee rates and a possibly empty *prefix* of observed telemetry.  The
prefix never reveals the actual actions: each historical record only confines
the realised import to a closed integer interval and may pin the segment-end
state of charge exactly.

The module reconstructs every legal prefix history (collapsed to reachable
boundary triples) and then chooses a single open-loop suffix that is safe for
all of them.  All arithmetic is integer and only the standard library is used.

Resource contract: the prefix frontier holds at most
``(capacity+1) * (grid_limit+1) * (2*grid_limit+1)`` triples; the suffix DP is
indexed by ``(first_import, cumulative_offset, last_import, suffix_peak)``.
Neither legal histories nor suffix action sequences are enumerated, and no DP
state is expanded per currency unit.
"""
from .common import require, ident, number, rows

MAX_TARIFFS = 24
MAX_TIERS = 10
MAX_SLOTS = 24
MAX_PREFIX = 8
MAX_GRID = 24
MAX_CAPACITY = 12

_TOP = 'currency fixed_charge tariffs slots battery grid_limit wear_rate demand_rate prefix'
_TARIFF = 'id tiers'
_TIER = 'up_to rate'
_SLOT = 'id load tariff_id'
_BATTERY = 'capacity initial_soc reserve charge_limit discharge_limit'
_PREFIX = 'slot_id import_lo import_hi soc_after'


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def _tiers(raw):
    """v1 tier semantics: 1..10 tiers, increasing finite bounds, unbounded last."""
    tiers = []
    previous = 0
    for tier in rows(raw, MAX_TIERS):
        require(tier, _TIER)
        upper = tier['up_to']
        price = number(tier['rate'], 0, 10**9)
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
    """Structural validation of one storage snapshot.  Raises ValueError."""
    require(data, _TOP)
    currency = ident(data['currency'])
    fixed = number(data['fixed_charge'], 0, 10**9)
    wear = number(data['wear_rate'], 0, 10**9)
    demand = number(data['demand_rate'], 0, 10**9)
    grid_limit = number(data['grid_limit'], 0, MAX_GRID)

    tariffs = []
    seen_tariffs = set()
    for tariff in rows(data['tariffs'], MAX_TARIFFS):
        require(tariff, _TARIFF)
        tariff_id = ident(tariff['id'])
        if tariff_id in seen_tariffs:
            raise ValueError('duplicate tariff id: ' + tariff_id)
        seen_tariffs.add(tariff_id)
        tariffs.append({'id': tariff_id, 'tiers': _tiers(tariff['tiers'])})

    slots = []
    seen_slots = set()
    for slot in rows(data['slots'], MAX_SLOTS):
        require(slot, _SLOT)
        slot_id = ident(slot['id'])
        if slot_id in seen_slots:
            raise ValueError('duplicate slot id: ' + slot_id)
        seen_slots.add(slot_id)
        load = number(slot['load'], 0, MAX_SLOTS)
        tariff_id = ident(slot['tariff_id'])
        if tariff_id not in seen_tariffs:
            raise ValueError('slot references unknown tariff: ' + tariff_id)
        slots.append({'id': slot_id, 'load': load, 'tariff_id': tariff_id})
    if not slots:
        raise ValueError('at least one slot is required')

    require(data['battery'], _BATTERY)
    capacity = number(data['battery']['capacity'], 1, MAX_CAPACITY)
    initial = number(data['battery']['initial_soc'], 0, capacity)
    reserve = number(data['battery']['reserve'], 0, capacity)
    charge_limit = number(data['battery']['charge_limit'], 0, capacity)
    discharge_limit = number(data['battery']['discharge_limit'], 0, capacity)

    raw_prefix = rows(data['prefix'], MAX_PREFIX)
    prefix_len = len(raw_prefix)
    if prefix_len > min(MAX_PREFIX, len(slots)):
        raise ValueError('prefix is longer than the leading slots')
    expected = [slot['id'] for slot in slots[:prefix_len]]
    order = {slot_id: index for index, slot_id in enumerate(expected)}
    records = {}
    for record in raw_prefix:
        require(record, _PREFIX)
        slot_id = ident(record['slot_id'])
        if slot_id not in order:
            raise ValueError('prefix must cover exactly the leading slots: ' + slot_id)
        if slot_id in records:
            raise ValueError('duplicate prefix slot: ' + slot_id)
        lo = number(record['import_lo'], 0, grid_limit)
        hi = number(record['import_hi'], 0, grid_limit)
        if lo > hi:
            raise ValueError('prefix import_lo must not exceed import_hi')
        soc_after = record['soc_after']
        if soc_after is not None:
            soc_after = number(soc_after, 0, capacity)
        records[slot_id] = {'slot_id': slot_id, 'import_lo': lo,
                            'import_hi': hi, 'soc_after': soc_after}
    if len(records) != prefix_len:
        raise ValueError('prefix must cover exactly the leading slots')
    prefix = [records[slot_id] for slot_id in expected]

    return {
        'currency': currency,
        'fixed_charge': fixed,
        'tariffs': tariffs,
        'slots': slots,
        'battery': {'capacity': capacity, 'initial_soc': initial,
                    'reserve': reserve, 'charge_limit': charge_limit,
                    'discharge_limit': discharge_limit},
        'grid_limit': grid_limit,
        'wear_rate': wear,
        'demand_rate': demand,
        'prefix': prefix,
    }


def normalized(data):
    """Canonical snapshot: tariffs by id, prefix records in slot order.

    Permuting the tariff array, the prefix record array or JSON keys therefore
    yields the identical canonical JSON and the identical semantic input SHA.
    """
    snap = validate_snapshot(data)
    return {
        'currency': snap['currency'],
        'fixed_charge': snap['fixed_charge'],
        'tariffs': sorted(snap['tariffs'], key=lambda tariff: tariff['id']),
        'slots': snap['slots'],
        'battery': snap['battery'],
        'grid_limit': snap['grid_limit'],
        'wear_rate': snap['wear_rate'],
        'demand_rate': snap['demand_rate'],
        'prefix': snap['prefix'],
    }


# --------------------------------------------------------------------------
# small integer helpers
# --------------------------------------------------------------------------

def _options(load, grid_limit, charge_limit, discharge_limit):
    """Legal (import, action) pairs for one slot.

    action > 0 stores ``action`` units but draws ``2*action`` from the grid;
    action < 0 releases ``-action`` units and serves the load with it.  Hence
    import = load + 2*action (charge) or load + action (discharge).  Selling is
    impossible and import must fit the grid limit.
    """
    result = []
    for action in range(-discharge_limit, charge_limit + 1):
        drawn = load + 2 * action if action > 0 else load + action
        if 0 <= drawn <= grid_limit:
            result.append((drawn, action))
    return result


def _tier_charge(tiers, quantity):
    """Incremental tier charge for one quantity (v1 semantics)."""
    total = 0
    lower = 0
    for tier in tiers:
        upper = tier['up_to']
        taken = max(0, (quantity if upper is None else min(quantity, upper)) - lower)
        total += taken * tier['rate']
        if upper is None:
            break
        lower = upper
    return total


def _pack(digits, base, offset):
    value = 0
    for digit in digits:
        value = value * base + (digit + offset)
    return value


def _unpack(value, length, base, offset):
    out = [0] * length
    for index in range(length - 1, -1, -1):
        value, remainder = divmod(value, base)
        out[index] = remainder - offset
    return out


def _base(snap):
    battery = snap['battery']
    return battery['charge_limit'] + battery['discharge_limit'] + 1, \
        battery['discharge_limit']


# --------------------------------------------------------------------------
# prefix reconstruction
# --------------------------------------------------------------------------

def _prefix_frontier(snap, charges):
    """Reachable prefix boundary triples with their exact worst witnesses.

    Each triple (SOC, last import, prefix peak) retains the maximum prefix
    energy+wear over histories reaching it and, for that maximum, the
    lexicographically smallest action list (packed base-``base`` digits).

    Returns (states, smin, smax); states is None when no legal history exists.
    """
    battery = snap['battery']
    capacity = battery['capacity']
    grid_limit = snap['grid_limit']
    charge_limit = battery['charge_limit']
    discharge_limit = battery['discharge_limit']
    wear_rate = snap['wear_rate']
    base, offset = _base(snap)

    states = {(battery['initial_soc'], 0, 0): (0, 0, 0, 0)}
    for position, record in enumerate(snap['prefix']):
        slot = snap['slots'][position]
        options = _options(slot['load'], grid_limit, charge_limit, discharge_limit)
        table = charges[slot['tariff_id']]
        lo, hi = record['import_lo'], record['import_hi']
        anchor = record['soc_after']
        new = {}
        for (soc, last, peak), (cost, energy, wear, packed) in states.items():
            for drawn, action in options:
                if not lo <= drawn <= hi:
                    continue
                after = soc + action
                if not 0 <= after <= capacity:
                    continue
                if anchor is not None and after != anchor:
                    continue
                pair = last + drawn
                peak2 = peak if peak >= pair else pair
                energy2 = energy + table[drawn]
                wear2 = wear + wear_rate * (action if action >= 0 else -action)
                cost2 = energy2 + wear2
                packed2 = packed * base + (action + offset)
                key = (after, drawn, peak2)
                old = new.get(key)
                if old is None or cost2 > old[0] or \
                        (cost2 == old[0] and packed2 < old[3]):
                    new[key] = (cost2, energy2, wear2, packed2)
        states = new
        if not states:
            return None, None, None
    socs = [key[0] for key in states]
    return states, min(socs), max(socs)


# --------------------------------------------------------------------------
# suffix DP
# --------------------------------------------------------------------------

def _suffix_terminals(snap, charges, start, lo, hi, need):
    """Exact suffix DP over all slots from ``start``.

    State ``(first_import, cumulative_offset, last_import, suffix_peak)`` keeps
    the lexicographically smallest ``(cost, sum_abs, packed_actions)``.  For a
    fixed state every remaining contribution (energy, wear, demand coupling)
    is identical, so this dominance is exact for all three objectives.

    Returns a dict ``(first_import, suffix_peak) -> (cost, sum_abs, packed)``
    restricted to terminal states with cumulative offset >= ``need``, or None.
    ``lo``/``hi`` are the cumulative-offset bounds that keep *every* reachable
    prefix SOC inside 0..capacity.
    """
    battery = snap['battery']
    grid_limit = snap['grid_limit']
    charge_limit = battery['charge_limit']
    discharge_limit = battery['discharge_limit']
    wear_rate = snap['wear_rate']
    slots = snap['slots']
    total_slots = len(slots)
    base, offset = _base(snap)

    first_slot = slots[start]
    table = charges[first_slot['tariff_id']]
    states = {}
    for drawn, action in _options(first_slot['load'], grid_limit,
                                  charge_limit, discharge_limit):
        if not lo <= action <= hi:
            continue
        key = (drawn, action, drawn, 0)
        value = (table[drawn] + wear_rate * abs(action), abs(action),
                 action + offset)
        old = states.get(key)
        if old is None or value < old:
            states[key] = value
    if not states:
        return None

    for position in range(start + 1, total_slots):
        slot = slots[position]
        table = charges[slot['tariff_id']]
        options = _options(slot['load'], grid_limit, charge_limit, discharge_limit)
        new = {}
        for (first, cumulative, last, peak), (cost, total_abs, packed) in states.items():
            for drawn, action in options:
                cumulative2 = cumulative + action
                if cumulative2 < lo or cumulative2 > hi:
                    continue
                pair = last + drawn
                peak2 = peak if peak >= pair else pair
                wear = wear_rate * (action if action >= 0 else -action)
                value = (cost + table[drawn] + wear, total_abs + abs(action),
                         packed * base + (action + offset))
                key = (first, cumulative2, drawn, peak2)
                old = new.get(key)
                if old is None or value < old:
                    new[key] = value
        if not new:
            return None
        states = new

    terminals = {}
    for (first, cumulative, last, peak), value in states.items():
        if cumulative < need:
            continue
        key = (first, peak)
        old = terminals.get(key)
        if old is None or value < old:
            terminals[key] = value
    return terminals or None


# --------------------------------------------------------------------------
# report assembly
# --------------------------------------------------------------------------

def _import_of(load, action):
    return load + 2 * action if action > 0 else load + action


def _feasible_report(snap, prefix_block, suffix_actions, prefix_actions,
                     prefix_energy, prefix_wear, prefix_cost, peak,
                     suffix_energy, suffix_wear, total, smin, smax):
    battery = snap['battery']
    slots = snap['slots']
    k = len(prefix_actions)

    prefix_imports = []
    prefix_soc_after = []
    soc = battery['initial_soc']
    for position, action in enumerate(prefix_actions):
        drawn = _import_of(slots[position]['load'], action)
        soc += action
        prefix_imports.append(drawn)
        prefix_soc_after.append(soc)
    prefix_end_soc = soc  # == initial_soc + sum(prefix_actions)

    suffix_imports = []
    suffix_soc_after = []
    cumulative_final = 0
    for position, action in enumerate(suffix_actions):
        drawn = _import_of(slots[k + position]['load'], action)
        cumulative_final += action
        suffix_imports.append(drawn)
        suffix_soc_after.append(prefix_end_soc + cumulative_final)

    plan_slots = []
    cumulative = 0
    for position, action in enumerate(suffix_actions):
        drawn = _import_of(slots[k + position]['load'], action)
        cumulative += action
        plan_slots.append({
            'slot_id': slots[k + position]['id'],
            'action': action,
            'import': drawn,
            'soc_after_lo': smin + cumulative,
            'soc_after_hi': smax + cumulative,
        })

    energy = prefix_energy + suffix_energy
    wear = prefix_wear + suffix_wear
    demand_charge = snap['demand_rate'] * peak
    fixed = snap['fixed_charge']

    return {
        'business_status': 'feasible',
        'currency': snap['currency'],
        'prefix': prefix_block,
        'suffix': {
            'slots': plan_slots,
            'actions': list(suffix_actions),
            'sum_abs': sum(abs(action) for action in suffix_actions),
            'soc_range_after': [smin + cumulative_final, smax + cumulative_final],
        },
        'worst_bill': {
            'total': total,
            'energy_charge': energy,
            'wear_charge': wear,
            'demand_charge': demand_charge,
            'fixed_charge': fixed,
            'peak': peak,
        },
        'objectives': [total, sum(abs(action) for action in suffix_actions),
                       list(suffix_actions)],
        'witness': {
            'prefix_actions': list(prefix_actions),
            'prefix_imports': prefix_imports,
            'prefix_soc_after': prefix_soc_after,
            'suffix_actions': list(suffix_actions),
            'suffix_imports': suffix_imports,
            'suffix_soc_after': suffix_soc_after,
            'prefix_energy_wear': prefix_cost,
            'peak': peak,
        },
    }


def _prefix_block(snap, states, smin, smax):
    base, offset = _base(snap)
    k = len(snap['prefix'])
    reachable = [
        {'soc': soc, 'last_import': last, 'peak': peak,
         'max_energy_wear': value[0],
         'actions': _unpack(value[3], k, base, offset)}
        for (soc, last, peak), value in sorted(states.items())
    ]
    return {'slots': k, 'soc_range': [smin, smax], 'reachable': reachable}


def analyze(data):
    """Full storage-plan result for one validated snapshot."""
    snap = validate_snapshot(data)
    battery = snap['battery']
    capacity = battery['capacity']
    reserve = battery['reserve']
    grid_limit = snap['grid_limit']
    wear_rate = snap['wear_rate']
    demand_rate = snap['demand_rate']
    fixed = snap['fixed_charge']
    slots = snap['slots']
    total_slots = len(slots)
    k = len(snap['prefix'])
    base, offset = _base(snap)

    charges = {
        tariff['id']: [_tier_charge(tariff['tiers'], drawn)
                       for drawn in range(grid_limit + 1)]
        for tariff in snap['tariffs']
    }

    states, smin, smax = _prefix_frontier(snap, charges)
    if states is None:
        return {'business_status': 'infeasible', 'reason': 'inconsistent_prefix',
                'currency': snap['currency']}

    prefix_block = _prefix_block(snap, states, smin, smax)

    # Prefix classes (peak, last import) share the whole boundary coupling:
    # demand joins the class peak with the suffix's first import, and both the
    # class peak and last import are identical across its histories.  Retain
    # the maximum prefix cost and its lexicographic witness.
    classes = {}
    for (soc, last, peak), (cost, energy, wear, packed) in states.items():
        key = (peak, last)
        old = classes.get(key)
        if old is None or cost > old[0] or \
                (cost == old[0] and packed < old[3]):
            classes[key] = (cost, energy, wear, packed, soc)

    # Terminal reserve is universal: every reachable prefix SOC must be at
    # least reserve (the suffix is empty, so the offset is 0).
    if k == total_slots:
        if smin < reserve:
            return {'business_status': 'infeasible', 'reason': 'no_safe_schedule',
                    'currency': snap['currency'], 'prefix': prefix_block}
        # The worst bill is the maximum over histories; tie -> lexicographically
        # smallest prefix action list.
        best = None
        for (peak, _last), (cost, energy, wear, packed, soc) in classes.items():
            total = fixed + cost + demand_rate * peak
            if best is None or total > best[0] or \
                    (total == best[0] and packed < best[1]):
                best = (total, packed, peak, cost, energy, wear, soc)
        total, packed, peak, cost, energy, wear, soc = best
        prefix_actions = _unpack(packed, k, base, offset)
        return _feasible_report(
            snap, prefix_block, [], prefix_actions, energy, wear, cost, peak,
            0, 0, total, smin, smax)

    lo = -smin
    hi = capacity - smax
    need = reserve - smin
    terminals = _suffix_terminals(snap, charges, k, lo, hi, need)
    if terminals is None:
        return {'business_status': 'infeasible', 'reason': 'no_safe_schedule',
                'currency': snap['currency'], 'prefix': prefix_block}

    # Joint choice.  For one candidate suffix the worst bill is the maximum
    # over prefix classes (each class contributes its maximum prefix cost
    # witness).  The suffix objective is then (worst total, suffix sum_abs,
    # suffix action list); a remaining tie takes the lexicographically
    # smallest prefix witness of the worst history.
    best = None
    for (first, suffix_peak), (suffix_cost, suffix_abs, suffix_packed) in terminals.items():
        worst = None
        for (peak, last), (cost, energy, wear, packed, soc) in classes.items():
            combined = peak
            boundary = last + first
            if boundary > combined:
                combined = boundary
            if suffix_peak > combined:
                combined = suffix_peak
            total = fixed + cost + suffix_cost + demand_rate * combined
            if worst is None or total > worst[0] or \
                    (total == worst[0] and packed < worst[1]):
                worst = (total, packed, peak, cost, energy, wear, soc, combined)
        total, packed, peak, cost, energy, wear, soc, combined = worst
        rank = ((total, suffix_abs, suffix_packed), packed)
        if best is None or rank < best[0]:
            best = (rank, combined, peak, cost, energy, wear, packed, soc,
                    suffix_cost, suffix_abs, suffix_packed)

    (rank, combined, peak, cost, energy, wear, packed, soc,
     suffix_cost, suffix_abs, suffix_packed) = best
    total = rank[0][0]
    suffix_actions = _unpack(suffix_packed, total_slots - k, base, offset)
    prefix_actions = _unpack(packed, k, base, offset)
    suffix_wear = wear_rate * suffix_abs
    suffix_energy = suffix_cost - suffix_wear
    return _feasible_report(
        snap, prefix_block, suffix_actions, prefix_actions, energy, wear, cost,
        combined, suffix_energy, suffix_wear, total, smin, smax)
