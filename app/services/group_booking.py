"""Booking Engine V2 — group creation service (spec §1, D5).

The primitive Phase 2's public multi-room portal will call: create, in ONE
transaction, a BookingGroup + N Bookings across types + a designated master
folio + inventory consumption (the bookings themselves — no counters). Any
partial failure rolls the WHOLE thing back: no orphan group, no consumed
inventory, no half-assigned rooms.

Rooms are chosen best-fit at creation (this represents the confirmation path).
Room revenue stays per booking (Booking.total_amount); only ad-hoc folio extras
consolidate to the master (existing group/master-folio backbone).
"""

from __future__ import annotations

import random
import string
from datetime import datetime

from . import inventory, assignment, holds


def _gen(prefix, n=6):
    return prefix + ''.join(random.choices(string.ascii_uppercase + string.digits, k=n))


def _unique_booking_ref():
    from ..models import Booking
    while True:
        ref = _gen('BK')
        if not Booking.query.filter_by(booking_ref=ref).first():
            return ref


def _unique_group_code():
    from ..models import BookingGroup
    while True:
        code = _gen('GRP', 5)
        if not BookingGroup.query.filter_by(group_code=code).first():
            return code


def create_group_booking(items, check_in, check_out, *, lead_guest,
                         created_by=None, group_name=None, num_guests_each=1,
                         status='confirmed', now=None):
    """Create a group booking atomically.

    items: list of {'room_type_id': int, 'qty': int}
    lead_guest: an existing Guest instance OR a dict of guest fields to create.
    Returns {'ok', 'group_id', 'booking_ids', 'reasons'}.
    On any failure the transaction is rolled back entirely.
    """
    from ..models import db, BookingGroup, Booking, Guest
    from .audit import log_activity
    now = now or datetime.utcnow()

    norm = [{'room_type_id': int(i['room_type_id']), 'qty': int(i.get('qty', 1))}
            for i in items if int(i.get('qty', 1)) > 0]
    if not norm:
        return {'ok': False, 'group_id': None, 'booking_ids': [],
                'reasons': ['no room-type items supplied.']}

    try:
        # 1. LOCK every involved type (ascending id — deadlock-safe).
        holds.lock_types(db.session, [i['room_type_id'] for i in norm])

        # 2. RE-CHECK availability for each type/qty under the lock.
        reasons = []
        for i in norm:
            av = inventory.available_for_stay(i['room_type_id'], check_in,
                                              check_out, i['qty'], now=now)
            if not av['ok']:
                reasons.append(f"type {i['room_type_id']} x{i['qty']}: "
                               + '; '.join(av['reasons']))
        if reasons:
            db.session.rollback()
            return {'ok': False, 'group_id': None, 'booking_ids': [],
                    'reasons': reasons}

        # 3. Lead guest.
        if isinstance(lead_guest, Guest):
            guest = lead_guest
        else:
            guest = Guest(**{k: lead_guest.get(k) for k in
                             ('first_name', 'last_name', 'email', 'phone',
                              'id_type', 'id_number', 'nationality')
                             if k in lead_guest})
            db.session.add(guest)
            db.session.flush()

        # 4. Group.
        group = BookingGroup(
            group_code=_unique_group_code(),
            group_name=(group_name or f'Group {guest.first_name or ""}'.strip())[:160],
            primary_contact_guest_id=guest.id,
            billing_mode='master', status='active',
        )
        db.session.add(group)
        db.session.flush()

        # 5. One Booking per room, best-fit assigned (avoiding re-pick).
        booking_ids = []
        picked_room_ids = []
        for i in norm:
            price = inventory.price_stay(i['room_type_id'], check_in, check_out)
            for _ in range(i['qty']):
                room = assignment.best_fit_room(
                    i['room_type_id'], check_in, check_out,
                    exclude_room_ids=picked_room_ids)
                if room is None:
                    # Should not happen (re-check passed), but be safe: abort all.
                    db.session.rollback()
                    return {'ok': False, 'group_id': None, 'booking_ids': [],
                            'reasons': ['ran out of contiguous rooms mid-assign '
                                        '(concurrent change) — rolled back.']}
                picked_room_ids.append(room.id)
                b = Booking(
                    booking_ref=_unique_booking_ref(),
                    room_id=room.id, guest_id=guest.id,
                    check_in_date=check_in, check_out_date=check_out,
                    num_guests=num_guests_each,
                    total_amount=price['total'],
                    status=status, booking_group_id=group.id,
                    billing_target='master', created_by=created_by,
                )
                db.session.add(b)
                db.session.flush()
                booking_ids.append(b.id)

        # 6. Master folio = first booking; ad-hoc extras consolidate there.
        group.master_booking_id = booking_ids[0]

        log_activity('group.created', actor_user_id=created_by,
                     description=(f'Group {group.group_code}: {len(booking_ids)} '
                                  f'booking(s) across {len(norm)} type(s), '
                                  f'{check_in}..{check_out}.'),
                     metadata={'group_code': group.group_code,
                               'booking_count': len(booking_ids),
                               'type_count': len(norm),
                               'master_booking_id': booking_ids[0]})

        # 7. Commit everything atomically (releases the type locks).
        db.session.commit()
        return {'ok': True, 'group_id': group.id, 'booking_ids': booking_ids,
                'reasons': []}

    except Exception as exc:               # noqa: BLE001 — atomic rollback contract
        db.session.rollback()
        return {'ok': False, 'group_id': None, 'booking_ids': [],
                'reasons': [f'unexpected error, rolled back: {type(exc).__name__}']}
