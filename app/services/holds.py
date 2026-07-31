"""Booking Engine V2 — hold lifecycle + DB-level concurrency enforcement.

A hold reserves inventory at the room-TYPE level (never a specific room).
Two stages (spec §4): selection (15m) and pending (6h). Expiry is a STATE
transition written by `sweep_expired` with an audit entry — never a delete.

Concurrency (spec §3): every acquisition runs inside a transaction that
LOCKS the relevant room_type row(s) `FOR UPDATE` (ordered by type id →
deadlock-safe for multi-type), RE-CHECKS availability against the single
inventory query, then inserts and commits (releasing the lock). App-level
checks alone are insufficient; the row lock is the acceptance bar.

    NOTE on SQLite: `FOR UPDATE` is a no-op on SQLite, so the *race guarantee*
    only holds on Postgres. Logic tests may run on SQLite; the blocking race
    test (tests/test_bev2_race.py) runs against real Postgres.

TTLs are config (PropertySettings.selection_hold_ttl_minutes /
pending_hold_ttl_hours) — never hardcoded.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import sqlalchemy as sa

from . import inventory


# ── config ──────────────────────────────────────────────────────────

def get_ttls() -> dict:
    """Return {'selection_minutes': int, 'pending_hours': int} from settings."""
    from .property_settings import get_settings
    s = get_settings()
    return {
        'selection_minutes': int(getattr(s, 'selection_hold_ttl_minutes', 15) or 15),
        'pending_hours':     int(getattr(s, 'pending_hold_ttl_hours', 6) or 6),
    }


# ── concurrency primitive ───────────────────────────────────────────

def lock_types(session, type_ids):
    """Row-lock the given room_type rows FOR UPDATE, in ASCENDING id order so
    multi-type bookings always take locks in the same order (deadlock-safe).
    No-op semantics on SQLite (FOR UPDATE ignored)."""
    from ..models import RoomType
    ordered = sorted(set(int(t) for t in type_ids))
    if not ordered:
        return []
    stmt = (sa.select(RoomType.id)
            .where(RoomType.id.in_(ordered))
            .order_by(RoomType.id)
            .with_for_update())
    return list(session.execute(stmt).scalars().all())


# ── acquisition (the hard path) ─────────────────────────────────────

def _acquire(hold_type, room_type_id, check_in, check_out, qty, *,
             ttl, session_token=None, guest_name=None, contact=None,
             lead_guest_id=None, group_id=None, created_by=None, now=None):
    """Shared acquire: lock type -> re-check -> insert -> commit. Returns dict."""
    from ..models import db, Hold
    from .audit import log_activity
    now = now or datetime.utcnow()

    # 1. LOCK the type row (transaction begins / continues).
    lock_types(db.session, [room_type_id])
    # 2. RE-CHECK availability under the lock (single inventory query).
    avail = inventory.available_for_stay(room_type_id, check_in, check_out,
                                         qty, now=now)
    if not avail['ok']:
        db.session.rollback()          # release the lock; nothing inserted
        return {'ok': False, 'hold_id': None, 'reasons': avail['reasons'],
                'available': avail}
    # 3. INSERT the hold.
    hold = Hold(
        room_type_id=room_type_id, qty=qty,
        check_in_date=check_in, check_out_date=check_out,
        hold_type=hold_type, state='active', expires_at=now + ttl,
        session_token=session_token, guest_name=guest_name, contact=contact,
        lead_guest_id=lead_guest_id, booking_group_id=group_id,
        created_by_user_id=created_by,
    )
    db.session.add(hold)
    db.session.flush()
    log_activity(
        f'hold.{hold_type}_created',
        actor_user_id=created_by,
        new_value='active',
        description=(f'{hold_type.capitalize()} hold #{hold.id}: {qty}× type '
                     f'{room_type_id} for {check_in}..{check_out}.'),
        metadata={'hold_id': hold.id, 'room_type_id': room_type_id, 'qty': qty,
                  'hold_type': hold_type, 'expires_at': hold.expires_at.isoformat()},
    )
    # 4. COMMIT (releases the FOR UPDATE lock).
    db.session.commit()
    return {'ok': True, 'hold_id': hold.id, 'expires_at': hold.expires_at,
            'reasons': [], 'available': avail}


def acquire_selection_hold(room_type_id, check_in, check_out, qty=1, *,
                           session_token=None, created_by=None,
                           group_id=None, now=None):
    ttl = timedelta(minutes=get_ttls()['selection_minutes'])
    return _acquire('selection', room_type_id, check_in, check_out, qty,
                    ttl=ttl, session_token=session_token,
                    created_by=created_by, group_id=group_id, now=now)


def acquire_pending_hold(room_type_id, check_in, check_out, qty=1, *,
                         guest_name=None, contact=None, lead_guest_id=None,
                         group_id=None, created_by=None, now=None):
    ttl = timedelta(hours=get_ttls()['pending_hours'])
    return _acquire('pending', room_type_id, check_in, check_out, qty,
                    ttl=ttl, guest_name=guest_name, contact=contact,
                    lead_guest_id=lead_guest_id, group_id=group_id,
                    created_by=created_by, now=now)


def promote_to_pending(hold_id, *, guest_name=None, contact=None,
                       lead_guest_id=None, created_by=None, now=None):
    """Convert a live selection hold into a pending hold (booking submission).
    Keeps the same inventory reservation; just relabels + extends the TTL."""
    from ..models import db, Hold
    from .audit import log_activity
    now = now or datetime.utcnow()
    hold = Hold.query.get(hold_id)
    if hold is None or hold.hold_type != 'selection' or not hold.is_live(now=now):
        return {'ok': False, 'reasons': ['selection hold not live.']}
    hold.hold_type = 'pending'
    hold.expires_at = now + timedelta(hours=get_ttls()['pending_hours'])
    if guest_name:    hold.guest_name = guest_name
    if contact:       hold.contact = contact
    if lead_guest_id: hold.lead_guest_id = lead_guest_id
    log_activity('hold.promoted_to_pending', actor_user_id=created_by,
                 old_value='selection', new_value='pending',
                 description=f'Hold #{hold.id} promoted selection→pending.',
                 metadata={'hold_id': hold.id})
    db.session.commit()
    return {'ok': True, 'hold_id': hold.id, 'expires_at': hold.expires_at}


# ── admin actions (audited) ─────────────────────────────────────────

def release_hold(hold_id, *, reason, user_id=None, now=None):
    """Admin 'release now' — state -> released, never a delete. Reason required."""
    from ..models import db, Hold
    from .audit import log_activity
    now = now or datetime.utcnow()
    hold = Hold.query.get(hold_id)
    if hold is None:
        return {'ok': False, 'reasons': ['hold not found.']}
    if hold.state != 'active':
        return {'ok': False, 'reasons': [f'hold is {hold.state}, not active.']}
    if not (reason or '').strip():
        return {'ok': False, 'reasons': ['a release reason is required.']}
    hold.state = 'released'
    hold.released_reason = reason.strip()[:255]
    hold.released_at = now
    hold.released_by_user_id = user_id
    log_activity('hold.released', actor_user_id=user_id,
                 old_value='active', new_value='released',
                 description=f'Hold #{hold.id} released by admin.',
                 metadata={'hold_id': hold.id, 'reason': reason.strip()[:200]})
    db.session.commit()
    return {'ok': True, 'hold_id': hold.id}


def extend_hold(hold_id, *, minutes=None, hours=None, user_id=None, now=None):
    """Admin 'extend' — push expires_at out. Audited."""
    from ..models import db, Hold
    from .audit import log_activity
    now = now or datetime.utcnow()
    hold = Hold.query.get(hold_id)
    if hold is None or hold.state != 'active':
        return {'ok': False, 'reasons': ['hold not active.']}
    delta = timedelta(minutes=minutes or 0, hours=hours or 0)
    if delta.total_seconds() <= 0:
        # default: one more full TTL for the hold's stage
        ttls = get_ttls()
        delta = (timedelta(minutes=ttls['selection_minutes'])
                 if hold.hold_type == 'selection'
                 else timedelta(hours=ttls['pending_hours']))
    old = hold.expires_at
    hold.expires_at = max(hold.expires_at, now) + delta
    log_activity('hold.extended', actor_user_id=user_id,
                 old_value=old.isoformat()[:64], new_value=hold.expires_at.isoformat()[:64],
                 description=f'Hold #{hold.id} extended.',
                 metadata={'hold_id': hold.id})
    db.session.commit()
    return {'ok': True, 'hold_id': hold.id, 'expires_at': hold.expires_at}


# ── expiry sweep (status transition, never delete) ──────────────────

def sweep_expired(*, now=None):
    """Transition every active hold past its expiry to state='expired', with a
    per-hold audit entry, and write a 'hold.sweep' summary (which /admin/diag
    surfaces as last-run). Returns {'expired': n, 'ran_at': dt}."""
    from ..models import db, Hold
    from .audit import log_activity
    now = now or datetime.utcnow()
    due = (Hold.query
           .filter(Hold.state == 'active', Hold.expires_at <= now)
           .all())
    for hold in due:
        prev = hold.hold_type
        hold.state = 'expired'
        log_activity(f'hold.{prev}_expired', old_value='active', new_value='expired',
                     description=f'{prev.capitalize()} hold #{hold.id} expired (auto-release).',
                     metadata={'hold_id': hold.id, 'room_type_id': hold.room_type_id})
    log_activity('hold.sweep', actor_type='system',
                 description=f'Hold sweep: {len(due)} hold(s) expired.',
                 metadata={'expired': len(due), 'ran_at': now.isoformat()})
    db.session.commit()
    return {'expired': len(due), 'ran_at': now}


def last_sweep():
    """Latest 'hold.sweep' audit entry (for /admin/diag observability), or None."""
    from ..models import ActivityLog
    return (ActivityLog.query
            .filter(ActivityLog.action == 'hold.sweep')
            .order_by(ActivityLog.created_at.desc())
            .first())


def confirm_pending(hold_id, *, lead_guest=None, user_id=None, num_guests_each=1,
                    now=None):
    """Admin confirmation: convert a live PENDING hold into real confirmed
    booking(s) with best-fit rooms (spec §4/§5), mark the hold 'converted'.
    Uses the atomic group-creation primitive so multi-qty is one group + master
    folio. Returns the group_booking result augmented with hold_id."""
    from ..models import db, Hold
    from .audit import log_activity
    from . import group_booking
    now = now or datetime.utcnow()
    hold = Hold.query.get(hold_id)
    if hold is None or hold.hold_type != 'pending' or not hold.is_live(now=now):
        return {'ok': False, 'reasons': ['pending hold not live.']}

    guest = lead_guest
    if guest is None and hold.lead_guest_id:
        from ..models import Guest
        guest = Guest.query.get(hold.lead_guest_id)
    if guest is None:
        return {'ok': False, 'reasons': ['a lead guest is required to confirm.']}

    # Mark converted (flush, same transaction) BEFORE the group re-check so the
    # hold no longer counts against its OWN inventory — otherwise a pending hold
    # for the last room blocks its own confirmation. If group creation fails it
    # rolls back this flush too (hold reverts to active).
    hold.state = 'converted'
    hold.converted_at = now
    db.session.flush()

    # Source guest counts from the pending hold so the now-UNCONDITIONAL capacity
    # backstop in create_group_booking runs on this path too (Pepper Phase 0).
    # Coalesce null→1/0 (same as confirm_group) — prod holds carry these, but a
    # legacy null must degrade safely, never 500 or falsely reject.
    p_adults = hold.adults if hold.adults is not None else 1
    p_children = hold.children if hold.children is not None else 0
    res = group_booking.create_group_booking(
        [{'room_type_id': hold.room_type_id, 'qty': hold.qty}],
        hold.check_in_date, hold.check_out_date,
        lead_guest=guest, created_by=user_id,
        num_guests_each=num_guests_each, status='confirmed',
        adults=p_adults, children=p_children, now=now)
    if not res['ok']:
        db.session.rollback()          # also reverts the 'converted' flush
        return res

    # group_booking committed (hold='converted' + bookings). Record the linkage.
    hold = Hold.query.get(hold_id)
    hold.converted_group_id = res['group_id']
    log_activity('hold.confirmed', booking_id=res['booking_ids'][0],
                 actor_user_id=user_id, old_value='pending', new_value='converted',
                 description=f'Pending hold #{hold.id} confirmed → group {res["group_id"]}.',
                 metadata={'hold_id': hold.id, 'group_id': res['group_id'],
                           'booking_ids': str(res['booking_ids'])[:200]})
    db.session.commit()
    res['hold_id'] = hold.id
    return res


# ── Phase 2: multi-type portal group flow (session_token = group key) ─

def holds_for_session(session_token, *, hold_type=None, state='active', now=None):
    """All holds for a browser session (the portal 'group'). Optionally live."""
    from ..models import Hold
    q = Hold.query.filter(Hold.session_token == session_token)
    if hold_type:
        q = q.filter(Hold.hold_type == hold_type)
    if state:
        q = q.filter(Hold.state == state)
    if now is not None:
        q = q.filter(Hold.expires_at > now)
    return q.order_by(Hold.id).all()


def acquire_multi_selection(items, check_in, check_out, session_token, *,
                            guests=None, created_by=None, now=None):
    """Atomically acquire selection holds for MULTIPLE types (spec §B1): one
    hold row per type, same session_token. All-or-nothing: if any type is no
    longer available, the whole thing rolls back (friendly 'just missed it').

    `guests` (the search-bar count) carries onto the holds as `adults` so the
    guest step pre-fills it — the count is authoritative from search onward."""
    from ..models import db, Hold
    from .audit import log_activity
    now = now or datetime.utcnow()
    g_adults = max(1, int(guests)) if guests else 1
    norm = [{'room_type_id': int(i['room_type_id']), 'qty': int(i.get('qty', 1))}
            for i in items if int(i.get('qty', 1)) > 0]
    if not norm:
        return {'ok': False, 'reasons': ['select at least one room.']}
    try:
        lock_types(db.session, [i['room_type_id'] for i in norm])
        reasons = []
        for i in norm:
            av = inventory.available_for_stay(i['room_type_id'], check_in,
                                              check_out, i['qty'], now=now)
            if not av['ok']:
                reasons.append(f"type {i['room_type_id']} x{i['qty']}: "
                               + '; '.join(av['reasons']))
        if reasons:
            db.session.rollback()
            return {'ok': False, 'reasons': reasons}
        ttl = timedelta(minutes=get_ttls()['selection_minutes'])
        hold_ids = []
        for i in norm:
            h = Hold(room_type_id=i['room_type_id'], qty=i['qty'],
                     check_in_date=check_in, check_out_date=check_out,
                     hold_type='selection', state='active', expires_at=now + ttl,
                     session_token=session_token, created_by_user_id=created_by,
                     adults=g_adults, children=0)
            db.session.add(h)
            db.session.flush()
            hold_ids.append(h.id)
        log_activity('hold.selection_group_created', actor_type='guest',
                     new_value='active',
                     description=(f'Selection group ({len(hold_ids)} type(s)) '
                                  f'{check_in}..{check_out}.'),
                     metadata={'session_token': session_token[:32],
                               'hold_ids': str(hold_ids)[:200]})
        db.session.commit()
        return {'ok': True, 'hold_ids': hold_ids,
                'expires_at': now + ttl, 'reasons': []}
    except Exception as exc:            # noqa: BLE001
        db.session.rollback()
        return {'ok': False, 'reasons': [f'rolled back: {type(exc).__name__}']}


def promote_group_to_pending(session_token, *, guest_name=None, contact=None,
                             lead_guest_id=None, slip_filename=None,
                             slip_drive_id=None, adults=None, children=None,
                             created_by=None, now=None):
    """Submission: convert a live SELECTION group into ONE PENDING group (6h),
    attaching lead guest + optional slip. Server re-validates the holds are
    still live (never trust the client timer). Audited transition, no delete."""
    from ..models import db
    from .audit import log_activity
    now = now or datetime.utcnow()
    live = holds_for_session(session_token, hold_type='selection',
                             state='active', now=now)
    if not live:
        return {'ok': False, 'reasons': ['your hold expired — please start over.']}
    ttl = timedelta(hours=get_ttls()['pending_hours'])
    for h in live:
        h.hold_type = 'pending'
        h.expires_at = now + ttl
        if guest_name:    h.guest_name = guest_name
        if contact:       h.contact = contact
        if lead_guest_id: h.lead_guest_id = lead_guest_id
        if slip_filename: h.payment_slip_filename = slip_filename
        if slip_drive_id: h.payment_slip_drive_id = slip_drive_id
        if adults is not None:   h.adults = adults
        if children is not None: h.children = children
    log_activity('hold.group_pending', actor_type='guest',
                 old_value='selection', new_value='pending',
                 description=f'Selection group → pending ({len(live)} hold(s)).',
                 metadata={'session_token': session_token[:32],
                           'count': len(live), 'slip': bool(slip_filename)})
    # Pepper outbox — a new booking request (from ANY source) queues an alert in
    # THIS transaction (at-least-once delivery). booking_id is null here: the real
    # Booking is created later at admin confirm; the pending request carries the
    # public reference the guest quotes.
    from . import pepper_outbox
    ref = session_token[:8].upper()
    pepper_outbox.emit('booking.created', reference=ref,
                       payload={'source': 'portal', 'holds': len(live),
                                'guest_name': guest_name, 'slip': bool(slip_filename)})
    if slip_filename:
        pepper_outbox.emit('slip.uploaded', reference=ref,
                           payload={'source': 'portal', 'filename': slip_filename})
    db.session.commit()
    return {'ok': True, 'expires_at': now + ttl, 'count': len(live)}


def confirm_group(session_token, *, lead_guest=None, user_id=None,
                  num_guests_each=1, now=None):
    """Admin confirmation of a PENDING group → booking(s) + auto-assignment.
    Single hold of qty=1 produces a PLAIN booking (no group overhead, per spec
    §B3 intent); anything larger produces one group + master folio. The holds
    are marked converted (never deleted); the slip transfers to the booking."""
    from ..models import db, Guest
    from .audit import log_activity
    from . import group_booking
    now = now or datetime.utcnow()
    live = holds_for_session(session_token, hold_type='pending',
                             state='active', now=now)
    if not live:
        return {'ok': False, 'reasons': ['pending group not live (expired?).']}

    guest = lead_guest
    if guest is None and live[0].lead_guest_id:
        guest = Guest.query.get(live[0].lead_guest_id)
    if guest is None:
        return {'ok': False, 'reasons': ['a lead guest is required to confirm.']}

    ci, co = live[0].check_in_date, live[0].check_out_date
    slip_fn = next((h.payment_slip_filename for h in live if h.payment_slip_filename), None)
    slip_dr = next((h.payment_slip_drive_id for h in live if h.payment_slip_drive_id), None)
    total_rooms = sum(h.qty for h in live)
    # per-GROUP guest totals (captured on the holds; per-room split deferred)
    g_adults = live[0].adults if live[0].adults is not None else 1
    g_children = live[0].children if live[0].children is not None else 0

    # exclude the converting group from its own availability re-check
    for h in live:
        h.state = 'converted'
        h.converted_at = now
    db.session.flush()

    res = group_booking.create_group_booking(
        [{'room_type_id': h.room_type_id, 'qty': h.qty} for h in live],
        ci, co, lead_guest=guest, created_by=user_id,
        num_guests_each=num_guests_each, status='confirmed',
        force_group=(total_rooms > 1),
        adults=g_adults, children=g_children, now=now)
    if not res['ok']:
        db.session.rollback()
        return res

    # transfer slip + link holds to the created group/booking
    from ..models import Booking, Hold
    for bid in res['booking_ids']:
        b = Booking.query.get(bid)
        if slip_fn and not b.payment_slip_filename:
            b.payment_slip_filename = slip_fn
            b.payment_slip_drive_id = slip_dr
    for h in Hold.query.filter(Hold.session_token == session_token,
                               Hold.state == 'converted').all():
        h.converted_group_id = res.get('group_id')
    log_activity('hold.group_confirmed', actor_user_id=user_id,
                 booking_id=res['booking_ids'][0],
                 old_value='pending', new_value='converted',
                 description=(f'Pending group confirmed → '
                              f'{"group " + str(res.get("group_id")) if res.get("group_id") else "booking " + str(res["booking_ids"][0])}.'),
                 metadata={'session_token': session_token[:32],
                           'booking_ids': str(res['booking_ids'])[:200],
                           'group_id': res.get('group_id')})
    db.session.commit()
    return res


# ── read helpers (admin holds panel) ────────────────────────────────

def active_holds(*, now=None):
    """Live holds (selection + pending), soonest-expiring first, for the panel."""
    from ..models import Hold
    now = now or datetime.utcnow()
    return (Hold.query
            .filter(Hold.state == 'active', Hold.expires_at > now)
            .order_by(Hold.expires_at.asc())
            .all())
