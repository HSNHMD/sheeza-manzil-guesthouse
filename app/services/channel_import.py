"""OTA Reservation Import + Exception Queue V1 — service layer.

This module is the safe inbound counterpart to services.channels.
It accepts a normalized reservation payload (dict) for the pilot
channel and either:

  - creates a Booking (with linked external ref + Guest), OR
  - queues a ChannelImportException for an admin to resolve.

V1 hard contract:

  - NO outbound HTTP. The caller must already hold a parsed payload.
    Real OTA HTTP clients land in a later sprint.
  - Idempotent: the same (external_source, external_reservation_ref)
    pair is rejected with `duplicate_skipped` instead of double-booking.
  - Conflict-safe: availability is checked via inventory.check_bookable;
    if zero rooms are bookable for the requested dates, the import is
    queued as a `conflict` exception — never silently inserted.
  - Mapping-strict: if `external_room_id` has no ChannelRoomMap on the
    connection, the import is queued as `mapping_missing`.
  - All paths emit ActivityLog rows. Each helper returns a small dict
    with `ok`, `action`, `booking`/`exception` so route handlers can
    flash a friendly message.

Allowed payload keys (all snake_case):
  external_reservation_ref   (REQUIRED)
  external_room_id           (REQUIRED — resolved via ChannelRoomMap)
  external_rate_plan_id      (optional)
  check_in   / check_out     (REQUIRED — date or YYYY-MM-DD string)
  num_guests                 (optional, default 1)
  guest_first_name           (REQUIRED)
  guest_last_name            (REQUIRED)
  guest_email                (optional)
  guest_phone                (optional)
  total_amount               (optional, float)
  notes                      (optional)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from typing import Any, Optional


# ── Helpers ────────────────────────────────────────────────────────

def _coerce_date(value) -> Optional[date]:
    if value is None or value == '':
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.strptime(value.strip(), '%Y-%m-%d').date()
        except ValueError:
            return None
    return None


def _summarize_payload(payload: dict, max_len: int = 2000) -> str:
    """Build a sanitized summary string of a payload — drops anything
    that looks like a credential / token."""
    blocked = {'password', 'secret', 'token', 'api_key', 'authorization'}
    safe = {k: v for k, v in payload.items()
            if k.lower() not in blocked and not k.lower().endswith('_token')}
    items = []
    for k, v in safe.items():
        s = repr(v)
        if len(s) > 200:
            s = s[:197] + '...'
        items.append(f'{k}={s}')
    out = ', '.join(items)
    return out[:max_len]


def _pick_room_id(room_type_id: int, check_in: date, check_out: date) -> Optional[int]:
    """Return a specific Room.id of the given type that has no booking
    or block conflict in [check_in, check_out). None if all are taken."""
    from ..models import Booking, RoomBlock, Room, RoomType

    rt = RoomType.query.get(room_type_id)
    if rt is None:
        return None

    # Mirror inventory._physical_rooms_of_type — prefer FK match, fall
    # back to legacy name match.
    rooms = Room.query.filter(
        Room.is_active.is_(True),
        Room.room_type_id == room_type_id,
    ).order_by(Room.id).all()
    if not rooms:
        rooms = Room.query.filter(
            Room.is_active.is_(True),
            Room.room_type == rt.name,
        ).order_by(Room.id).all()

    _holding = ('unconfirmed', 'pending_verification', 'confirmed',
                'checked_in')
    for r in rooms:
        if (r.status or '').lower() == 'maintenance':
            continue
        if (r.housekeeping_status or '').lower() == 'out_of_order':
            continue
        booking_conflict = (Booking.query
            .filter(Booking.room_id == r.id,
                    Booking.status.in_(_holding),
                    Booking.check_in_date < check_out,
                    Booking.check_out_date > check_in)
            .first())
        if booking_conflict is not None:
            continue
        block_conflict = (RoomBlock.query
            .filter(RoomBlock.room_id == r.id,
                    RoomBlock.start_date < check_out,
                    RoomBlock.end_date > check_in)
            .first())
        if block_conflict is not None:
            continue
        return r.id
    return None


# ── Result helpers ─────────────────────────────────────────────────

@dataclass
class ImportResult:
    ok: bool
    action: str           # 'imported' | 'duplicate_skipped' | 'queued' | 'failed'
    message: str
    booking: Any = None
    exception: Any = None


# ── Core: import_reservation ───────────────────────────────────────

def import_reservation(*, connection, payload: dict,
                       actor_user_id: Optional[int] = None) -> ImportResult:
    """Idempotent inbound reservation import.

    Either creates a Booking or queues a ChannelImportException; never
    both. The behavior matrix:

      - duplicate (external_source + ref pair already exists)
            → `duplicate_skipped`, no rows touched.
      - mapping for external_room_id missing
            → `mapping_missing` exception queued.
      - payload field validation fails
            → `invalid_payload` exception queued.
      - room type has zero available rooms for the requested dates
            → `conflict` exception queued.
      - happy path
            → Booking created + Guest created + external ref linked +
              `channel.reservation_imported` ActivityLog row.

    Returns an ImportResult.
    """
    from ..models import (db, Booking, Guest,
                          ChannelRoomMap, ChannelRatePlanMap,
                          ChannelImportException)
    from .audit import log_activity
    from .channels import CHANNEL_NAMES, BOOKING_SOURCES
    from ..routes.bookings import generate_booking_ref

    channel_name = connection.channel_name
    if channel_name not in CHANNEL_NAMES:
        return _fail(connection, payload,
                     issue_type='invalid_payload',
                     message=f'channel {channel_name!r} not recognized',
                     actor_user_id=actor_user_id)

    ref = (payload.get('external_reservation_ref') or '').strip()
    if not ref:
        return _fail(connection, payload,
                     issue_type='invalid_payload',
                     message='external_reservation_ref is required.',
                     actor_user_id=actor_user_id)
    if len(ref) > 120:
        return _fail(connection, payload,
                     issue_type='invalid_payload',
                     message='external_reservation_ref too long (>120 chars).',
                     actor_user_id=actor_user_id)

    # ── 1. Idempotency / duplicate guard ──────────────────────────
    existing = (Booking.query
                .filter(Booking.external_source == channel_name)
                .filter(Booking.external_reservation_ref == ref)
                .first())
    if existing is not None:
        log_activity(
            'channel.reservation_duplicate_skipped',
            actor_user_id=actor_user_id,
            description=(
                f'Duplicate import skipped: {channel_name} '
                f'reservation {ref!r} already linked to booking '
                f'{existing.booking_ref}.'
            ),
            metadata={
                'channel_connection_id':    connection.id,
                'channel_name':             channel_name,
                'external_source':          channel_name,
                'external_reservation_ref': ref,
                'booking_id':               existing.id,
                'booking_ref':              existing.booking_ref,
            },
        )
        db.session.commit()
        return ImportResult(
            ok=True, action='duplicate_skipped',
            message=(f'Already imported as {existing.booking_ref} — '
                     f'no changes.'),
            booking=existing,
        )

    # ── 2. Room mapping ───────────────────────────────────────────
    ext_room_id = (payload.get('external_room_id') or '').strip()
    if not ext_room_id:
        return _fail(connection, payload,
                     issue_type='invalid_payload',
                     message='external_room_id is required.',
                     actor_user_id=actor_user_id, ref=ref)

    rm = (ChannelRoomMap.query
          .filter_by(channel_connection_id=connection.id,
                     external_room_id=ext_room_id,
                     is_active=True)
          .first())
    if rm is None:
        return _fail(connection, payload,
                     issue_type='mapping_missing',
                     message=(f'No active room mapping for '
                              f'external_room_id={ext_room_id!r}. '
                              f'Add the mapping on this channel '
                              f'connection, then retry.'),
                     actor_user_id=actor_user_id, ref=ref)

    # ── 3. Rate plan mapping (optional) ───────────────────────────
    ext_rp_id = (payload.get('external_rate_plan_id') or '').strip() or None
    rate_plan_id = None
    if ext_rp_id:
        rpm = (ChannelRatePlanMap.query
               .filter_by(channel_connection_id=connection.id,
                          external_rate_plan_id=ext_rp_id,
                          is_active=True)
               .first())
        if rpm is None:
            return _fail(connection, payload,
                         issue_type='mapping_missing',
                         message=(f'No active rate plan mapping for '
                                  f'external_rate_plan_id={ext_rp_id!r}.'),
                         actor_user_id=actor_user_id, ref=ref)
        rate_plan_id = rpm.rate_plan_id

    # ── 4. Field validation ───────────────────────────────────────
    check_in  = _coerce_date(payload.get('check_in'))
    check_out = _coerce_date(payload.get('check_out'))
    if check_in is None or check_out is None:
        return _fail(connection, payload,
                     issue_type='invalid_payload',
                     message='check_in and check_out (YYYY-MM-DD) required.',
                     actor_user_id=actor_user_id, ref=ref)
    if check_out <= check_in:
        return _fail(connection, payload,
                     issue_type='invalid_payload',
                     message='check_out must be after check_in.',
                     actor_user_id=actor_user_id, ref=ref)

    fname = (payload.get('guest_first_name') or '').strip()
    lname = (payload.get('guest_last_name') or '').strip()
    if not fname or not lname:
        return _fail(connection, payload,
                     issue_type='invalid_payload',
                     message='guest_first_name and guest_last_name required.',
                     actor_user_id=actor_user_id, ref=ref)

    try:
        num_guests = int(payload.get('num_guests') or 1)
    except (TypeError, ValueError):
        num_guests = 1
    if num_guests < 1:
        num_guests = 1

    total_amount_raw = payload.get('total_amount')
    try:
        total_amount = float(total_amount_raw) if total_amount_raw is not None else 0.0
    except (TypeError, ValueError):
        total_amount = 0.0

    # ── 5. Availability — pick a free room of the mapped type ─────
    room_id = _pick_room_id(rm.room_type_id, check_in, check_out)
    if room_id is None:
        return _fail(connection, payload,
                     issue_type='conflict',
                     message=(f'No room of type #{rm.room_type_id} '
                              f'available for {check_in.isoformat()} → '
                              f'{check_out.isoformat()}. Manual review '
                              f'required (move/upgrade or decline).'),
                     actor_user_id=actor_user_id, ref=ref,
                     suggested='Resolve overbooking — move existing '
                               'guest, upgrade, or decline OTA.')

    # ── 6. Create Guest + Booking ─────────────────────────────────
    guest = Guest(
        first_name=fname[:64], last_name=lname[:64],
        email=(payload.get('guest_email') or None),
        phone=(payload.get('guest_phone') or None),
    )
    db.session.add(guest)
    db.session.flush()

    booking = Booking(
        booking_ref=generate_booking_ref(),
        room_id=room_id,
        guest_id=guest.id,
        check_in_date=check_in,
        check_out_date=check_out,
        num_guests=num_guests,
        status='confirmed',
        total_amount=total_amount,
        source=channel_name,
        external_source=channel_name,
        external_reservation_ref=ref,
        billing_target='guest',
        created_by=actor_user_id,
    )
    db.session.add(booking)
    db.session.flush()

    log_activity(
        'channel.reservation_imported',
        actor_user_id=actor_user_id,
        description=(
            f'Imported {channel_name} reservation {ref!r} as '
            f'booking {booking.booking_ref} '
            f'({check_in.isoformat()} → {check_out.isoformat()}, '
            f'room #{room_id}).'
        ),
        metadata={
            'channel_connection_id':    connection.id,
            'channel_name':             channel_name,
            'external_source':          channel_name,
            'external_reservation_ref': ref,
            'booking_id':               booking.id,
            'booking_ref':              booking.booking_ref,
            'entity_type':              'reservation',
            'entity_id':                booking.id,
        },
    )
    db.session.commit()
    return ImportResult(
        ok=True, action='imported',
        message=f'Imported as booking {booking.booking_ref}.',
        booking=booking,
    )


def _fail(connection, payload, *, issue_type, message,
          actor_user_id, ref=None, suggested=None) -> ImportResult:
    """Queue a ChannelImportException + emit the matching ActivityLog
    row. Used for every non-happy path of import_reservation()."""
    from ..models import db, ChannelImportException
    from .audit import log_activity

    ref = ref or (payload.get('external_reservation_ref') or '').strip()
    # Belt-and-suspenders: never store a totally empty ref since the
    # column is NOT NULL. Fall back to a synthetic placeholder.
    if not ref:
        ref = f'<missing-{datetime.utcnow().strftime("%Y%m%d%H%M%S%f")}>'
    ref = ref[:120]

    exc = ChannelImportException(
        channel_connection_id=connection.id,
        external_source=connection.channel_name,
        external_reservation_ref=ref,
        issue_type=issue_type,
        suggested_action=(suggested or message)[:500],
        payload_summary=_summarize_payload(payload),
        status='new',
    )
    db.session.add(exc)
    db.session.flush()

    if issue_type == 'conflict':
        action = 'channel.reservation_conflict_queued'
    else:
        action = 'channel.reservation_import_failed'

    log_activity(
        action,
        actor_user_id=actor_user_id,
        description=(
            f'Inbound {connection.channel_name} reservation {ref!r} '
            f'queued for review: {issue_type} — {message}'
        )[:500],
        metadata={
            'channel_connection_id':    connection.id,
            'channel_name':             connection.channel_name,
            'external_source':          connection.channel_name,
            'external_reservation_ref': ref,
            'issue_type':               issue_type,
            'entity_type':              'reservation',
            'entity_id':                exc.id,
        },
    )
    db.session.commit()
    return ImportResult(
        ok=False, action='queued',
        message=f'Queued for review: {message}',
        exception=exc,
    )


# ── Exception-queue lifecycle ──────────────────────────────────────

def update_exception_status(*, exception, new_status: str,
                            actor_user_id: Optional[int] = None,
                            linked_booking_id: Optional[int] = None,
                            notes: Optional[str] = None) -> dict:
    """Move a ChannelImportException through its lifecycle.

    Allowed: new ↔ reviewed; reviewed → resolved | ignored;
    new → resolved | ignored. resolved/ignored are TERMINAL.

    `linked_booking_id` is captured when status flips to 'resolved'
    so the audit trail records which manual booking covered the OTA
    reservation. `notes` is stored on the exception.
    """
    from ..models import (db, ChannelImportException, Booking,
                          ChannelImportException as _CIE)
    from .audit import log_activity

    if exception is None:
        return {'ok': False, 'error': 'exception not found.'}
    if exception.status in ('resolved', 'ignored'):
        return {'ok': False,
                'error': f'exception is {exception.status} — terminal.'}

    allowed_targets = {
        'new':      {'reviewed', 'resolved', 'ignored'},
        'reviewed': {'resolved', 'ignored'},
    }
    target = (new_status or '').strip().lower()
    if target not in allowed_targets.get(exception.status, set()):
        return {'ok': False,
                'error': (f'cannot move from {exception.status} to '
                          f'{target!r}.')}

    if linked_booking_id is not None:
        b = Booking.query.get(linked_booking_id)
        if b is None:
            return {'ok': False, 'error': 'linked booking not found.'}
        exception.linked_booking_id = b.id

    old_status = exception.status
    exception.status = target
    if target in ('reviewed', 'resolved', 'ignored'):
        exception.reviewed_at = datetime.utcnow()
        exception.reviewed_by_user_id = actor_user_id
    if notes:
        exception.notes = notes[:1000]

    log_activity(
        'channel.exception_status_changed',
        actor_user_id=actor_user_id,
        description=(
            f'Channel exception #{exception.id} {old_status} → {target} '
            f'({exception.external_source} ref '
            f'{exception.external_reservation_ref!r}).'
        ),
        metadata={
            'channel_connection_id':    exception.channel_connection_id,
            'channel_name':             exception.external_source,
            'external_source':          exception.external_source,
            'external_reservation_ref': exception.external_reservation_ref,
            'entity_type':              'channel_import_exception',
            'entity_id':                exception.id,
            'old_status':               old_status,
            'new_status':               target,
        },
    )
    db.session.commit()
    return {'ok': True, 'error': None, 'exception': exception}


# ── Read helpers (for the admin queue page) ────────────────────────

def list_exceptions(*, status: Optional[str] = None,
                    issue_type: Optional[str] = None,
                    channel_connection_id: Optional[int] = None,
                    open_only: bool = False,
                    limit: int = 200):
    from ..models import ChannelImportException
    q = ChannelImportException.query
    if open_only:
        q = q.filter(ChannelImportException.status.in_(('new', 'reviewed')))
    if status:
        q = q.filter(ChannelImportException.status == status)
    if issue_type:
        q = q.filter(ChannelImportException.issue_type == issue_type)
    if channel_connection_id:
        q = q.filter(ChannelImportException.channel_connection_id ==
                     channel_connection_id)
    return q.order_by(ChannelImportException.created_at.desc()).limit(limit).all()


def summary_counts() -> dict:
    """KPI tile counts for the exception-queue page."""
    from ..models import ChannelImportException
    open_q = ChannelImportException.query.filter(
        ChannelImportException.status.in_(('new', 'reviewed')))
    return {
        'total':    ChannelImportException.query.count(),
        'open':     open_q.count(),
        'new':      open_q.filter(ChannelImportException.status == 'new').count(),
        'reviewed': open_q.filter(ChannelImportException.status == 'reviewed').count(),
        'conflict': open_q.filter(ChannelImportException.issue_type == 'conflict').count(),
    }
