"""Executable examples of V2 quantity rules. This is not a live Teable posting service."""
from decimal import Decimal


def number(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError('Quantity cannot be boolean')
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError('Quantity must be finite and nonnegative (positive for movement)')
    return result


def to_base_quantity(quantity, conversion_snapshot, *, confirmed, whole_units=True):
    if not confirmed:
        raise ValueError('Packaging conversion has not been confirmed')
    result = number(quantity, positive=True) * number(conversion_snapshot, positive=True)
    if whole_units and result != result.to_integral_value():
        raise ValueError('Indivisible base unit cannot have fractional quantity')
    return result


def validate_allocations(quantity, sku, channel, allocations):
    total = Decimal(0)
    keys = set()
    for item in allocations:
        if item['id'] in keys:
            raise ValueError('Duplicate allocation identity')
        keys.add(item['id'])
        if item['sku'] != sku or item['channel'] != channel:
            raise ValueError('Allocation SKU/channel does not match handover document')
        if item.get('plan_order_line') and item['plan_order_line'] != item['order_line']:
            raise ValueError('Optional plan belongs to another order line')
        total += number(item['quantity'], positive=True)
    if total != number(quantity, positive=True):
        raise ValueError('Allocated total must equal physical handover quantity')


def summarize(ordered_quantity, movements, *, closed_quantity=0):
    """For a confirmed zero opening balance; examples omit migration and transactional storage."""
    ordered = number(ordered_quantity)
    closed = number(closed_quantity)
    gross = returned = credit = Decimal(0)
    effects, records, returns_by_pickup, reversed_ids = {}, {}, {}, set()
    unresolved_returns = set()
    for item in movements:
        key, kind = item['id'], item['kind']
        if key in records:
            raise ValueError('Posting identity already exists; retry must return the original result')
        qty = number(item['quantity'], positive=True)
        if kind == 'pickup':
            effect = (qty, Decimal(0), qty)
        elif kind == 'return':
            original = item['against']
            if original not in records or records[original]['kind'] != 'pickup' or original in reversed_ids:
                raise ValueError('Return requires an existing unreversed pickup allocation')
            cumulative = returns_by_pickup.get(original, Decimal(0)) + qty
            if cumulative > number(records[original]['quantity']):
                raise ValueError('Return exceeds original pickup allocation')
            returns_by_pickup[original] = cumulative
            policy = item['resolution']
            if policy not in {'reopen', 'no_reopen', 'pending'}:
                raise ValueError('Unknown return resolution')
            if policy == 'pending':
                unresolved_returns.add(key)
            effect = (Decimal(0), qty, -qty if policy == 'reopen' else Decimal(0))
        elif kind == 'reversal':
            original = item['against']
            if original not in records or original in reversed_ids or records[original]['kind'] == 'reversal':
                raise ValueError('Reversal must reference an unreversed posted original')
            if qty != number(records[original]['quantity']):
                raise ValueError('Example reversals reverse the full original allocation')
            if records[original]['kind'] == 'pickup' and returns_by_pickup.get(original, Decimal(0)):
                raise ValueError('Reverse dependent returns before reversing their original pickup')
            if records[original]['kind'] == 'return':
                parent = records[original]['against']
                returns_by_pickup[parent] -= qty
            effect = tuple(-n for n in effects[original])
            reversed_ids.add(original)
        else:
            raise ValueError('Unknown movement kind')
        records[key], effects[key] = item, effect
        gross += effect[0]
        returned += effect[1]
        credit += effect[2]
    if closed > ordered or gross < 0 or returned < 0 or gross - returned < 0:
        raise ValueError('Inconsistent physical or closed quantities')
    unresolved = bool(unresolved_returns - reversed_ids)
    remaining = ordered - closed - credit
    if not unresolved and remaining < 0:
        raise ValueError('Over-fulfillment or excessive short-close requires explicit handling')
    return {'ordered': ordered, 'closed': closed, 'gross_picked': gross,
            'returned': returned, 'net_picked': gross - returned,
            'fulfillment_credit': None if unresolved else credit,
            'to_pick': None if unresolved else remaining}
