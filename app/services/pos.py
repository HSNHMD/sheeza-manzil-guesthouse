"""POS / F&B V1 — sale processing on top of Folio + Cashiering.

Hard contract:

  - POS is NOT a separate accounting truth. Every sale becomes one or
    more `FolioItem` rows. "Pay now" sales also produce a closing
    payment FolioItem (signed-negative `total_amount`) cross-linked to
    a `CashierTransaction`, mirroring the cashiering V1 pattern.

  - V1 REQUIRES a booking. Walk-in pay-now (no booking) is deferred:
    `FolioItem.booking_id` is NOT NULL on the schema, so a clean
    folio-tracked walk-in path needs a small Phase 2 schema change
    or a synthetic "house" booking. Per the spec we don't fake either.

  - Posting refuses if the booking is checked-out, cancelled, or
    rejected. Pre-arrival statuses are allowed (a guest who is on the
    way can still order from the bar). Operator picks the booking
    explicitly — no silent guessing.

  - Per-line FolioItem.item_type is taken from the corresponding
    PosItem.default_item_type, which is whitelisted against
    services.folio.ITEM_TYPES on save (so a typo can never produce
    invalid folio rows).

  - source_module='pos' is stamped on every folio item the sale
    produces. Reports, audits, and folio drill-downs can filter on
    this to show "POS-originated charges only".

  - ActivityLog actions:
        pos.cart_submitted        (one per submission, both modes)
        pos.item_posted_to_folio  (one per cart line, on success)
        pos.sale_paid             (one extra row when mode='pay_now')
        pos.sale_failed           (when validation/permission rejects)
"""

from __future__ import annotations

import json as _json
from typing import Optional

from .folio import ITEM_TYPES as FOLIO_ITEM_TYPES
from .cashiering import (
    normalize_payment_method, validate_payment_input,
    transaction_label,
)


# ── POS-allowed folio item types ────────────────────────────────────
#
# The full folio vocabulary is large (it includes 'discount', 'payment',
# 'adjustment', etc.) — POS items must NOT be any of those. A POS sale
# only ever creates positive charges; the payment leg is created
# separately for pay-now flows.
POS_FOLIO_ITEM_TYPES = (
    'restaurant', 'goods', 'service', 'fee',
    'laundry', 'transfer', 'excursion',
)

# Booking statuses where a POS sale must be REFUSED. Everything else
# is allowed (including pre-arrival statuses).
_BLOCKED_BOOKING_STATUSES = ('checked_out', 'cancelled', 'rejected')


# ── Validation ──────────────────────────────────────────────────────

def normalize_pos_item_type(raw) -> Optional[str]:
    if not raw:
        return None
    norm = str(raw).strip().lower()
    return norm if norm in POS_FOLIO_ITEM_TYPES else None


def validate_cart(cart) -> dict:
    """Validate a cart payload. Returns {'errors': [...], 'cleaned': [...]}.

    `cart` is an iterable of dicts:
        {'pos_item_id': int, 'qty': float|str,
         'price_override': float|str|None, 'note': str|None}
    """
    from ..models import PosItem

    errors = []
    cleaned = []
    if not cart:
        return {'errors': ['cart is empty.'], 'cleaned': []}

    for i, line in enumerate(cart, start=1):
        try:
            pos_item_id = int(line.get('pos_item_id'))
        except (TypeError, ValueError):
            errors.append(f'line {i}: pos_item_id required.')
            continue
        item = PosItem.query.get(pos_item_id)
        if item is None or not item.is_active:
            errors.append(f'line {i}: item {pos_item_id} not found or inactive.')
            continue
        try:
            qty = float(line.get('qty', 1))
        except (TypeError, ValueError):
            errors.append(f'line {i}: qty must be a number.')
            continue
        if qty <= 0:
            errors.append(f'line {i}: qty must be > 0.')
            continue
        if qty > 99:
            errors.append(f'line {i}: qty cannot exceed 99 (V1 cap).')
            continue

        price = item.price
        po = line.get('price_override')
        if po not in (None, '', 'null'):
            try:
                price = float(po)
            except (TypeError, ValueError):
                errors.append(f'line {i}: price_override must be numeric.')
                continue
            if price < 0:
                errors.append(f'line {i}: price cannot be negative.')
                continue

        note = (line.get('note') or '').strip() or None
        if note and len(note) > 200:
            note = note[:200]

        cleaned.append({
            'pos_item_id': pos_item_id,
            'item':        item,
            'qty':         round(qty, 2),
            'price':       round(price, 2),
            'note':        note,
            'line_total':  round(qty * price, 2),
        })

    return {'errors': errors, 'cleaned': cleaned}


