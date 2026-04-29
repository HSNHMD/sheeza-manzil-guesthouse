"""Online Menu / QR Ordering V1 — service layer.

Pure functions used by app/routes/menu_orders.py:

  - public_menu_payload()   — list of active categories + items shown
                              to guests (mirrors the POS terminal
                              source so the same catalog drives both).
  - validate_cart_input()   — validates the inbound JSON cart shape
                              from the public form.
  - match_booking()         — looks up an active in-house booking by
                              room number + last name. Returns None
                              if the combo doesn't match any in-house
                              row. Failure is silent; the order is
                              still recorded with the typed strings.
  - create_order()          — atomic create of GuestOrder + items.
  - confirm_order / deliver / cancel — staff lifecycle.
  - post_to_folio()         — staff-explicit; creates FolioItem rows
                              on the linked booking using
                              source_module='online_menu'. Refuses if
                              the order has no booking link.

Hard rules:

  - V1 NEVER auto-posts to folio. Only `post_to_folio()` does it, and
    it only runs when staff clicks the explicit "Post to room" button
    on the admin queue.
  - V1 has no online payment. Orders end at status='delivered' (or
    'cancelled'); settlement happens via the existing booking folio
    or POS pay-now flow.
  - source_module='online_menu' is stamped on every FolioItem the
    `post_to_folio()` action produces, so reports and audits can
    isolate menu-originated revenue from terminal-originated revenue.

  - ActivityLog actions:
        guest_order.created
        guest_order.confirmed
        guest_order.delivered
        guest_order.cancelled
        guest_order.posted_to_folio
"""

from __future__ import annotations

import secrets
from datetime import date, datetime
from typing import Optional

from .folio import ITEM_TYPES as FOLIO_ITEM_TYPES


# Subset of folio item types the menu orders are allowed to post.
# Same whitelist as POS V1 — discount / payment / adjustment never
# come from a guest order.
MENU_FOLIO_ITEM_TYPES = (
    'restaurant', 'goods', 'service', 'fee',
    'laundry', 'transfer', 'excursion',
)

# Booking statuses the menu order will attach to.
_LIVE_STATUSES = ('checked_in', 'confirmed', 'payment_verified',
                  'payment_uploaded')

# Status enum.
ORDER_STATUSES = ('new', 'confirmed', 'delivered', 'cancelled')


# ── Catalog payload ─────────────────────────────────────────────────

def public_menu_payload():
    """Return {'categories': [...], 'items': [...]} for the public menu.

    Only active categories + items are surfaced.
    """
    from ..models import PosCategory, PosItem

    cats = (PosCategory.query
            .filter_by(is_active=True)
            .order_by(PosCategory.sort_order, PosCategory.name)
            .all())
    items = (PosItem.query
             .filter_by(is_active=True)
             .order_by(PosItem.category_id, PosItem.sort_order, PosItem.name)
             .all())
    return {'categories': cats, 'items': items}


# ── Validation ──────────────────────────────────────────────────────

def validate_cart_input(cart) -> dict:
    """Validate the JSON cart payload from the public form.

    Same shape as POS:
        [{'pos_item_id': int, 'qty': float, 'note': str?}, ...]

    Returns {'errors': [...], 'cleaned': [...]} where each cleaned line
    has the snapshotted name + price + line_total.
    """
    from ..models import PosItem

    errors = []
    cleaned = []
    if not cart:
        return {'errors': ['cart is empty.'], 'cleaned': []}
    if not isinstance(cart, list):
        return {'errors': ['cart must be a list.'], 'cleaned': []}

    for i, line in enumerate(cart, start=1):
        if not isinstance(line, dict):
            errors.append(f'line {i}: invalid shape.')
            continue
        try:
            pos_item_id = int(line.get('pos_item_id'))
        except (TypeError, ValueError):
            errors.append(f'line {i}: pos_item_id required.')
            continue
        item = PosItem.query.get(pos_item_id)
        if item is None or not item.is_active:
            errors.append(f'line {i}: item {pos_item_id} not found '
                          f'or inactive.')
            continue
        try:
            qty = float(line.get('qty', 1))
        except (TypeError, ValueError):
            errors.append(f'line {i}: qty must be a number.')
            continue
        if qty <= 0:
            errors.append(f'line {i}: qty must be > 0.')
            continue
        if qty > 20:
            # Guests aren't likely to order 20× anything; cap defensively.
            errors.append(f'line {i}: qty cannot exceed 20 (V1 cap).')
            continue

        note = (line.get('note') or '').strip() or None
        if note and len(note) > 200:
            note = note[:200]

        item_type = item.default_item_type
        if item_type not in MENU_FOLIO_ITEM_TYPES:
            # Should never happen — POS validation prevents it — but
            # we double-check at submit time.
            item_type = 'restaurant'

        cleaned.append({
            'pos_item_id':    pos_item_id,
            'item':           item,
            'name_snapshot':  item.name,
            'item_type':      item_type,
            'unit_price':     round(item.price, 2),
            'qty':            round(qty, 2),
            'line_total':     round(qty * item.price, 2),
            'note':           note,
        })

    return {'errors': errors, 'cleaned': cleaned}


