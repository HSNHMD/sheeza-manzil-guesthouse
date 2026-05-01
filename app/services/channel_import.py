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


# ── Idempotent event tracking ─────────────────────────────────────

def _coerce_event_id(payload: dict, event_type: str) -> str:
    """Return the OTA-provided external_event_id if present, otherwise
    a deterministic synthetic id derived from (event_type, ref,
    payload-hash). Synthetic ids stay stable across retries with an
    identical payload — so the unique index on
    (channel_connection_id, external_event_id) still dedupes them."""
    raw = (payload.get('external_event_id') or '').strip()
    if raw:
        return raw[:120]
    import hashlib, json as _json
    ref = (payload.get('external_reservation_ref') or '').strip()
    try:
        body = _json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError):
        body = repr(sorted((k, repr(v)) for k, v in payload.items()))
    h = hashlib.sha256(f'{event_type}:{ref}:{body}'.encode()).hexdigest()[:32]
    return f'{event_type}:{ref}:{h}'[:120]


def _check_duplicate_event(connection, external_event_id: str):
    from ..models import ChannelInboundEvent
    return (ChannelInboundEvent.query
            .filter_by(channel_connection_id=connection.id,
                       external_event_id=external_event_id)
            .first())


def _record_event(connection, *, external_event_id: str,
                  external_reservation_ref: str,
                  event_type: str, result_status: str,
                  linked_booking_id: Optional[int] = None,
                  exception_id: Optional[int] = None,
                  notes: Optional[str] = None):
    from ..models import db, ChannelInboundEvent
    row = ChannelInboundEvent(
        channel_connection_id=connection.id,
        external_event_id=external_event_id[:120],
        external_reservation_ref=external_reservation_ref[:120],
        event_type=event_type,
        result_status=result_status,
        linked_booking_id=linked_booking_id,
        exception_id=exception_id,
        notes=(notes or '')[:500] or None,
    )
    db.session.add(row)
    db.session.flush()
    return row


def _emit_duplicate_skipped(connection, *, event_id, ref, event_type,
                            actor_user_id=None) -> ImportResult:
    from ..models import db
    from .audit import log_activity
    log_activity(
        'channel.event_duplicate_skipped',
        actor_user_id=actor_user_id,
        description=(
            f'Duplicate {event_type} event {event_id!r} on '
            f'{connection.channel_name} reservation {ref!r} — already '
            f'processed; no changes.'
        )[:500],
        metadata={
            'channel_connection_id':    connection.id,
            'channel_name':             connection.channel_name,
            'external_source':          connection.channel_name,
            'external_reservation_ref': ref,
            'event_type':               event_type,
            'result_status':            'duplicate_skipped',
        },
    )
    db.session.commit()
    return ImportResult(
        ok=True, action='duplicate_skipped',
        message=f'Event {event_id!r} already processed — no changes.',
    )


def _queue_action(event_type: str) -> str:
    """Map event_type → ActivityLog action name used when the event
    is queued as an exception (rather than auto-applied)."""
    return {
        'reservation_imported':   'channel.reservation_import_failed',
        'reservation_modified':   'channel.reservation_modification_queued',
        'reservation_cancelled':  'channel.reservation_cancellation_queued',
    }.get(event_type, 'channel.reservation_import_failed')


