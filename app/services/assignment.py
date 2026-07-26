"""Booking Engine V2 — auto-assignment (spec §5).

Best-fit: at confirmation, pick the physical room of the type whose calendar
gaps most TIGHTLY fit the stay, minimizing fragmentation of the remaining
inventory. Never a mid-stay room change (contiguity respected — one whole
room for the whole stay). Deterministic tie-break by room id. Staff drag on
the tape chart remains authoritative; this only proposes.
"""

from __future__ import annotations

from datetime import date, timedelta

from . import inventory

_BIG = 10 ** 6   # "no neighbouring booking" gap sentinel


def _neighbour_gaps(room, check_in, check_out):
    """Days of free space immediately before check_in and after check_out on
    this room, bounded by the nearest holding booking (or _BIG if none).
    Tighter (smaller) gaps => the stay packs against existing bookings, leaving
    larger contiguous ranges elsewhere."""
    from ..models import Booking

    holding = inventory._HOLDING_STATUSES
    # nearest booking ending on/before check_in
    prev = (Booking.query
            .filter(Booking.room_id == room.id,
                    Booking.status.in_(holding),
                    Booking.check_out_date <= check_in)
            .order_by(Booking.check_out_date.desc())
            .first())
    # nearest booking starting on/after check_out
    nxt = (Booking.query
           .filter(Booking.room_id == room.id,
                   Booking.status.in_(holding),
                   Booking.check_in_date >= check_out)
           .order_by(Booking.check_in_date.asc())
           .first())
    gap_before = (check_in - prev.check_out_date).days if prev else _BIG
    gap_after = (nxt.check_in_date - check_out).days if nxt else _BIG
    return gap_before, gap_after


def best_fit_room(room_type_id, check_in, check_out, *, exclude_room_ids=None):
    """Return the best-fit Room for the stay, or None if the type has no whole
    contiguous room free. exclude_room_ids lets a multi-room group avoid
    re-picking a room within the same transaction."""
    exclude = set(exclude_room_ids or ())
    candidates = [r for r in inventory.free_rooms_for_stay(
        room_type_id, check_in, check_out) if r.id not in exclude]
    if not candidates:
        return None

    def score(room):
        gb, ga = _neighbour_gaps(room, check_in, check_out)
        # minimise total surrounding slack; tie-break deterministically by id
        return (gb + ga, room.id)

    return min(candidates, key=score)


def assign_room(booking, room_id, *, user_id=None, reason='auto', now=None):
    """Set booking.room_id and audit. Used by confirmation (auto) and any
    programmatic reassignment. Tape-chart drag has its own audited path."""
    from ..models import db, Room
    from .audit import log_activity
    old_room = booking.room_id
    booking.room_id = room_id
    room = Room.query.get(room_id)
    log_activity('booking.room_assigned',
                 booking=booking, actor_user_id=user_id,
                 old_value=str(old_room) if old_room else None,
                 new_value=str(room_id),
                 description=(f'Booking {booking.booking_ref} assigned to room '
                              f'{room.number if room else room_id} ({reason}).'),
                 metadata={'room_id': room_id, 'reason': reason,
                           'room_number': room.number if room else None})
    # caller commits
    return booking