def cart_total(cleaned_cart) -> float:
    return round(sum(l['line_total'] for l in cleaned_cart), 2)


# ── Booking match ───────────────────────────────────────────────────

def match_booking(room_number: Optional[str],
                  guest_name: Optional[str]):
    """Best-effort attach: returns a Booking iff a single in-house row
    matches BOTH room number AND a case-insensitive last-name substring.

    Silent: returns None on no-match, multi-match, or empty input. The
    order is still recorded with the typed strings so staff can resolve
    by hand.
    """
    from ..models import Booking, Guest, Room

    if not room_number or not guest_name:
        return None
    rn = str(room_number).strip()
    gn = str(guest_name).strip().lower()
    if not rn or not gn:
        return None

    today = date.today()
    rows = (Booking.query
            .join(Room, Booking.room_id == Room.id)
            .join(Guest, Booking.guest_id == Guest.id)
            .filter(Room.number == rn)
            .filter(Booking.status.in_(_LIVE_STATUSES))
            .filter(Booking.check_in_date <= today)
            .filter(Booking.check_out_date >= today)
            .all())

    matches = [
        b for b in rows
        if (b.guest is not None
            and gn in (b.guest.last_name or '').lower())
    ]
    if len(matches) == 1:
        return matches[0]
    return None


# ── Create ──────────────────────────────────────────────────────────

def _new_token() -> str:
    return secrets.token_urlsafe(16)


def create_order(*,
        cleaned_cart,
        room_number: Optional[str] = None,
        guest_name: Optional[str] = None,
        contact_phone: Optional[str] = None,
        notes: Optional[str] = None,
        source: str = 'guest_menu') -> dict:
    """Create the GuestOrder + child rows. Caller commits.

    Returns:
      {ok, error, order, booking, total}.
    """
    from ..models import db, GuestOrder, GuestOrderItem
    from .audit import log_activity

    if not cleaned_cart:
        return {'ok': False, 'error': 'cart is empty.',
                'order': None, 'booking': None, 'total': 0.0}
    total = cart_total(cleaned_cart)
    if total <= 0:
        return {'ok': False, 'error': 'cart total must be > 0.',
                'order': None, 'booking': None, 'total': 0.0}

    booking = match_booking(room_number, guest_name)

    order = GuestOrder(
        public_token=_new_token(),
        booking_id=(booking.id if booking else None),
        room_number_input=(str(room_number).strip()[:20]
                           if room_number else None),
        guest_name_input=(str(guest_name).strip()[:120]
                          if guest_name else None),
        contact_phone=(str(contact_phone).strip()[:40]
                       if contact_phone else None),
        notes=(str(notes).strip()[:500] if notes else None),
        status='new',
        total_amount=total,
        source=(source if source in ('guest_menu', 'qr_menu') else 'guest_menu'),
    )
    db.session.add(order)
    db.session.flush()

    for line in cleaned_cart:
        db.session.add(GuestOrderItem(
            order_id=order.id,
            pos_item_id=line['pos_item_id'],
            item_name_snapshot=line['name_snapshot'][:120],
            item_type_snapshot=line['item_type'][:30],
            unit_price=line['unit_price'],
            quantity=line['qty'],
            line_total=line['line_total'],
            note=line['note'],
        ))

    log_activity(
        'guest_order.created',
        actor_type='guest',
        booking=booking,
        description=(
            f'Guest order #{order.id} submitted '
            f'({len(cleaned_cart)} item'
            f'{"s" if len(cleaned_cart) != 1 else ""}, '
            f'total {total:.2f}'
            f'{", linked to booking " + booking.booking_ref if booking else ", unlinked"}).'
        ),
        metadata={
            'order_id':     order.id,
            'booking_id':   booking.id if booking else None,
            'booking_ref':  booking.booking_ref if booking else None,
            'room_number':  order.room_number_input,
            'item_count':   len(cleaned_cart),
            'total_amount': total,
            'source':       order.source,
        },
        capture_request=False,
    )

    return {'ok': True, 'error': None, 'order': order,
            'booking': booking, 'total': total}


# ── Lifecycle transitions ───────────────────────────────────────────

