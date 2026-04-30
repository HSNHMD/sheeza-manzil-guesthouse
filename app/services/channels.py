"""Channel Manager Foundation V1 — internal service layer.

Pure DB writes. **NEVER** makes an outbound HTTP call to an OTA in
this V1 — that's reserved for Phase 4 of the build plan documented
in docs/channel_manager_build_phases.md. Every helper here either
creates a local mapping row, queues a no-op SyncJob, or writes a
SyncLog event — all internal.

Hard contract:

  - V1 is a SCHEMA + WORKFLOW preview, not a sync. The "test sync"
    button on the admin UI creates a `ChannelSyncJob` of `job_type=
    'test_noop'` and a matching `ChannelSyncLog` row. It does not
    contact any OTA.

  - All credential-bearing config lives in env vars / a future
    secret manager — NEVER in `ChannelConnection.config_json`. The
    column is for non-secret display config + mapping hints only.

  - The `bookings.external_source / external_reservation_ref` pair
    has a partial unique index (PostgreSQL) so duplicate imports
    are impossible at the DB level. The `link_external_ref()`
    helper checks first and returns a friendly error instead of
    triggering a constraint violation.

  - Every state-changing helper writes an ActivityLog row. Strict
    metadata whitelist documented in each call site.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional


# ── Whitelisted vocabularies ────────────────────────────────────────

# Booking.source values. The first 4 are first-party origins;
# the remainder are OTA channels (matching CHANNEL_NAMES below).
BOOKING_SOURCES = (
    'direct',
    'walk_in',
    'whatsapp',
    'booking_engine',
    'booking_com',
    'expedia',
    'agoda',
    'airbnb',
    'other',
)

# Channels we expect to integrate later. Same vocab used for both
# Booking.external_source and ChannelConnection.channel_name (the
# overlap is intentional — Booking.source can carry the channel name
# for OTA-originated bookings).
CHANNEL_NAMES = (
    'booking_com',
    'expedia',
    'agoda',
    'airbnb',
    'other',
)

CONNECTION_STATUSES = (
    'inactive',     # configured but not in use
    'sandbox',      # talking to OTA's sandbox endpoint
    'active',       # live production sync
    'error',        # fault state — operator must investigate
)

SYNC_JOB_TYPES = (
    'availability_push',
    'rate_push',
    'restriction_push',
    'reservation_import',
    'reservation_update',
    'cancellation_import',
    'full_resync',
    'test_noop',     # V1 staging-safe placeholder
)

SYNC_DIRECTIONS = ('outbound', 'inbound')

SYNC_STATUSES = (
    'queued', 'running', 'success', 'failed',
    'skipped', 'dead_lettered',
)


def is_valid_channel_name(name: Optional[str]) -> bool:
    return name in CHANNEL_NAMES


def is_valid_booking_source(name: Optional[str]) -> bool:
    return name in BOOKING_SOURCES


def is_valid_status(name: Optional[str]) -> bool:
    return name in CONNECTION_STATUSES


# ── Connections ─────────────────────────────────────────────────────

def create_connection(*,
        channel_name: str,
        property_id: Optional[int] = None,
        account_label: Optional[str] = None,
        notes: Optional[str] = None,
        status: str = 'inactive',
        user=None) -> dict:
    """Create a ChannelConnection. Caller commits.

    Refuses if the (property, channel_name) pair already exists —
    one connection per channel per property in V1.
    """
    from ..models import db, ChannelConnection
    from .audit import log_activity
    from .property import current_property_id

    if not is_valid_channel_name(channel_name):
        return {'ok': False,
                'error': f'channel_name must be one of '
                         f'{", ".join(CHANNEL_NAMES)}.',
                'connection': None}
    if not is_valid_status(status):
        return {'ok': False,
                'error': f'status must be one of '
                         f'{", ".join(CONNECTION_STATUSES)}.',
                'connection': None}

    pid = property_id if property_id is not None else current_property_id()
    if pid is None:
        return {'ok': False,
                'error': 'no active property — seed Property first.',
                'connection': None}

    # Enforce one-per-property-per-channel
    existing = (ChannelConnection.query
                .filter_by(property_id=pid, channel_name=channel_name)
                .first())
    if existing is not None:
        return {'ok': False,
                'error': (f'connection for {channel_name} already exists '
                          f'on this property.'),
                'connection': None}

    conn = ChannelConnection(
        property_id=pid,
        channel_name=channel_name,
        status=status,
        account_label=(account_label or '').strip()[:160] or None,
        notes=(notes or '').strip()[:2000] or None,
    )
    db.session.add(conn)
    db.session.flush()

    log_activity(
        'channel.connection_created',
        actor_user_id=getattr(user, 'id', None),
        description=(
            f'Channel connection {channel_name} ({status}) created '
            f'on property #{pid}.'
        ),
        metadata={
            'channel_connection_id': conn.id,
            'property_id':           pid,
            'channel_name':          channel_name,
            'status':                status,
        },
    )
    return {'ok': True, 'error': None, 'connection': conn}


def update_connection_status(conn, new_status: str, *, user=None) -> dict:
    """Flip a connection's status with audit. Refuses unknown statuses."""
    from .audit import log_activity

    if not is_valid_status(new_status):
        return {'ok': False,
                'error': f'status must be one of '
                         f'{", ".join(CONNECTION_STATUSES)}.'}
    if conn.status == new_status:
        return {'ok': True, 'error': None, 'no_op': True}
    old = conn.status
    conn.status = new_status

    log_activity(
        'channel.connection_updated',
        actor_user_id=getattr(user, 'id', None),
        description=(
            f'Channel connection {conn.channel_name} status: '
            f'{old} → {new_status}.'
        ),
        metadata={
            'channel_connection_id': conn.id,
            'property_id':           conn.property_id,
            'channel_name':          conn.channel_name,
            'old_status':            old,
            'new_status':            new_status,
        },
    )
    return {'ok': True, 'error': None, 'no_op': False}


