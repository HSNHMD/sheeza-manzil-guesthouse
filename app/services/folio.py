"""Guest Folio (V1) — pure helpers + balance calculations.

V1 contract (binding):
- Folio is ADDITIVE for extras only. It does NOT replace the existing
  Invoice/Booking accounting flow. Booking.total_amount remains the
  source of truth for room revenue. Folio represents laundry,
  restaurant, transfer, fees, discounts, payments, and adjustments —
  everything BUT the room nights themselves.
- total_amount on a FolioItem is stored SIGNED. Charges are positive,
  payments and discounts are negative. Sign is applied at post time
  (in the route handler) based on item_type.
- Voided items are excluded from balance calculations. Voiding is
  the only way to "undo" a posted item — there is no DELETE endpoint.

This module is intentionally pure (no Flask context required) so it
can be unit-tested without spinning up an app.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping, Optional


# ── Whitelisted enums ────────────────────────────────────────────────

# Item types are split by accounting class. The display order below is
# the order shown in the admin dropdown.
ITEM_TYPES = (
    'room_charge',
    'restaurant',
    'laundry',
    'transfer',
    'excursion',
    'goods',
    'service',
    'fee',
    'damage',
    'discount',
    'payment',
    'adjustment',
    'other',
)

ITEM_TYPE_LABELS = {
    'room_charge': 'Room charge',
    'restaurant':  'Restaurant',
    'laundry':     'Laundry',
    'transfer':    'Transfer (boat/flight)',
    'excursion':   'Excursion',
    'goods':       'Goods (minibar/shop)',
    'service':     'Service (extra)',
    'fee':         'Fee (late checkout, etc.)',
    'damage':      'Damage',
    'discount':    'Discount',
    'payment':     'Payment received',
    'adjustment':  'Manual adjustment',
    'other':       'Other',
}

# Item types whose total_amount must be stored as a NEGATIVE number
# (they reduce the folio balance). The route handler applies the sign
# based on this set so the form can always accept positive amounts.
NEGATIVE_ITEM_TYPES = frozenset(('discount', 'payment'))

# Item types where the admin may enter a signed amount (positive or
# negative). adjustment is the canonical example — useful for both
# corrections and credits.
SIGNED_ITEM_TYPES = frozenset(('adjustment', 'other'))

# Item types that always increase the balance.
POSITIVE_ITEM_TYPES = frozenset(set(ITEM_TYPES) - NEGATIVE_ITEM_TYPES
                                - SIGNED_ITEM_TYPES)

STATUSES = ('open', 'invoiced', 'paid', 'voided')

SOURCE_MODULES = ('manual', 'booking', 'accounting', 'pos', 'system')

# Active (non-voided) statuses — included in folio balance.
ACTIVE_STATUSES = frozenset(('open', 'invoiced', 'paid'))


# ── Validation / normalization ───────────────────────────────────────

def normalize_folio_item_type(item_type: Optional[str]) -> Optional[str]:
    """Return the lowercased, validated item_type, or None if invalid.

    Pure function — never raises.
    """
    if not item_type:
        return None
    norm = str(item_type).strip().lower()
    return norm if norm in ITEM_TYPES else None


def display_folio_item_label(item_type: Optional[str]) -> str:
    """Return the human-friendly label for a stored item_type, or a
    fallback if the type is unknown."""
    norm = normalize_folio_item_type(item_type)
    if norm is None:
        return str(item_type or 'unknown')
    return ITEM_TYPE_LABELS[norm]


def _safe_float(value, default=None):
    """Parse to float without ValueError. Empty / None → default."""
    if value is None or value == '':
        return default
    try:
        # Decimal first to reject "1.2.3" reliably; convert to float for
        # column compatibility.
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return None


def validate_folio_item(*,
                        item_type,
                        description,
                        quantity,
                        unit_price,
                        tax_amount=0,
                        service_charge_amount=0) -> dict:
    """Validate input from the admin add-folio-item form.

    Returns a dict ``{'errors': [...], 'cleaned': {...}}``. The cleaned
    dict (when errors is empty) has typed values: quantity/prices are
    floats; description is stripped; item_type is normalized.

    Pure function — no DB access, no Flask context.
    """
    errors = []
    cleaned = {}

    # item_type
    norm_type = normalize_folio_item_type(item_type)
    if norm_type is None:
        errors.append('item_type is required and must be a valid type')
    cleaned['item_type'] = norm_type

    # description
    desc = (description or '').strip()
    if not desc:
        errors.append('description is required')
    elif len(desc) > 255:
        errors.append('description must be 255 characters or fewer')
    cleaned['description'] = desc

    # quantity
    q = _safe_float(quantity, default=1.0)
    if q is None:
        errors.append('quantity must be a number')
    elif q <= 0:
        errors.append('quantity must be greater than zero')
    cleaned['quantity'] = q

    # unit_price (admin always enters POSITIVE; sign is applied later)
    up = _safe_float(unit_price, default=None)
    if up is None:
        errors.append('unit_price must be a number')
    elif up < 0 and norm_type not in SIGNED_ITEM_TYPES:
        # Negative unit_price only allowed for adjustment / other.
        errors.append('unit_price must be positive for this item type — '
                      'use a discount or adjustment to record a credit')
    cleaned['unit_price'] = up

    # tax_amount + service_charge_amount (always positive, default 0)
    ta = _safe_float(tax_amount, default=0.0)
    if ta is None or ta < 0:
        errors.append('tax_amount must be a non-negative number')
    cleaned['tax_amount'] = ta

    sca = _safe_float(service_charge_amount, default=0.0)
    if sca is None or sca < 0:
        errors.append('service_charge_amount must be a non-negative number')
    cleaned['service_charge_amount'] = sca

    return {'errors': errors, 'cleaned': cleaned}


# ── Sign application ────────────────────────────────────────────────

def signed_total(item_type: str,
                 amount: float,
                 tax_amount: float = 0.0,
                 service_charge_amount: float = 0.0) -> float:
    """Compute the SIGNED total_amount for a folio item.

    Rules:
      - Positive types: amount + tax + service_charge
      - Negative types (discount, payment): -(amount + tax + sc)
        (taxes/sc on a discount or payment are typically zero, but the
        helper keeps the math symmetric for completeness.)
      - Signed types (adjustment, other): amount keeps its sign;
        tax + sc are added with the same sign as amount.
    """
    base = (amount or 0.0) + (tax_amount or 0.0) + (service_charge_amount or 0.0)
    norm = normalize_folio_item_type(item_type) or item_type

    if norm in NEGATIVE_ITEM_TYPES:
        # Force negative — admin entered positive.
        return -abs(base)
    if norm in SIGNED_ITEM_TYPES:
        # Signed types: pass through with admin's sign (already on amount).
        # Tax + SC ride along with amount's sign so totals stay consistent.
        if (amount or 0.0) < 0:
            return -abs(base)
        return abs(base)
    # Positive types
    return abs(base)


# ── Query helpers ────────────────────────────────────────────────────

def get_open_folio_items(booking):
    """Return the booking's non-voided folio items, oldest first.

    Accepts a Booking ORM instance and returns a list (materializes the
    relationship so callers can iterate without DB round-trips).
    """
    if booking is None:
        return []
    items = []
    rel = getattr(booking, 'folio_items', None)
    if rel is None:
        return []
    iterable = rel.all() if hasattr(rel, 'all') else list(rel)
    for it in iterable:
        if it.status != 'voided':
            items.append(it)
    items.sort(key=lambda r: r.created_at)
    return items


def folio_balance(booking) -> float:
    """Return the booking's folio balance.

    balance = sum(item.total_amount for non-voided items)

    Voided items contribute zero. Negative totals (payments, discounts)
    naturally reduce the balance because of the signed-storage rule.
    """
    return round(sum(it.total_amount for it in get_open_folio_items(booking)),
                 2)


def calculate_folio_totals(booking) -> dict:
    """Return aggregate totals split by accounting class.

    Returns a dict with these keys:
        total_charges       — sum of positive items (charges, fees, services)
        total_credits       — sum of |total| for discounts + payments
                              (always reported as a positive number)
        total_adjustments   — net of signed-type rows (adjustment, other)
        balance             — total_charges - total_credits + total_adjustments
                              (equivalent to folio_balance(booking))
        item_count_open     — count of non-voided rows
        item_count_voided   — count of voided rows

    Pure aggregation — never raises on empty folio.
    """
    open_items = get_open_folio_items(booking)
    total_charges = 0.0
    total_credits = 0.0
    total_adjustments = 0.0

    for it in open_items:
        norm = normalize_folio_item_type(it.item_type) or 'other'
        if norm in NEGATIVE_ITEM_TYPES:
            total_credits += abs(it.total_amount)
        elif norm in SIGNED_ITEM_TYPES:
            total_adjustments += it.total_amount
        else:
            total_charges += it.total_amount

    voided_count = 0
    rel = getattr(booking, 'folio_items', None) if booking is not None else None
    if rel is not None:
        iterable = rel.all() if hasattr(rel, 'all') else list(rel)
        voided_count = sum(1 for it in iterable if it.status == 'voided')

    balance = total_charges - total_credits + total_adjustments

    return {
        'total_charges':     round(total_charges, 2),
        'total_credits':     round(total_credits, 2),
        'total_adjustments': round(total_adjustments, 2),
        'balance':           round(balance, 2),
        'item_count_open':   len(open_items),
        'item_count_voided': voided_count,
    }
