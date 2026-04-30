"""Reservation Board mutation services.

Wraps the read-only conflict-check helpers in services.board_actions
with the actual DB writes + ActivityLog entries. Each function
returns a structured `Result` so route handlers don't need to know
about flash/JSON formatting.

Three operations:

  1. apply_booking_room_move  — drag a booking to a new room
  2. apply_stay_update        — extend / shorten the stay
  3. split_stay               — mid-stay room change foundation

All three are pure with respect to the Flask app context (they need
one for db.session, but they don't touch request/flash/url_for) so
they can be unit-tested with a test_client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


@dataclass
class Result:
    """Mutation outcome — caller decides how to render it."""
    ok:       bool
    message:  str
    booking:  Optional[object] = None
    extra:    dict = field(default_factory=dict)

    @classmethod
    def fail(cls, message: str, **extra) -> 'Result':
        return cls(ok=False, message=message, extra=extra)

    @classmethod
    def success(cls, message: str, booking=None, **extra) -> 'Result':
        return cls(ok=True, message=message, booking=booking, extra=extra)


# ── Phase B: room move ─────────────────────────────────────────────

def apply_booking_room_move(*, booking_id: int, target_room_id: int,
                            actor_user_id: Optional[int] = None,
                            note: Optional[str] = None) -> Result:
    """Move a booking to a different room.

    Validates via `check_booking_room_move_conflict` before mutating.
    Refuses to move bookings whose status is cancelled / rejected
    (they don't consume a room anyway). Logs `booking.room_moved`
    with the old + new room numbers.
    """
    from ..models import db, Booking, Room
    from .board_actions import check_booking_room_move_conflict
    from .audit import log_activity

    booking = Booking.query.get(booking_id)
    if booking is None:
        return Result.fail('Booking not found.')

    target_room = Room.query.get(target_room_id)
    if target_room is None:
        return Result.fail('Target room not found.')
    if not target_room.is_active:
        return Result.fail(f'Room {target_room.number} is inactive.')

    if booking.status in ('cancelled', 'rejected'):
        return Result.fail(
            f'Booking {booking.booking_ref} is {booking.status} '
            f'and cannot be moved.'
        )

    if booking.room_id == target_room_id:
        return Result.fail(
            f'Booking is already on room {target_room.number}.'
        )

    conflict = check_booking_room_move_conflict(
        target_room_id=target_room_id,
        check_in_date=booking.check_in_date,
        check_out_date=booking.check_out_date,
        exclude_booking_id=booking.id,
    )
    if conflict is not None:
        return Result.fail(f'Cannot move: {conflict}.')

    old_room = booking.room
    old_room_number = old_room.number if old_room else '?'
    booking.room_id = target_room_id

    log_activity(
        'booking.room_moved',
        booking_id=booking.id,
        actor_user_id=actor_user_id,
        description=(
            f'Booking {booking.booking_ref} moved from '
            f'#{old_room_number} → #{target_room.number}.'
            + (f' Note: {note}' if note else '')
        ),
        metadata={
            'booking_id':        booking.id,
            'booking_ref':       booking.booking_ref,
            'old_room_id':       getattr(old_room, 'id', None),
            'old_room_number':   old_room_number,
            'new_room_id':       target_room.id,
            'new_room_number':   target_room.number,
            'check_in_date':     booking.check_in_date.isoformat(),
            'check_out_date':    booking.check_out_date.isoformat(),
            'note':              (note or '')[:240],
        },
    )
    db.session.commit()

    return Result.success(
        f'Moved {booking.booking_ref} to room {target_room.number}.',
        booking=booking,
        old_room_number=old_room_number,
        new_room_number=target_room.number,
    )


# ── Phase C: stay extend / shorten ─────────────────────────────────

def apply_stay_update(*, booking_id: int,
                      new_check_in: Optional[date] = None,
                      new_check_out: Optional[date] = None,
                      actor_user_id: Optional[int] = None,
                      note: Optional[str] = None) -> Result:
    """Update one or both ends of a booking's stay.

    Either `new_check_in` or `new_check_out` (or both) must be
    supplied. The other end is preserved from the existing booking.
    Validates via `check_stay_update_conflict`. Logs
    `booking.stay_updated` with old/new dates.
    """
    from ..models import db, Booking
    from .board_actions import check_stay_update_conflict
    from .audit import log_activity

    booking = Booking.query.get(booking_id)
    if booking is None:
        return Result.fail('Booking not found.')

    if booking.status in ('cancelled', 'rejected'):
        return Result.fail(
            f'Booking {booking.booking_ref} is {booking.status} '
            f'and cannot be resized.'
        )

    old_in  = booking.check_in_date
    old_out = booking.check_out_date

    target_in  = new_check_in  if new_check_in  is not None else old_in
    target_out = new_check_out if new_check_out is not None else old_out

    if target_in == old_in and target_out == old_out:
        return Result.fail('No change in stay dates.')

    conflict = check_stay_update_conflict(
        room_id=booking.room_id,
        booking_id=booking.id,
        new_check_in=target_in,
        new_check_out=target_out,
    )
    if conflict is not None:
        return Result.fail(f'Cannot resize: {conflict}.')

    booking.check_in_date  = target_in
    booking.check_out_date = target_out

    nights_old = (old_out  - old_in).days
    nights_new = (target_out - target_in).days

    log_activity(
        'booking.stay_updated',
        booking_id=booking.id,
        actor_user_id=actor_user_id,
        description=(
            f'Booking {booking.booking_ref} resized: '
            f'{old_in}→{old_out} ({nights_old}n) ⇒ '
            f'{target_in}→{target_out} ({nights_new}n).'
            + (f' Note: {note}' if note else '')
        ),
        metadata={
            'booking_id':       booking.id,
            'booking_ref':      booking.booking_ref,
            'old_check_in':     old_in.isoformat(),
            'old_check_out':    old_out.isoformat(),
            'new_check_in':     target_in.isoformat(),
            'new_check_out':    target_out.isoformat(),
            'nights_old':       nights_old,
            'nights_new':       nights_new,
            'nights_delta':     nights_new - nights_old,
            'note':             (note or '')[:240],
        },
    )
    db.session.commit()

    return Result.success(
        f'Stay updated: {target_in.isoformat()} → '
        f'{target_out.isoformat()} ({nights_new} nights).',
        booking=booking,
        nights_old=nights_old, nights_new=nights_new,
    )


# ── Phase D: split stay (mid-stay room change foundation) ──────────

def split_stay(*, booking_id: int, split_date: date,
               target_room_id: int,
               actor_user_id: Optional[int] = None,
               note: Optional[str] = None) -> Result:
    """Materialize a mid-stay room change as two StaySegments.

    On success the booking now has TWO stay_segments rows:

        segment 1: original room, check_in_date → split_date
        segment 2: target  room, split_date     → check_out_date

    Both segments are attached to the same Booking — folio, guest,
    payments, and history continue to live on the single booking row.
    The Booking.room_id stays the ORIGINAL room (so existing board
    rendering is unchanged); the segments table is the new source of
    truth for "where is the guest tonight?". Segment-aware rendering
    is the next sprint.

    Validations:
      - split_date strictly inside (check_in, check_out)
      - target_room exists, active, different from current room
      - target_room has no booking / block overlap on
        [split_date, check_out_date)
      - booking is not cancelled/rejected/checked_out
    """
    from ..models import db, Booking, Room, StaySegment
    from .board_actions import check_booking_room_move_conflict
    from .audit import log_activity

    booking = Booking.query.get(booking_id)
    if booking is None:
        return Result.fail('Booking not found.')

    if booking.status in ('cancelled', 'rejected', 'checked_out'):
        return Result.fail(
            f'Booking {booking.booking_ref} is {booking.status} '
            f'— cannot split.'
        )

    if not (booking.check_in_date < split_date < booking.check_out_date):
        return Result.fail(
            f'Split date must be strictly between check-in '
            f'({booking.check_in_date}) and check-out '
            f'({booking.check_out_date}).'
        )

    target_room = Room.query.get(target_room_id)
    if target_room is None:
        return Result.fail('Target room not found.')
    if not target_room.is_active:
        return Result.fail(f'Room {target_room.number} is inactive.')
    if target_room.id == booking.room_id:
        return Result.fail(
            'Target room is the same as the current room.'
        )

    # Conflict check is on the second-half range only — the first
    # half already lives on booking.room_id without overlap.
    conflict = check_booking_room_move_conflict(
        target_room_id=target_room.id,
        check_in_date=split_date,
        check_out_date=booking.check_out_date,
        exclude_booking_id=booking.id,
    )
    if conflict is not None:
        return Result.fail(f'Cannot split: {conflict}.')

    # If the booking already has segments, refuse — re-segmenting is
    # a separate UX we'll design after the foundation lands.
    existing = booking.stay_segments.count()
    if existing > 0:
        return Result.fail(
            f'Booking already has {existing} stay segment(s). '
            f'Re-segmenting is not supported in V1.'
        )

    seg1 = StaySegment(
        booking_id=booking.id, room_id=booking.room_id,
        start_date=booking.check_in_date, end_date=split_date,
        notes=note, created_by_user_id=actor_user_id,
    )
    seg2 = StaySegment(
        booking_id=booking.id, room_id=target_room.id,
        start_date=split_date, end_date=booking.check_out_date,
        notes=note, created_by_user_id=actor_user_id,
    )
    db.session.add_all([seg1, seg2])
    db.session.flush()

    log_activity(
        'booking.stay_split',
        booking_id=booking.id,
        actor_user_id=actor_user_id,
        description=(
            f'Booking {booking.booking_ref} split at {split_date}: '
            f'{seg1.room.number} for nights '
            f'{seg1.start_date}→{seg1.end_date}, then '
            f'{seg2.room.number} for nights '
            f'{seg2.start_date}→{seg2.end_date}.'
            + (f' Note: {note}' if note else '')
        ),
        metadata={
            'booking_id':         booking.id,
            'booking_ref':        booking.booking_ref,
            'split_date':         split_date.isoformat(),
            'segment_1_room_id':  seg1.room_id,
            'segment_1_room_number': seg1.room.number,
            'segment_1_nights':   seg1.nights,
            'segment_2_room_id':  seg2.room_id,
            'segment_2_room_number': seg2.room.number,
            'segment_2_nights':   seg2.nights,
            'note':               (note or '')[:240],
        },
    )
    db.session.commit()

    return Result.success(
        f'Booking {booking.booking_ref} split: '
        f'{seg1.room.number} ({seg1.nights}n) → '
        f'{seg2.room.number} ({seg2.nights}n).',
        booking=booking,
        segments=[
            {'id': seg1.id, 'room': seg1.room.number,
             'start': seg1.start_date.isoformat(),
             'end':   seg1.end_date.isoformat(),
             'nights': seg1.nights},
            {'id': seg2.id, 'room': seg2.room.number,
             'start': seg2.start_date.isoformat(),
             'end':   seg2.end_date.isoformat(),
             'nights': seg2.nights},
        ],
    )