def _queue_for_review(connection, payload, *, event_type, event_id,
                      issue_type, message, actor_user_id,
                      ref, booking=None, suggested=None) -> ImportResult:
    """Queue a ChannelImportException + write a ChannelInboundEvent
    row tagged 'queued'. Used by apply_modification / apply_cancellation.
    """
    from ..models import db, ChannelImportException
    from .audit import log_activity

    exc = ChannelImportException(
        channel_connection_id=connection.id,
        external_source=connection.channel_name,
        external_reservation_ref=ref[:120],
        issue_type=issue_type,
        suggested_action=(suggested or message)[:500],
        payload_summary=_summarize_payload(payload),
        status='new',
        linked_booking_id=(booking.id if booking is not None else None),
    )
    db.session.add(exc)
    db.session.flush()

    log_activity(
        _queue_action(event_type),
        actor_user_id=actor_user_id,
        description=(
            f'{event_type} event for {connection.channel_name} '
            f'reservation {ref!r} queued for review: {issue_type} — '
            f'{message}'
        )[:500],
        metadata={
            'channel_connection_id':    connection.id,
            'channel_name':             connection.channel_name,
            'external_source':          connection.channel_name,
            'external_reservation_ref': ref,
            'event_type':               event_type,
            'issue_type':               issue_type,
            'result_status':            'queued',
            'booking_id':  (booking.id if booking is not None else None),
            'booking_ref': (booking.booking_ref if booking is not None else None),
            'entity_type':              'channel_import_exception',
            'entity_id':                exc.id,
        },
    )
    _record_event(
        connection,
        external_event_id=event_id,
        external_reservation_ref=ref,
        event_type=event_type, result_status='queued',
        linked_booking_id=(booking.id if booking is not None else None),
        exception_id=exc.id,
        notes=message,
    )
    db.session.commit()
    return ImportResult(
        ok=False, action='queued',
        message=f'Queued for review: {message}',
        exception=exc, booking=booking,
    )


# ── Modification handler ───────────────────────────────────────────

def _booking_for_ref(connection, ref):
    from ..models import Booking
    return (Booking.query
            .filter(Booking.external_source == connection.channel_name)
            .filter(Booking.external_reservation_ref == ref)
            .first())