# ── Mappings ────────────────────────────────────────────────────────

def create_room_map(*,
        connection,
        room_type_id: int,
        external_room_id: str,
        external_room_name_snapshot: Optional[str] = None,
        inventory_count_override: Optional[int] = None,
        notes: Optional[str] = None,
        user=None) -> dict:
    """Map an internal RoomType to an external room id on this channel.

    Refuses if either side already mapped — one-to-one within the
    connection.
    """
    from ..models import db, RoomType, ChannelRoomMap
    from .audit import log_activity

    if RoomType.query.get(room_type_id) is None:
        return {'ok': False, 'error': 'room_type_id not found.', 'map': None}

    ext = (external_room_id or '').strip()
    if not ext or len(ext) > 80:
        return {'ok': False,
                'error': 'external_room_id required (1–80 chars).',
                'map': None}

    # Either-side dedup
    by_type = (ChannelRoomMap.query
               .filter_by(channel_connection_id=connection.id,
                           room_type_id=room_type_id)
               .first())
    if by_type is not None:
        return {'ok': False,
                'error': (f'room type already mapped on this channel '
                          f'(external id {by_type.external_room_id!r}).'),
                'map': None}
    by_ext = (ChannelRoomMap.query
              .filter_by(channel_connection_id=connection.id,
                          external_room_id=ext)
              .first())
    if by_ext is not None:
        return {'ok': False,
                'error': f'external_room_id {ext!r} already mapped on '
                          f'this channel.',
                'map': None}

    m = ChannelRoomMap(
        channel_connection_id=connection.id,
        room_type_id=room_type_id,
        external_room_id=ext,
        external_room_name_snapshot=(external_room_name_snapshot or
                                      '').strip()[:160] or None,
        inventory_count_override=inventory_count_override,
        notes=(notes or '').strip()[:2000] or None,
        is_active=True,
    )
    db.session.add(m)
    db.session.flush()

    log_activity(
        'channel.mapping_created',
        actor_user_id=getattr(user, 'id', None),
        description=(
            f'Room map: type #{room_type_id} → '
            f'{connection.channel_name} ext {ext!r}.'
        ),
        metadata={
            'channel_connection_id': connection.id,
            'channel_name':          connection.channel_name,
            'entity_type':           'room_map',
            'entity_id':             m.id,
            'room_type_id':          room_type_id,
            'external_room_id':      ext,
        },
    )
    return {'ok': True, 'error': None, 'map': m}


def create_rate_plan_map(*,
        connection,
        rate_plan_id: int,
        external_rate_plan_id: str,
        external_rate_plan_name_snapshot: Optional[str] = None,
        meal_plan_external_id: Optional[str] = None,
        cancellation_policy_external_id: Optional[str] = None,
        notes: Optional[str] = None,
        user=None) -> dict:
    """Map an internal RatePlan to an external rate plan id."""
    from ..models import db, RatePlan, ChannelRatePlanMap
    from .audit import log_activity

    if RatePlan.query.get(rate_plan_id) is None:
        return {'ok': False, 'error': 'rate_plan_id not found.', 'map': None}

    ext = (external_rate_plan_id or '').strip()
    if not ext or len(ext) > 80:
        return {'ok': False,
                'error': 'external_rate_plan_id required (1–80 chars).',
                'map': None}

    # Dedup on plan / external
    by_plan = (ChannelRatePlanMap.query
               .filter_by(channel_connection_id=connection.id,
                           rate_plan_id=rate_plan_id)
               .first())
    if by_plan is not None:
        return {'ok': False,
                'error': 'rate plan already mapped on this channel.',
                'map': None}
    by_ext = (ChannelRatePlanMap.query
              .filter_by(channel_connection_id=connection.id,
                          external_rate_plan_id=ext)
              .first())
    if by_ext is not None:
        return {'ok': False,
                'error': f'external_rate_plan_id {ext!r} already mapped.',
                'map': None}

    m = ChannelRatePlanMap(
        channel_connection_id=connection.id,
        rate_plan_id=rate_plan_id,
        external_rate_plan_id=ext,
        external_rate_plan_name_snapshot=(external_rate_plan_name_snapshot
                                           or '').strip()[:160] or None,
        meal_plan_external_id=(meal_plan_external_id or '').strip() or None,
        cancellation_policy_external_id=(cancellation_policy_external_id
                                          or '').strip() or None,
        notes=(notes or '').strip()[:2000] or None,
        is_active=True,
    )
    db.session.add(m)
    db.session.flush()

    log_activity(
        'channel.mapping_created',
        actor_user_id=getattr(user, 'id', None),
        description=(
            f'Rate plan map: plan #{rate_plan_id} → '
            f'{connection.channel_name} ext {ext!r}.'
        ),
        metadata={
            'channel_connection_id': connection.id,
            'channel_name':          connection.channel_name,
            'entity_type':           'rate_plan_map',
            'entity_id':             m.id,
            'rate_plan_id':          rate_plan_id,
            'external_rate_plan_id': ext,
        },
    )
    return {'ok': True, 'error': None, 'map': m}


