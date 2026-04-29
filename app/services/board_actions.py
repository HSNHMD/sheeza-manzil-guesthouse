"""Board operational interactions — pure helpers.

These functions are kept separate from the route handlers so they can
be unit-tested without a Flask context. They never write to the DB on
their own — they validate and return either a structured result or an
error reason. The route handler is responsible for committing.

Three operations are supported:

  1. Move a booking to a different room.
  2. Update a booking's check-out date (extend or shorten).
  3. Create / remove a date-ranged room block (out-of-order period).

Each operation has a ``check_*_conflict`` helper that returns either
None (operation safe) or a short human-readable string explaining why
not. Routes surface that string via flash().
"""

from __future__ import annotations

from datetime import date
from typing import Optional


# Booking statuses that DO NOT consume a room (cancelled / rejected
# do not need to be checked for overlap).
_INACTIVE_BOOKING_STATUSES = frozenset(('cancelled', 'rejected'))

# Allowed reasons for a room block. Kept tight so the dropdown stays
# scannable; "other" is the catch-all.
ROOM_BLOCK_REASONS = (
    ('maintenance',    'Maintenance'),
    ('owner_hold',     'Owner hold'),
    ('deep_cleaning',  'Deep cleaning'),
    ('damage_repair',  'Damage repair'),
    ('other',          'Other'),
)


def parse_iso_date(value) -> Optional[date]:
    """Parse a YYYY-MM-DD string into a date, or None if invalid."""
    from datetime import datetime
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def overlaps(a_start: date, a_end: date,
             b_start: date, b_end: date) -> bool:
    """Half-open interval overlap. End dates are exclusive (matching the
    Booking.check_out_date / RoomBlock.end_date convention)."""
    if a_start is None or a_end is None or b_start is None or b_end is None:
        return False
    return a_start < b_end and a_end > b_start


# ── Conflict checks (route handlers call these before mutating) ───

def check_booking_room_move_conflict(*, target_room_id: int,
                                     check_in_date: date,
                                     check_out_date: date,
                                     exclude_booking_id: int) -> Optional[str]:
    """Return None if the booking can be moved to the target room
    without overlap, else a short reason string.

    Lazy imports to keep this module pure / Flask-context-free.
    """
    from ..models import Booking, RoomBlock

    # Other active bookings on the target room
    overlapping = (
        Booking.query
        .filter(
            Booking.room_id == target_room_id,
            Booking.id != exclude_booking_id,
            ~Booking.status.in_(_INACTIVE_BOOKING_STATUSES),
            Booking.check_in_date  < check_out_date,
            Booking.check_out_date > check_in_date,
        )
        .first()
    )
    if overlapping is not None:
        return (f'overlaps with booking {overlapping.booking_ref} '
                f'({overlapping.check_in_date} → {overlapping.check_out_date})')

    # Active room blocks
    block = (
        RoomBlock.query
        .filter(
            RoomBlock.room_id == target_room_id,
            RoomBlock.removed_at.is_(None),
            RoomBlock.start_date < check_out_date,
            RoomBlock.end_date   > check_in_date,
        )
        .first()
    )
    if block is not None:
        return (f'overlaps with active room block '
                f'({block.start_date} → {block.end_date}, {block.reason})')

    return None


def check_stay_update_conflict(*, room_id: int, booking_id: int,
                               new_check_in: date,
                               new_check_out: date) -> Optional[str]:
    """Return None if the new dates can apply to this booking without
    conflicting with other bookings or active blocks on the same room."""
    if new_check_in is None or new_check_out is None:
        return 'invalid date'
    if new_check_out <= new_check_in:
        return 'check-out must be after check-in'

    # The same logic as room move, just keyed on the existing room.
    return check_booking_room_move_conflict(
        target_room_id=room_id,
        check_in_date=new_check_in,
        check_out_date=new_check_out,
        exclude_booking_id=booking_id,
    )


def check_room_block_conflict(*, room_id: int,
                              start_date: date,
                              end_date: date) -> Optional[str]:
    """Return None if a new block can be placed on this room without
    overlapping ACTIVE bookings or other ACTIVE blocks."""
    from ..models import Booking, RoomBlock

    if start_date is None or end_date is None:
        return 'invalid date'
    if end_date <= start_date:
        return 'end date must be after start date'

    booking = (
        Booking.query
        .filter(
            Booking.room_id == room_id,
            ~Booking.status.in_(_INACTIVE_BOOKING_STATUSES),
            Booking.check_in_date  < end_date,
            Booking.check_out_date > start_date,
        )
        .first()
    )
    if booking is not None:
        return (f'overlaps with booking {booking.booking_ref} '
                f'({booking.check_in_date} → {booking.check_out_date})')

    other_block = (
        RoomBlock.query
        .filter(
            RoomBlock.room_id == room_id,
            RoomBlock.removed_at.is_(None),
            RoomBlock.start_date < end_date,
            RoomBlock.end_date   > start_date,
        )
        .first()
    )
    if other_block is not None:
        return (f'overlaps with existing block '
                f'({other_block.start_date} → {other_block.end_date})')

    return None


# ── Display helpers ─────────────────────────────────────────────────

def block_label(reason: str) -> str:
    """Return the human-readable label for a block reason."""
    for slug, label in ROOM_BLOCK_REASONS:
        if slug == reason:
            return label
    return reason or 'Block'