def apply_modification(*, connection, payload: dict,
                       actor_user_id: Optional[int] = None) -> ImportResult:
    """Apply an OTA modification event to an existing booking.

    Behavior matrix:
      - duplicate event_id        → `duplicate_skipped`, no rows touched
      - booking not found         → `booking_not_found` exception queued
      - booking in unsafe state   → `modification_unsafe_state` queued
                                    (checked_in, checked_out, cancelled)
      - new dates conflict        → `conflict` queued — booking unchanged
      - new external_room_id has  → `mapping_missing` queued
        no active mapping
      - happy path                → safe-field diff applied; emits
                                    `channel.reservation_modified`

    Safe modifications applied automatically (when present in payload):
        check_in / check_out
        num_guests
        guest_first_name / guest_last_name / guest_email / guest_phone
        total_amount
        external_room_id (re-mapped → new room of mapped type)

    Anything else (e.g. payment changes, status overrides) intentionally
    requires a manual review — V1 never silently mutates folio state.
    """
    from ..models import (db, Booking, Guest, ChannelRoomMap)
    from .audit import log_activity

    ref = (payload.get('external_reservation_ref') or '').strip()
    if not ref:
        return _queue_for_review(
            connection, payload,
            event_type='reservation_modified',
            event_id=_coerce_event_id(payload, 'reservation_modified'),
            issue_type='invalid_payload',
            message='external_reservation_ref is required.',
            actor_user_id=actor_user_id, ref='<missing>',
        )

    event_id = _coerce_event_id(payload, 'reservation_modified')

    # 1. Idempotency
    existing = _check_duplicate_event(connection, event_id)
    if existing is not None:
        return _emit_duplicate_skipped(
            connection, event_id=event_id, ref=ref,
            event_type='reservation_modified',
            actor_user_id=actor_user_id,
        )

    # 2. Locate booking
    booking = _booking_for_ref(connection, ref)
    if booking is None:
        return _queue_for_review(
            connection, payload,
            event_type='reservation_modified', event_id=event_id,
            issue_type='booking_not_found',
            message=(f'No local booking matches '
                     f'{connection.channel_name} reservation {ref!r}. '
                     f'Import the reservation first, then re-send the '
                     f'modification.'),
            actor_user_id=actor_user_id, ref=ref,
        )

    # 3. State gate
    if booking.status in ('checked_in', 'checked_out', 'cancelled'):
        return _queue_for_review(
            connection, payload,
            event_type='reservation_modified', event_id=event_id,
            issue_type='modification_unsafe_state',
            message=(f'Booking {booking.booking_ref} is '
                     f'{booking.status} — modifications cannot be '
                     f'auto-applied. An operator must decide.'),
            actor_user_id=actor_user_id, ref=ref, booking=booking,
        )

    # 4. Compute the diff
    diff = {}
    new_check_in  = _coerce_date(payload.get('check_in'))
    new_check_out = _coerce_date(payload.get('check_out'))
    if 'check_in'  in payload and new_check_in  is None and payload.get('check_in'):
        return _queue_for_review(
            connection, payload,
            event_type='reservation_modified', event_id=event_id,
            issue_type='invalid_payload',
            message='check_in must be YYYY-MM-DD.',
            actor_user_id=actor_user_id, ref=ref, booking=booking,
        )
    if 'check_out' in payload and new_check_out is None and payload.get('check_out'):
        return _queue_for_review(
            connection, payload,
            event_type='reservation_modified', event_id=event_id,
            issue_type='invalid_payload',
            message='check_out must be YYYY-MM-DD.',
            actor_user_id=actor_user_id, ref=ref, booking=booking,
        )

    target_check_in  = new_check_in  or booking.check_in_date
    target_check_out = new_check_out or booking.check_out_date
    if target_check_out <= target_check_in:
        return _queue_for_review(
            connection, payload,
            event_type='reservation_modified', event_id=event_id,
            issue_type='invalid_payload',
            message='check_out must be after check_in.',
            actor_user_id=actor_user_id, ref=ref, booking=booking,
        )

    target_room_id = booking.room_id
    new_ext_room   = (payload.get('external_room_id') or '').strip()
    if new_ext_room:
        rm = (ChannelRoomMap.query
              .filter_by(channel_connection_id=connection.id,
                         external_room_id=new_ext_room,
                         is_active=True)
              .first())
        if rm is None:
            return _queue_for_review(
                connection, payload,
                event_type='reservation_modified', event_id=event_id,
                issue_type='mapping_missing',
                message=(f'No active room mapping for '
                         f'external_room_id={new_ext_room!r}.'),
                actor_user_id=actor_user_id, ref=ref, booking=booking,
            )
        # If the mapped type changed OR dates changed, re-pick a room
        # to confirm availability for the new window/type.
        new_room_id = _pick_room_id_excluding(
            rm.room_type_id, target_check_in, target_check_out,
            exclude_booking_id=booking.id,
        )
        if new_room_id is None:
            return _queue_for_review(
                connection, payload,
                event_type='reservation_modified', event_id=event_id,
                issue_type='conflict',
                message=(f'No room of the newly-mapped type available '
                         f'for {target_check_in.isoformat()} → '
                         f'{target_check_out.isoformat()}.'),
                actor_user_id=actor_user_id, ref=ref, booking=booking,
                suggested='Move existing guest, upgrade, or decline.',
            )
        target_room_id = new_room_id

    # 5. Date-range conflict check (only if dates shifted, no room
    #    re-map — the room_id stays the same)
    if (new_check_in is not None or new_check_out is not None) and \
       not new_ext_room:
        # Check there's no other booking on this same room overlapping
        # the new window.
        from ..models import Booking as _B, RoomBlock as _RB
        _holding = ('unconfirmed', 'pending_verification',
                    'confirmed', 'checked_in')
        clash = (_B.query
                 .filter(_B.room_id == booking.room_id,
                         _B.id != booking.id,
                         _B.status.in_(_holding),
                         _B.check_in_date < target_check_out,
                         _B.check_out_date > target_check_in)
                 .first())
        if clash is None:
            clash_block = (_RB.query
                           .filter(_RB.room_id == booking.room_id,
                                   _RB.start_date < target_check_out,
                                   _RB.end_date > target_check_in)
                           .first())
            if clash_block is not None:
                clash = clash_block
        if clash is not None:
            return _queue_for_review(
                connection, payload,
                event_type='reservation_modified', event_id=event_id,
                issue_type='conflict',
                message=(f'Booking {booking.booking_ref} cannot be '
                         f'extended to {target_check_in.isoformat()} → '
                         f'{target_check_out.isoformat()}: room is '
                         f'occupied / blocked in that range.'),
                actor_user_id=actor_user_id, ref=ref, booking=booking,
                suggested='Move to a different room or decline.',
            )

    # 6. Apply
    if new_check_in is not None and new_check_in != booking.check_in_date:
        diff['check_in_date'] = (booking.check_in_date.isoformat(),
                                  new_check_in.isoformat())
        booking.check_in_date = new_check_in
    if new_check_out is not None and new_check_out != booking.check_out_date:
        diff['check_out_date'] = (booking.check_out_date.isoformat(),
                                   new_check_out.isoformat())
        booking.check_out_date = new_check_out

    if 'num_guests' in payload and payload.get('num_guests') is not None:
        try:
            ng = int(payload.get('num_guests'))
        except (TypeError, ValueError):
            ng = booking.num_guests
        if ng >= 1 and ng != booking.num_guests:
            diff['num_guests'] = (booking.num_guests, ng)
            booking.num_guests = ng

    if 'total_amount' in payload and payload.get('total_amount') is not None:
        try:
            ta = float(payload.get('total_amount'))
        except (TypeError, ValueError):
            ta = booking.total_amount
        if ta != booking.total_amount:
            diff['total_amount'] = (booking.total_amount, ta)
            booking.total_amount = ta

    if target_room_id != booking.room_id:
        diff['room_id'] = (booking.room_id, target_room_id)
        booking.room_id = target_room_id

    # Guest-level fields are safe to update in place — they only
    # affect the contact card, not booking integrity.
    if booking.guest is not None:
        for fld, payload_key, max_len in (
            ('first_name', 'guest_first_name', 64),
            ('last_name',  'guest_last_name',  64),
            ('email',      'guest_email',      120),
            ('phone',      'guest_phone',      20),
        ):
            new_val = payload.get(payload_key)
            if new_val is None:
                continue
            new_val = str(new_val).strip()[:max_len] or None
            old_val = getattr(booking.guest, fld, None)
            if new_val != old_val:
                diff[f'guest.{fld}'] = (old_val, new_val)
                setattr(booking.guest, fld, new_val)

    # 7. Idempotent no-op: if nothing changed, still record the event
    #    so the next replay short-circuits as duplicate.
    if not diff:
        log_activity(
            'channel.reservation_modified',
            actor_user_id=actor_user_id,
            description=(
                f'Modification event for booking {booking.booking_ref} '
                f'({connection.channel_name} ref {ref!r}) was a no-op '
                f'— payload matched current state.'
            )[:500],
            metadata={
                'channel_connection_id':    connection.id,
                'channel_name':             connection.channel_name,
                'external_source':          connection.channel_name,
                'external_reservation_ref': ref,
                'event_type':               'reservation_modified',
                'result_status':            'no_op',
                'booking_id':               booking.id,
                'booking_ref':              booking.booking_ref,
            },
        )
        _record_event(
            connection,
            external_event_id=event_id,
            external_reservation_ref=ref,
            event_type='reservation_modified',
            result_status='success',
            linked_booking_id=booking.id, notes='no-op (already up to date)',
        )
        db.session.commit()
        return ImportResult(
            ok=True, action='imported',
            message=f'No changes — booking {booking.booking_ref} '
                    f'already matches the OTA state.',
            booking=booking,
        )

    log_activity(
        'channel.reservation_modified',
        actor_user_id=actor_user_id,
        description=(
            f'Modified booking {booking.booking_ref} from '
            f'{connection.channel_name} reservation {ref!r}: '
            f'{", ".join(sorted(diff.keys()))}.'
        )[:500],
        metadata={
            'channel_connection_id':    connection.id,
            'channel_name':             connection.channel_name,
            'external_source':          connection.channel_name,
            'external_reservation_ref': ref,
            'event_type':               'reservation_modified',
            'result_status':            'success',
            'booking_id':               booking.id,
            'booking_ref':              booking.booking_ref,
            'fields_changed':           ','.join(sorted(diff.keys())),
        },
    )
    _record_event(
        connection,
        external_event_id=event_id,
        external_reservation_ref=ref,
        event_type='reservation_modified',
        result_status='success',
        linked_booking_id=booking.id,
        notes=f'fields: {", ".join(sorted(diff.keys()))}',
    )
    db.session.commit()
    return ImportResult(
        ok=True, action='imported',
        message=(f'Updated {booking.booking_ref}: '
                 f'{", ".join(sorted(diff.keys()))}.'),
        booking=booking,
    )