# ── External-reservation-ref linking ────────────────────────────────

def link_external_ref(booking, *,
        external_source: str,
        external_reservation_ref: str,
        user=None) -> dict:
    """Set Booking.external_source + external_reservation_ref +
    Booking.source = external_source. Refuses if the same
    (external_source, external_reservation_ref) pair already exists
    on a different booking — anti-duplicate-import guard.
    """
    from ..models import db, Booking
    from .audit import log_activity

    if not is_valid_channel_name(external_source):
        return {'ok': False,
                'error': f'external_source must be one of '
                         f'{", ".join(CHANNEL_NAMES)}.'}
    ref = (external_reservation_ref or '').strip()
    if not ref or len(ref) > 120:
        return {'ok': False,
                'error': 'external_reservation_ref required (1–120 chars).'}

    # Dedup at the DB level we can't guarantee on SQLite, so check
    # explicitly here. The PostgreSQL partial unique index also
    # enforces this, but Python-side check gives us a friendly error
    # instead of an IntegrityError.
    dup = (Booking.query
           .filter(Booking.external_source == external_source)
           .filter(Booking.external_reservation_ref == ref)
           .filter(Booking.id != booking.id)
           .first())
    if dup is not None:
        return {'ok': False,
                'error': (f'reservation {ref!r} from {external_source} '
                          f'is already linked to booking '
                          f'{dup.booking_ref}.')}

    booking.source = external_source
    booking.external_source = external_source
    booking.external_reservation_ref = ref

    log_activity(
        'booking.external_ref_linked',
        actor_user_id=getattr(user, 'id', None),
        booking=booking,
        description=(
            f'Booking {booking.booking_ref} linked to '
            f'{external_source} reservation {ref!r}.'
        ),
        metadata={
            'booking_id':                 booking.id,
            'booking_ref':                booking.booking_ref,
            'external_source':            external_source,
            'external_reservation_ref':   ref,
            'source':                     external_source,
        },
    )
    return {'ok': True, 'error': None}


# ── Sync jobs / logs (V1: test-noop only) ───────────────────────────

def enqueue_test_sync_job(connection, *,
        job_type: str = 'test_noop',
        direction: str = 'outbound',
        user=None) -> dict:
    """Create a ChannelSyncJob in 'queued' state, immediately run it
    as a no-op (V1 makes ZERO real OTA calls), mark it 'success',
    and write a matching ChannelSyncLog row. Returns the job + log.
    """
    from ..models import db, ChannelSyncJob, ChannelSyncLog
    from .audit import log_activity

    if job_type not in SYNC_JOB_TYPES:
        return {'ok': False,
                'error': f'job_type must be one of '
                         f'{", ".join(SYNC_JOB_TYPES)}.'}
    if direction not in SYNC_DIRECTIONS:
        return {'ok': False,
                'error': f'direction must be one of '
                         f'{", ".join(SYNC_DIRECTIONS)}.'}

    now = datetime.utcnow()
    job = ChannelSyncJob(
        channel_connection_id=connection.id,
        job_type=job_type,
        direction=direction,
        status='success',          # no-op: queued → success immediately
        attempt_count=1,
        started_at=now,
        completed_at=now,
        payload_summary='V1 test no-op — no external API call made.',
        requested_by_user_id=getattr(user, 'id', None),
    )
    db.session.add(job)
    db.session.flush()

    log_row = ChannelSyncLog(
        channel_connection_id=connection.id,
        sync_job_id=job.id,
        entity_type='test_noop',
        entity_id=None,
        direction=direction,
        action='simulated',
        status='skipped',
        message=(
            f'V1 channel-foundation test sync. No outbound '
            f'request made. job_type={job_type}.'
        ),
    )
    db.session.add(log_row)
    db.session.flush()

    log_activity(
        'channel.sync_job_created',
        actor_user_id=getattr(user, 'id', None),
        description=(
            f'Test sync job ({job_type}) on channel '
            f'{connection.channel_name} — V1 no-op.'
        ),
        metadata={
            'channel_connection_id': connection.id,
            'channel_name':          connection.channel_name,
            'sync_job_id':           job.id,
            'job_type':              job_type,
            'direction':             direction,
        },
    )
    return {'ok': True, 'error': None, 'job': job, 'log': log_row}