def _transition(order, new_status, *, user, **stamps):
    from .audit import log_activity

    valid_transitions = {
        'new':       {'confirmed', 'cancelled'},
        'confirmed': {'delivered', 'cancelled'},
        'delivered': set(),     # terminal
        'cancelled': set(),     # terminal
    }
    if new_status not in valid_transitions.get(order.status, set()):
        return {'ok': False,
                'error': f'cannot transition from {order.status!r} '
                         f'to {new_status!r}.'}

    order.status = new_status
    now = datetime.utcnow()
    if new_status == 'confirmed':
        order.confirmed_at = now
        order.confirmed_by_user_id = getattr(user, 'id', None)
    elif new_status == 'delivered':
        order.delivered_at = now
        order.delivered_by_user_id = getattr(user, 'id', None)
    elif new_status == 'cancelled':
        order.cancelled_at = now
        order.cancelled_by_user_id = getattr(user, 'id', None)
        order.cancel_reason = (stamps.get('cancel_reason') or '')[:255] or None

    log_activity(
        f'guest_order.{new_status}',
        actor_user_id=getattr(user, 'id', None),
        booking=order.booking,
        description=(
            f'Guest order #{order.id} {new_status} by '
            f'{getattr(user, "username", "system")}.'
        ),
        metadata={
            'order_id':    order.id,
            'booking_id':  order.booking_id,
            'booking_ref': order.booking.booking_ref if order.booking else None,
            'item_count':  order.items.count(),
            'total_amount': order.total_amount,
            'source':       order.source,
        },
    )
    return {'ok': True, 'error': None}


def confirm_order(order, *, user):
    return _transition(order, 'confirmed', user=user)


def deliver_order(order, *, user):
    return _transition(order, 'delivered', user=user)


def cancel_order(order, *, user, reason: Optional[str] = None):
    return _transition(order, 'cancelled', user=user,
                        cancel_reason=reason)


# ── Folio post (staff-explicit) ─────────────────────────────────────

def post_to_folio(order, *, user) -> dict:
    """Create FolioItem rows on the linked booking. Caller commits.

    Refuses if:
      - order has no booking link
      - linked booking is checked_out / cancelled / rejected
      - order is already posted
      - order is cancelled
    """
    from ..models import db, FolioItem, Booking
    from .audit import log_activity
    from .pos import can_post_to_booking

    if order.is_posted_to_folio:
        return {'ok': False, 'error': 'order already posted to folio.'}
    if order.status == 'cancelled':
        return {'ok': False, 'error': 'cannot post a cancelled order.'}
    if order.booking_id is None:
        return {'ok': False,
                'error': 'order has no booking link — staff must '
                         'attach a booking first.'}

    booking = Booking.query.get(order.booking_id)
    err = can_post_to_booking(booking)
    if err:
        return {'ok': False, 'error': err}

    item_ids = []
    for it in order.items:
        item_type = it.item_type_snapshot
        if item_type not in MENU_FOLIO_ITEM_TYPES:
            item_type = 'restaurant'
        description = (
            f'Online menu · {it.item_name_snapshot}'
        )
        if it.quantity != 1.0:
            description += (f' × {it.quantity:.0f}'
                            if float(it.quantity).is_integer()
                            else f' × {it.quantity:.2f}')
        if it.note:
            description = (description + ' · ' + it.note)[:255]

        fi = FolioItem(
            booking_id=booking.id,
            guest_id=booking.guest_id,
            item_type=item_type,
            description=description,
            quantity=it.quantity,
            unit_price=it.unit_price,
            amount=it.line_total,
            tax_amount=0.0,
            service_charge_amount=0.0,
            total_amount=it.line_total,
            status='open',
            source_module='online_menu',
            posted_by_user_id=getattr(user, 'id', None),
        )
        db.session.add(fi)
        db.session.flush()
        item_ids.append(fi.id)

    order.posted_to_folio_at = datetime.utcnow()
    order.posted_by_user_id = getattr(user, 'id', None)
    order.folio_item_ids = ','.join(str(i) for i in item_ids)[:255]

    log_activity(
        'guest_order.posted_to_folio',
        actor_user_id=getattr(user, 'id', None),
        booking=booking,
        description=(
            f'Guest order #{order.id} posted to folio of booking '
            f'{booking.booking_ref}.'
        ),
        metadata={
            'order_id':         order.id,
            'booking_id':       booking.id,
            'booking_ref':      booking.booking_ref,
            'item_count':       len(item_ids),
            'total_amount':     order.total_amount,
            'first_folio_item': item_ids[0] if item_ids else None,
            'source':           'online_menu',
        },
    )
    return {'ok': True, 'error': None,
            'folio_item_ids': item_ids,
            'total': order.total_amount}