def _pick_room_id_excluding(room_type_id, check_in, check_out, *,
                             exclude_booking_id):
    """Same as _pick_room_id but ignores ONE specific booking — useful
    when the same booking is being shifted in time and would otherwise
    conflict with itself."""
    from ..models import Booking, RoomBlock, Room, RoomType
    rt = RoomType.query.get(room_type_id)
    if rt is None:
        return None
    rooms = Room.query.filter(
        Room.is_active.is_(True),
        Room.room_type_id == room_type_id,
    ).order_by(Room.id).all()
    if not rooms:
        rooms = Room.query.filter(
            Room.is_active.is_(True),
            Room.room_type == rt.name,
        ).order_by(Room.id).all()
    _holding = ('unconfirmed', 'pending_verification',
                'confirmed', 'checked_in')
    for r in rooms:
        if (r.status or '').lower() == 'maintenance':
            continue
        if (r.housekeeping_status or '').lower() == 'out_of_order':
            continue
        clash = (Booking.query
                 .filter(Booking.room_id == r.id,
                         Booking.id != exclude_booking_id,
                         Booking.status.in_(_holding),
                         Booking.check_in_date < check_out,
                         Booking.check_out_date > check_in)
                 .first())
        if clash is not None:
            continue
        block = (RoomBlock.query
                 .filter(RoomBlock.room_id == r.id,
                         RoomBlock.start_date < check_out,
                         RoomBlock.end_date > check_in)
                 .first())
        if block is not None:
            continue
        return r.id
    return None


