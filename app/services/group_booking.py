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
                         status='confirmed', force_group=True,
                         adults=None, children=None, now=None,
                         payment_method=None):
    """Create a booking (or group booking) atomically.

    items: list of {'room_type_id': int, 'qty': int}
    lead_guest: an existing Guest instance OR a dict of guest fields to create.
    force_group: when False AND exactly one room is booked, produce a PLAIN
      booking (no BookingGroup / master-folio overhead) per spec §B3 intent.
    payment_method: optional 'cash' | 'bank_transfer' | … (cashiering vocabulary),
      stamped on every booking so finance (Alfred) can split cash vs transfer and
      the bot can pick the cash-received-vs-slip alert. None leaves it unset.
    Returns {'ok', 'group_id', 'booking_ids', 'reasons'} (group_id None if plain).
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
    make_group = force_group or sum(i['qty'] for i in norm) > 1

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
            # Nationality REQUIRED for a NEW guest created via the shared layer
            # (e.g. the Pepper internal API). Existing Guest instances (admin
            # confirm paths) are not re-validated — their record already exists.
            if not (lead_guest.get('nationality') or '').strip():
                db.session.rollback()
                return {'ok': False, 'group_id': None, 'booking_ids': [],
                        'reasons': ['nationality is required.']}
            guest = Guest(**{k: lead_guest.get(k) for k in
                             ('first_name', 'last_name', 'email', 'phone',
                              'id_type', 'id_number', 'nationality')
                             if k in lead_guest})
            db.session.add(guest)
            db.session.flush()

        # 4. Group (only when multi-room or forced).
        group = None
        if make_group:
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
                    total_amount=price['total'], status=status,
                    booking_group_id=(group.id if group else None),
                    billing_target=('master' if group else 'individual'),
                    created_by=created_by,
                    payment_method=payment_method,
                )
                db.session.add(b)
                db.session.flush()
                booking_ids.append(b.id)

        if group:
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
        else:
            log_activity('booking.created', booking_id=booking_ids[0],
                         actor_user_id=created_by, new_value=status,
                         description=(f'Booking created ({check_in}..{check_out}, '
                                      f'plain — single room).'),
                         metadata={'booking_id': booking_ids[0]})

        # 6b. Guest counts (per-GROUP totals; per-room split deferred). Stored on
        # the group (multi) or the single booking, and on the (master) booking's
        # num_guests for folio-header display.
        #
        # Capacity/fee is checked UNCONDITIONALLY (Pepper Phase 0): every create
        # path — admin confirm AND the internal API — passes the same occupancy
        # backstop; no caller may skip it via adults=None. Null counts are
        # coalesced (adults→1, children→0), mirroring confirm_group's own
        # null-handling (holds.py), so a legacy hold with missing counts degrades
        # to a safe 1-guest booking rather than a 500 or a false rejection.
        adults = adults if adults is not None else 1
        children = children if children is not None else 0
        total = adults + children
        head = Booking.query.get(booking_ids[0])
        head.adults = adults
        head.children = children
        head.num_guests = total
        if group:
            group.adults = adults
            group.children = children

        # 6c. Extra-person fee — itemized folio line(s) on the master/single
        # booking (NOT folded into the room rate). Recomputed here from the
        # same occupancy math the portal showed the guest. Backstop capacity
        # check in case config changed between hold and confirm.
        from . import occupancy
        from ..models import FolioItem
        fee_nights = max(1, (check_out - check_in).days)
        occ = occupancy.compute(norm, total, fee_nights)
        if occ['over_capacity']:
            db.session.rollback()
            return {'ok': False, 'group_id': None, 'booking_ids': [],
                    'reasons': [occupancy.block_message(
                        total, occ['max_per_room'], occ['min_rooms'])]}
        for tier in occ['breakdown']:
            amt = round(tier['persons'] * tier['nights'] * tier['fee'], 2)
            db.session.add(FolioItem(
                booking_id=head.id, guest_id=guest.id,
                property_id=head.property_id, item_type='fee',
                description=(f"Extra person fee ({tier['persons']} guest"
                             f" × {tier['nights']} night @ MVR "
                             f"{tier['fee']:.0f}/night)"),
                quantity=tier['persons'] * tier['nights'],
                unit_price=tier['fee'], amount=amt,
                tax_amount=0.0, service_charge_amount=0.0,
                total_amount=amt, status='open',
                source_module='booking_engine'))

        # 7. Commit everything atomically (releases the type locks).
        db.session.commit()
        return {'ok': True, 'group_id': (group.id if group else None),
                'booking_ids': booking_ids, 'reasons': []}

    except Exception as exc:               # noqa: BLE001 — atomic rollback contract
        db.session.rollback()
        return {'ok': False, 'group_id': None, 'booking_ids': [],
                'reasons': [f'unexpected error, rolled back: {type(exc).__name__}']}