def cart_total(cleaned_cart) -> float:
    return round(sum(l['line_total'] for l in cleaned_cart), 2)


def can_post_to_booking(booking) -> Optional[str]:
    """Return None if the booking is a valid POS target, else an error
    string describing why it is not."""
    if booking is None:
        return 'booking not found.'
    status = (getattr(booking, 'status', '') or '').strip().lower()
    if status in _BLOCKED_BOOKING_STATUSES:
        return (
            f'cannot post POS charges to a {status.replace("_", " ")} '
            f'booking.'
        )
    return None


# ── Sale processing ────────────────────────────────────────────────

def post_sale(*,
        booking,
        cleaned_cart,
        mode: str,
        cashier_user,
        sale_note: Optional[str] = None,
        payment_method: Optional[str] = None,
        reference_number: Optional[str] = None,
) -> dict:
    """Create the folio + cashier rows for a POS sale. Atomic — caller
    must commit the session.

    mode:
        'room'    — leave the charges on the booking's folio, status='open'
        'pay_now' — also create a payment FolioItem + CashierTransaction
                    that immediately settles the sale's total.

    Returns:
        {ok, error, total, folio_item_ids[], cashier_txn_id|None}
    """
    from ..models import db, FolioItem, CashierTransaction
    from .audit import log_activity

    if mode not in ('room', 'pay_now'):
        return {'ok': False, 'error': f'invalid mode: {mode!r}',
                'total': 0.0, 'folio_item_ids': [],
                'cashier_txn_id': None}

    # Hard guard: cannot post to a closed booking
    err = can_post_to_booking(booking)
    if err:
        return {'ok': False, 'error': err, 'total': 0.0,
                'folio_item_ids': [], 'cashier_txn_id': None}

    if not cleaned_cart:
        return {'ok': False, 'error': 'cart is empty.',
                'total': 0.0, 'folio_item_ids': [],
                'cashier_txn_id': None}

    total = cart_total(cleaned_cart)
    if total <= 0:
        return {'ok': False, 'error': 'cart total must be > 0.',
                'total': 0.0, 'folio_item_ids': [],
                'cashier_txn_id': None}

    # ── For pay_now, validate payment up front ──
    if mode == 'pay_now':
        method = normalize_payment_method(payment_method)
        if method is None:
            return {'ok': False,
                    'error': 'payment_method required for pay_now.',
                    'total': total, 'folio_item_ids': [],
                    'cashier_txn_id': None}

    # ── Create one FolioItem per cart line ──
    folio_item_ids = []
    for line in cleaned_cart:
        item = line['item']
        item_type = normalize_pos_item_type(item.default_item_type) \
                    or 'restaurant'
        description = f'POS · {item.name}'
        if line['qty'] != 1.0:
            description += f' × {line["qty"]:.0f}' \
                if float(line['qty']).is_integer() else \
                f' × {line["qty"]:.2f}'
        if line['note']:
            description = (description + ' · ' + line['note'])[:255]

        fi = FolioItem(
            booking_id=booking.id,
            guest_id=booking.guest_id,
            item_type=item_type,
            description=description,
            quantity=line['qty'],
            unit_price=line['price'],
            amount=line['line_total'],
            tax_amount=0.0,
            service_charge_amount=0.0,
            total_amount=line['line_total'],
            status='open',
            source_module='pos',
            posted_by_user_id=getattr(cashier_user, 'id', None),
        )
        db.session.add(fi)
        db.session.flush()
        folio_item_ids.append(fi.id)

        log_activity(
            'pos.item_posted_to_folio',
            actor_user_id=getattr(cashier_user, 'id', None),
            booking=booking,
            description=(
                f'POS posted "{item.name}" × {line["qty"]} '
                f'to booking {booking.booking_ref}.'
            ),
            metadata={
                'booking_id':    booking.id,
                'booking_ref':   booking.booking_ref,
                'folio_item_id': fi.id,
                'pos_item_id':   item.id,
                'category_id':   item.category_id,
                'qty':           line['qty'],
                'unit_price':    line['price'],
                'line_total':    line['line_total'],
                'source_module': 'pos',
            },
        )

    cashier_txn_id = None
    if mode == 'pay_now':
        # Create the closing payment FolioItem (signed-negative) +
        # CashierTransaction, mirror cashiering V1.
        method = normalize_payment_method(payment_method)
        ref = (reference_number or '').strip() or None
        sale_note_clean = (sale_note or '').strip() or None

        pay_fi = FolioItem(
            booking_id=booking.id,
            guest_id=booking.guest_id,
            item_type='payment',
            description=(
                f'POS sale · {transaction_label(method)} payment'
                f'{" · " + ref if ref else ""}'
            )[:255],
            quantity=1.0,
            unit_price=total,
            amount=total,
            tax_amount=0.0,
            service_charge_amount=0.0,
            total_amount=-total,                # signed-negative
            status='open',
            source_module='pos',
            posted_by_user_id=getattr(cashier_user, 'id', None),
        )
        db.session.add(pay_fi)
        db.session.flush()

        txn = CashierTransaction(
            booking_id=booking.id,
            guest_id=booking.guest_id,
            folio_item_id=pay_fi.id,
            invoice_id=getattr(getattr(booking, 'invoice', None),
                                'id', None),
            amount=total,
            currency='MVR',
            payment_method=method,
            reference_number=ref,
            received_by_user_id=getattr(cashier_user, 'id', None),
            transaction_type='payment',
            status='posted',
            notes=(sale_note_clean[:500] if sale_note_clean else None),
        )
        db.session.add(txn)
        db.session.flush()

        # Cross-link
        pay_fi.metadata_json = _json.dumps({
            'cashier_transaction_id': txn.id,
            'payment_method':         method,
            'source':                 'pos',
        })
        cashier_txn_id = txn.id
        folio_item_ids.append(pay_fi.id)

        log_activity(
            'pos.sale_paid',
            actor_user_id=getattr(cashier_user, 'id', None),
            booking=booking,
            description=(
                f'POS sale paid: {total:.2f} {txn.currency} via '
                f'{transaction_label(method)} for booking '
                f'{booking.booking_ref}.'
            ),
            metadata={
                'booking_id':              booking.id,
                'booking_ref':             booking.booking_ref,
                'cashier_transaction_id':  txn.id,
                'payment_method':          method,
                'total_amount':            total,
                'source_module':           'pos',
                'reference_number_present': bool(ref),
            },
        )

    log_activity(
        'pos.cart_submitted',
        actor_user_id=getattr(cashier_user, 'id', None),
        booking=booking,
        description=(
            f'POS cart submitted ({mode}): {len(cleaned_cart)} '
            f'line{"s" if len(cleaned_cart) != 1 else ""}, '
            f'total {total:.2f}.'
        ),
        metadata={
            'booking_id':     booking.id,
            'booking_ref':    booking.booking_ref,
            'mode':           mode,
            'item_count':     len(cleaned_cart),
            'total_amount':   total,
            'source_module':  'pos',
        },
    )

    return {
        'ok': True,
        'error': None,
        'total': total,
        'folio_item_ids': folio_item_ids,
        'cashier_txn_id': cashier_txn_id,
    }