# ── Cancellation handler ──────────────────────────────────────────

def _booking_has_paid_invoice(booking) -> bool:
    """True if any invoice on the booking has amount_paid > 0. Drives
    the `cancel_unsafe_state` gate — we never auto-cancel a booking
    that already took money. Refund decision is operator-only."""
    from ..models import Invoice
    paid = (Invoice.query
            .filter(Invoice.booking_id == booking.id,
                    Invoice.amount_paid > 0)
            .first())
    return paid is not None


def apply_cancellation(*, connection, payload: dict,
                       actor_user_id: Optional[int] = None) -> ImportResult:
    """Apply an OTA cancellation event.

    Behavior matrix:
      - duplicate event_id        → `duplicate_skipped`
      - booking not found         → `booking_not_found` exception
      - already cancelled         → log idempotent + record
                                    `result_status='already_cancelled'`,
                                    return ok=True
      - status is checked_in /
        checked_out               → `cancel_unsafe_state` exception
      - booking has paid invoice  → `cancel_unsafe_state` exception
                                    (refund decision required)
      - happy path                → set Booking.status='cancelled',
                                    emit `channel.reservation_cancelled`

    NEVER mutates folio / payment rows. Folio cleanup is operator-
    driven via the existing accounting surfaces.
    """
    from ..models import db, Booking
    from .audit import log_activity

    ref = (payload.get('external_reservation_ref') or '').strip()
    if not ref:
        return _queue_for_review(
            connection, payload,
            event_type='reservation_cancelled',
            event_id=_coerce_event_id(payload, 'reservation_cancelled'),
            issue_type='invalid_payload',
            message='external_reservation_ref is required.',
            actor_user_id=actor_user_id, ref='<missing>',
        )

    event_id = _coerce_event_id(payload, 'reservation_cancelled')

    # 1. Idempotency on event_id
    if _check_duplicate_event(connection, event_id) is not None:
        return _emit_duplicate_skipped(
            connection, event_id=event_id, ref=ref,
            event_type='reservation_cancelled',
            actor_user_id=actor_user_id,
        )

    # 2. Locate
    booking = _booking_for_ref(connection, ref)
    if booking is None:
        return _queue_for_review(
            connection, payload,
            event_type='reservation_cancelled', event_id=event_id,
            issue_type='booking_not_found',
            message=(f'No local booking matches '
                     f'{connection.channel_name} reservation {ref!r}.'),
            actor_user_id=actor_user_id, ref=ref,
        )

    # 3. Already cancelled — idempotent success
    if booking.status == 'cancelled':
        log_activity(
            'channel.event_duplicate_skipped',
            actor_user_id=actor_user_id,
            description=(
                f'Cancellation for booking {booking.booking_ref} '
                f'({connection.channel_name} ref {ref!r}) already '
                f'applied — no changes.'
            ),
            metadata={
                'channel_connection_id':    connection.id,
                'channel_name':             connection.channel_name,
                'external_source':          connection.channel_name,
                'external_reservation_ref': ref,
                'event_type':               'reservation_cancelled',
                'result_status':            'already_cancelled',
                'booking_id':               booking.id,
                'booking_ref':              booking.booking_ref,
            },
        )
        _record_event(
            connection,
            external_event_id=event_id,
            external_reservation_ref=ref,
            event_type='reservation_cancelled',
            result_status='already_cancelled',
            linked_booking_id=booking.id,
            notes='booking was already cancelled.',
        )
        db.session.commit()
        return ImportResult(
            ok=True, action='duplicate_skipped',
            message=(f'Booking {booking.booking_ref} is already '
                     f'cancelled — no changes.'),
            booking=booking,
        )

    # 4. State gate
    if booking.status in ('checked_in', 'checked_out'):
        return _queue_for_review(
            connection, payload,
            event_type='reservation_cancelled', event_id=event_id,
            issue_type='cancel_unsafe_state',
            message=(f'Booking {booking.booking_ref} is '
                     f'{booking.status} — cancellation requires '
                     f'operator decision (refund / no-show / late '
                     f'cancellation policy).'),
            actor_user_id=actor_user_id, ref=ref, booking=booking,
            suggested='Decide refund + adjust folio manually, then '
                      'mark this exception resolved.',
        )

    # 5. Payment gate
    if _booking_has_paid_invoice(booking):
        return _queue_for_review(
            connection, payload,
            event_type='reservation_cancelled', event_id=event_id,
            issue_type='cancel_unsafe_state',
            message=(f'Booking {booking.booking_ref} has at least one '
                     f'paid invoice — auto-cancellation would leave '
                     f'orphan funds. An operator must process the '
                     f'refund first.'),
            actor_user_id=actor_user_id, ref=ref, booking=booking,
            suggested='Refund + void invoice via /accounting, then '
                      'cancel manually + mark this exception resolved.',
        )

    # 6. Apply
    booking.status = 'cancelled'
    log_activity(
        'channel.reservation_cancelled',
        actor_user_id=actor_user_id,
        description=(
            f'Cancelled booking {booking.booking_ref} from '
            f'{connection.channel_name} reservation {ref!r}.'
        ),
        metadata={
            'channel_connection_id':    connection.id,
            'channel_name':             connection.channel_name,
            'external_source':          connection.channel_name,
            'external_reservation_ref': ref,
            'event_type':               'reservation_cancelled',
            'result_status':            'success',
            'booking_id':               booking.id,
            'booking_ref':              booking.booking_ref,
        },
    )
    _record_event(
        connection,
        external_event_id=event_id,
        external_reservation_ref=ref,
        event_type='reservation_cancelled',
        result_status='success',
        linked_booking_id=booking.id,
        notes=(payload.get('reason') or 'OTA cancellation')[:500],
    )
    db.session.commit()
    return ImportResult(
        ok=True, action='imported',
        message=f'Cancelled booking {booking.booking_ref}.',
        booking=booking,
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
