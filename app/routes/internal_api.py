"""Pepper internal API (Phase 0).

Localhost-only surface for the Pepper Telegram bot. In production this blueprint
is served by a SEPARATE WSGI process (see internal_wsgi.py) bound to a UNIX
DOMAIN SOCKET owned by a dedicated UID — it is NOT registered on the public app,
so the internal routes are never reachable over the public TCP port.

Auth: a single static bearer token (``PEPPER_INTERNAL_TOKEN``, ≥32 bytes),
compared in constant time. The bot holds NO database credentials — every data
access, including the whitelist, goes through here.

All booking creation goes through ``group_booking.create_group_booking`` — the
same shared validation authority the admin console uses (single validation path).
"""

from __future__ import annotations

import hmac
import os
from datetime import date
from functools import wraps

from flask import Blueprint, request, jsonify, current_app

internal_api_bp = Blueprint('internal_api', __name__,
                            url_prefix='/api/internal/pepper')


def _expected_token():
    return (current_app.config.get('PEPPER_INTERNAL_TOKEN')
            or os.environ.get('PEPPER_INTERNAL_TOKEN') or '')


def require_bearer(fn):
    """Constant-time bearer check. 401 on missing/blank/mismatched token; 503 if
    the server has no token configured (fail closed, never open)."""
    @wraps(fn)
    def _wrap(*a, **kw):
        expected = _expected_token()
        if not expected:
            return jsonify(error='internal API not configured'), 503
        auth = request.headers.get('Authorization', '')
        token = auth[7:] if auth.startswith('Bearer ') else ''
        if not token or not hmac.compare_digest(token, expected):
            return jsonify(error='unauthorized'), 401
        return fn(*a, **kw)
    return _wrap


def _parse_date(s):
    return date.fromisoformat(s)


@internal_api_bp.get('/ping')
@require_bearer
def ping():
    return jsonify(ok=True, service='pepper-internal')


@internal_api_bp.get('/whitelist/<int:telegram_id>')
@require_bearer
def whitelist(telegram_id):
    """PII-free allow/deny for the bot's pre-auth check. Returns role only."""
    from ..models import PepperUser
    owner_id = os.environ.get('PEPPER_OWNER_ID')
    if owner_id and str(telegram_id) == str(owner_id):
        return jsonify(allowed=True, role='owner')
    u = PepperUser.query.get(telegram_id)
    if u is None or not u.is_active:
        return jsonify(allowed=False, role=None)
    return jsonify(allowed=True, role=u.role)


@internal_api_bp.get('/brand')
@require_bearer
def brand():
    """Bank/payment block for the flow's success message — property config, NOT
    guest data. Values come from branding.get_brand() (property_settings), never
    hardcoded in the bot."""
    from ..services.branding import get_brand
    b = get_brand()
    return jsonify(bank_name=b.get('bank_name', ''),
                   bank_account_name=b.get('bank_account_name', ''),
                   bank_account_number=b.get('bank_account_number', ''),
                   short_name=b.get('short_name', ''))


@internal_api_bp.get('/availability')
@require_bearer
def availability():
    from ..services import portal as portal_svc
    try:
        ci = _parse_date(request.args['check_in'])
        co = _parse_date(request.args['check_out'])
    except (KeyError, ValueError):
        return jsonify(error='check_in/check_out required (YYYY-MM-DD)'), 400
    guests = int(request.args.get('guests', 1) or 1)
    cards = portal_svc.search(ci, co, guests)
    return jsonify(check_in=ci.isoformat(), check_out=co.isoformat(),
                   rooms=[{'room_type_id': c['room_type'].id,
                           'name': c['room_type'].name,
                           'available_qty': c['available_qty'],
                           'sold_out': c['sold_out'],
                           'price_total_per_room': c['price_total_per_room'],
                           'price_per_night': c['price_per_night']}
                          for c in cards])


@internal_api_bp.post('/quote')
@require_bearer
def quote():
    from ..services import inventory, occupancy
    data = request.get_json(silent=True) or {}
    try:
        ci = _parse_date(data['check_in']); co = _parse_date(data['check_out'])
        items = [{'room_type_id': int(i['room_type_id']), 'qty': int(i['qty'])}
                 for i in data['items']]
        guests = int(data.get('guests', 1) or 1)
    except (KeyError, ValueError, TypeError):
        return jsonify(error='items, check_in, check_out required'), 400
    nights = max(1, (co - ci).days)
    room_total = sum(inventory.price_stay(i['room_type_id'], ci, co)['total']
                     * i['qty'] for i in items)
    occ = occupancy.compute(items, guests, nights)
    return jsonify(nights=nights, room_total=room_total,
                   extra_person_fee=occ['fee_total'],
                   over_capacity=occ['over_capacity'],
                   total=room_total + occ['fee_total'])


@internal_api_bp.post('/bookings')
@require_bearer
def create_booking():
    """One-shot create via the shared authority. Guest counts and nationality are
    REQUIRED here — the API rejects (400) rather than letting a default slip
    through (parity with the portal)."""
    from ..models import db
    from ..services import group_booking
    data = request.get_json(silent=True) or {}
    try:
        ci = _parse_date(data['check_in']); co = _parse_date(data['check_out'])
        items = [{'room_type_id': int(i['room_type_id']), 'qty': int(i['qty'])}
                 for i in data['items']]
        guest = data['guest']
        adults = int(data['adults'])
    except (KeyError, ValueError, TypeError):
        return jsonify(error='items, check_in, check_out, guest, adults required'), 400
    children = int(data.get('children', 0) or 0)
    if not (guest.get('nationality') or '').strip():
        return jsonify(error='guest.nationality is required'), 400
    # Payment method: canonical cashiering vocabulary; default bank_transfer
    # (the historical slip path). Stamped on the booking so finance can split
    # cash vs transfer AND the alert can pick cash-received-vs-slip.
    pm = (data.get('payment_method') or 'bank_transfer').strip().lower()
    if pm not in ('cash', 'bank_transfer'):
        return jsonify(error="payment_method must be 'cash' or 'bank_transfer'"), 400

    res = group_booking.create_group_booking(
        items, ci, co, lead_guest=guest, adults=adults, children=children,
        created_by=None, status=data.get('status', 'confirmed'),
        force_group=bool(data.get('force_group', False)),
        payment_method=pm)
    if not res['ok']:
        return jsonify(ok=False, reasons=res['reasons']), 409
    # Outbox alert joins THIS request's transaction (the create committed inside
    # create_group_booking; emit + commit here so the alert is durable).
    # payment_method rides in the payload so _assemble_alert can branch the alert
    # (cash-received button vs slip flow) WITHOUT a schema read.
    from ..services import pepper_outbox
    bid = res['booking_ids'][0]
    pepper_outbox.emit('booking.created', booking_id=bid,
                       payload={'source': 'bot', 'group_id': res['group_id'],
                                'payment_method': pm})
    db.session.commit()
    return jsonify(ok=True, group_id=res['group_id'],
                   booking_ids=res['booking_ids']), 201


@internal_api_bp.get('/bookings/<int:booking_id>')
@require_bearer
def booking_detail(booking_id):
    from ..models import Booking
    b = Booking.query.get(booking_id)
    if b is None:
        return jsonify(error='not found'), 404
    return jsonify(id=b.id, booking_ref=b.booking_ref, status=b.status,
                   check_in=b.check_in_date.isoformat(),
                   check_out=b.check_out_date.isoformat(),
                   adults=b.adults, children=b.children, num_guests=b.num_guests,
                   total_amount=float(b.total_amount or 0))


_MDV = {'mdv', 'maldivian', 'mv', 'maldives'}


def _green_tax(nationality):
    if not nationality:
        return 'unknown'
    return 'exempt' if nationality.strip().lower() in _MDV else 'applies'


def _pending_holds_for_ref(reference):
    # Include 'expired' (swept) pending holds too, so a lapsed request still
    # renders a truthful (EXPIRED-marked) alert instead of "details unavailable".
    from ..models import Hold
    from sqlalchemy import func
    return (Hold.query.filter(
        Hold.hold_type == 'pending', Hold.state.in_(('active', 'expired')),
        func.upper(func.substr(Hold.session_token, 1, 8)) == reference)
        .order_by(Hold.id).all())


def _event_payload(ev):
    """The outbox row's JSON payload as a dict (empty on absent/bad JSON)."""
    import json
    try:
        return json.loads(ev.payload_json or '{}')
    except (ValueError, TypeError):
        return {}


def _assemble_alert(ev):
    """Render the §7.2 alert fields server-side (bot stays PMS-logic-free).
    Deliberately OMITS id/passport numbers (PII discipline)."""
    from ..models import Booking, RoomType
    from ..services import inventory, occupancy
    if ev.booking_id:
        b = Booking.query.get(ev.booking_id)
        if b is None:
            return None
        g = b.guest
        nat = g.nationality if g else None
        # payment_method rides in the outbox payload (durable branch, no schema
        # dependency); fall back to the booking column if the payload predates it.
        pm = (_event_payload(ev).get('payment_method')
              or b.payment_method or 'bank_transfer')
        return {'source': 'bot', 'ref': b.booking_ref, 'booking_id': b.id,
                'guest_name': (g.full_name if g else '—') or '—',
                'nationality': nat or '—', 'green_tax': _green_tax(nat),
                'check_in': b.check_in_date.isoformat(),
                'check_out': b.check_out_date.isoformat(),
                'nights': (b.check_out_date - b.check_in_date).days,
                'rooms': '—', 'adults': b.adults, 'children': b.children,
                'total': float(b.total_amount or 0), 'deadline': None,
                'payment_method': pm,
                # a VALID slip = filename on file AND not soft-rejected (mirror
                # the hold branch), so a rejected booking slip re-reads as
                # 'awaiting slip' rather than falsely 'uploaded'.
                'has_slip': bool(b.payment_slip_filename and not b.slip_rejected_at),
                'slip_rejected': bool(b.slip_rejected_at)}
    if ev.reference:
        holds = _pending_holds_for_ref(ev.reference)
        if not holds:
            return None
        h0 = holds[0]
        g = h0.lead_guest
        ci, co = h0.check_in_date, h0.check_out_date
        nights = max(1, (co - ci).days)
        items = [{'room_type_id': h.room_type_id, 'qty': h.qty} for h in holds]
        room_total = sum(inventory.price_stay(h.room_type_id, ci, co)['total'] * h.qty
                         for h in holds)
        adults = h0.adults if h0.adults is not None else 1
        children = h0.children if h0.children is not None else 0
        occ = occupancy.compute(items, adults + children, nights)
        rooms = ', '.join(f"{h.qty}× {RoomType.query.get(h.room_type_id).name}"
                          for h in holds)
        from datetime import datetime
        nat = g.nationality if g else None
        deadline = min(h.expires_at for h in holds)
        return {'source': 'portal', 'ref': ev.reference,
                'guest_name': (g.full_name if g else h0.guest_name) or '—',
                'nationality': nat or '—', 'green_tax': _green_tax(nat),
                'check_in': ci.isoformat(), 'check_out': co.isoformat(),
                'nights': nights, 'rooms': rooms,
                'adults': adults, 'children': children,
                'total': round(room_total + occ['fee_total'], 2),
                'deadline': deadline.isoformat(),
                'expired': deadline <= datetime.utcnow(),
                # a VALID slip = filename on file AND not soft-rejected
                'has_slip': any(h.payment_slip_filename and not h.slip_rejected_at
                                for h in holds),
                'slip_rejected': any(h.slip_rejected_at for h in holds)}
    return None


@internal_api_bp.get('/outbox')
@require_bearer
def outbox_list():
    from ..models import PepperOutbox
    q = PepperOutbox.query
    if request.args.get('undelivered') in ('1', 'true', 'yes'):
        q = q.filter(PepperOutbox.delivered_at.is_(None))
    rows = q.order_by(PepperOutbox.id).limit(100).all()
    return jsonify(events=[{'id': r.id, 'event_type': r.event_type,
                            'booking_id': r.booking_id, 'reference': r.reference,
                            'payload': r.payload_json,
                            'created_at': r.created_at.isoformat(),
                            'alert': _assemble_alert(r)}
                           for r in rows])


@internal_api_bp.get('/slip')
@require_bearer
def slip():
    """Return the payment-slip image bytes for a pending hold (by reference) or a
    booking (by id), so the bot can re-upload it to Telegram (no PMS session)."""
    import os
    from flask import send_file
    from ..models import Booking
    ref = request.args.get('reference')
    bid = request.args.get('booking_id')
    filename = None
    if ref:
        h = next((x for x in _pending_holds_for_ref(ref)
                  if x.payment_slip_filename), None)
        filename = h.payment_slip_filename if h else None
    elif bid:
        b = Booking.query.get(int(bid))
        filename = b.payment_slip_filename if b else None
    if not filename:
        return jsonify(error='no slip on file'), 404
    # Uploads live in the `app` PACKAGE dir (app/uploads), NOT the running app's
    # root_path — the internal API is served by a separate WSGI app whose
    # root_path is the repo root, so current_app.root_path would be wrong here.
    upload_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads')
    path = os.path.join(upload_dir, filename)
    if not os.path.exists(path):
        return jsonify(error='slip file missing'), 404
    return send_file(path)


def _last_actor(reference, actions):
    """Actor name from the most recent matching pepper audit for a reference —
    for the loser's 'already handled by X' message."""
    return _last_actor_by('reference', reference, actions)


def _last_actor_by(match_key, match_val, actions):
    """Actor name from the most recent pepper audit whose metadata[match_key]
    equals match_val — the target-kind-agnostic form (reference for holds,
    booking_id for bookings). For the loser's 'already handled by X' message."""
    import json
    from ..models import ActivityLog
    rows = (ActivityLog.query.filter(ActivityLog.action.in_(actions))
            .order_by(ActivityLog.id.desc()).limit(30).all())
    for r in rows:
        try:
            meta = json.loads(r.metadata_json or '{}')
        except ValueError:
            meta = {}
        if meta.get(match_key) == match_val:
            return meta.get('actor_name') or 'someone'
    return 'someone'


def _lock_active_pending(reference):
    """Row-lock (FOR UPDATE) the active pending holds for a reference — the
    DB-level idempotency claim. On Postgres two racing taps serialize here and
    only one sees rows 'active'; the loser sees them already converted/released."""
    from sqlalchemy import func
    from ..models import Hold
    return (Hold.query.filter(
        func.upper(func.substr(Hold.session_token, 1, 8)) == reference,
        Hold.hold_type == 'pending', Hold.state == 'active')
        .with_for_update().all())


@internal_api_bp.post('/holds/verify')
@require_bearer
def holds_verify():
    """✅ Verify = confirm the pending hold (creates the Booking). Idempotent:
    exactly one caller wins; the loser gets who already confirmed."""
    from ..models import db
    from ..services import holds as holds_svc
    from ..services.audit import log_activity
    data = request.get_json(silent=True) or {}
    reference = (data.get('reference') or '').strip()
    actor_name = data.get('actor_name') or 'staff'
    if not reference:
        return jsonify(error='reference required'), 400
    rows = _lock_active_pending(reference)
    if not rows:                                     # someone else already acted
        return jsonify(ok=False, already=True,
                       by=_last_actor(reference, ['pepper.hold_confirmed'])), 409
    # Slip guard: never confirm a hold without a valid (non-rejected) slip.
    if not any(h.payment_slip_filename and not h.slip_rejected_at for h in rows):
        return jsonify(ok=False, no_slip=True,
                       reasons=['no valid slip on record — cannot verify']), 409
    res = holds_svc.confirm_group(rows[0].session_token, user_id=None)
    if not res.get('ok'):
        db.session.rollback()
        return jsonify(ok=False, reasons=res.get('reasons', ['confirm failed'])), 409
    log_activity('pepper.hold_confirmed', actor_type='ai_agent', new_value='confirmed',
                 booking_id=(res['booking_ids'][0] if res.get('booking_ids') else None),
                 description=f'Hold {reference} confirmed via Pepper by {actor_name}.',
                 metadata={'reference': reference, 'actor_name': actor_name,
                           'telegram_id': data.get('actor_id')})
    db.session.commit()
    return jsonify(ok=True, by=actor_name, booking_ids=res.get('booking_ids'))


@internal_api_bp.post('/holds/reject')
@require_bearer
def holds_reject():
    """❌ Reject = release the pending hold with a reason. Idempotent."""
    from datetime import datetime
    from ..models import db
    from ..services.audit import log_activity
    data = request.get_json(silent=True) or {}
    reference = (data.get('reference') or '').strip()
    actor_name = data.get('actor_name') or 'staff'
    reason = (data.get('reason') or '').strip() or 'no reason given'
    if not reference:
        return jsonify(error='reference required'), 400
    rows = _lock_active_pending(reference)
    if not rows:                                     # confirmed already -> can't reject
        return jsonify(ok=False, already=True,
                       by=_last_actor(reference,
                                      ['pepper.hold_confirmed', 'pepper.slip_rejected'])), 409
    # SOFT reject: mark the slip rejected + reason; the hold stays 'active' on its
    # normal expiry (release stays PMS-only), and the file is KEPT (never deleted /
    # overwritten) so the guest can re-upload a new one.
    now = datetime.utcnow()
    for h in rows:
        h.slip_rejected_at = now
        h.slip_rejected_reason = reason[:255]
    log_activity('pepper.slip_rejected', actor_type='ai_agent', new_value='slip_rejected',
                 description=f'Slip for hold {reference} rejected via Pepper by '
                             f'{actor_name}: {reason}',
                 metadata={'reference': reference, 'actor_name': actor_name,
                           'reason': reason, 'telegram_id': data.get('actor_id')})
    db.session.commit()
    return jsonify(ok=True, by=actor_name)


@internal_api_bp.get('/holds/state')
@require_bearer
def holds_state():
    """Authoritative current disposition for a reference. The bot's ↩︎ Cancel and
    reject-timeout re-arm decision goes through THIS (the same truth as
    verify/reject) so it never re-arms ✅/❌ on a hold someone already confirmed.

      pending       -> active pending hold WITH a valid slip  (armable ✅/❌)
      slip_rejected -> active pending hold, slip soft-rejected (not armable)
      awaiting_slip -> active pending hold, no slip yet        (not armable)
      expired       -> holds lapsed
      confirmed     -> holds consumed by a confirm (by whom, if known)
    """
    from datetime import datetime
    reference = (request.args.get('reference') or '').strip()
    if not reference:
        return jsonify(error='reference required'), 400
    rows = _pending_holds_for_ref(reference)          # active + expired pending
    now = datetime.utcnow()
    active = [h for h in rows if h.state == 'active' and h.expires_at > now]
    if active:
        if any(h.payment_slip_filename and not h.slip_rejected_at for h in active):
            return jsonify(ok=True, state='pending', armable=True)
        if any(h.slip_rejected_at for h in active):
            reason = next((h.slip_rejected_reason for h in active
                           if h.slip_rejected_at), None)
            return jsonify(ok=True, state='slip_rejected', armable=False, reason=reason)
        return jsonify(ok=True, state='awaiting_slip', armable=False)
    if rows:                                           # rows exist but none active
        return jsonify(ok=True, state='expired', armable=False)
    return jsonify(ok=True, state='confirmed', armable=False,   # consumed by confirm
                   by=_last_actor(reference, ['pepper.hold_confirmed']))


# ── Bot-created booking slip flow (D-slip) ──────────────────────────────────
# Mirror of the hold machinery above, targeted at a BOOKING that the guided
# /newbooking flow created as status='pending_verification'. The bot's own
# booking flows through the SAME §7 slip → verify/reject path as a portal hold —
# only the target differs (Booking vs Hold).


def _uploads_dir():
    """The `app/uploads` package dir — the same dir the GET /slip endpoint reads
    from. NOT current_app.root_path (the internal WSGI app's root_path is the
    repo root, so root_path/uploads would be wrong here)."""
    import os
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads')


def _save_booking_slip(file):
    """Hardened save for a booking slip: reuse the Phase 0 allowlist + size cap
    from public._save_file, but write to app/uploads (matching GET /slip) and
    use a fresh unique filename (supersede-never-delete). Returns (name, drive_id).
    Raises UploadRejected on a disallowed/empty/oversized file."""
    import os
    import uuid
    from .public import (_upload_ext, ALLOWED_UPLOAD_EXTS, MAX_UPLOAD_BYTES,
                         UploadRejected)
    ext = _upload_ext(getattr(file, 'filename', None))
    if ext not in ALLOWED_UPLOAD_EXTS:
        raise UploadRejected(
            'Unsupported file type — upload an image (JPG/PNG/…) or PDF.')
    file_bytes = file.read()
    if not file_bytes:
        raise UploadRejected('That file was empty — please re-upload.')
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise UploadRejected('File too large — the maximum is 10 MB.')
    from ..services.drive import upload_file as drive_upload
    name = f'pepperslip_{uuid.uuid4().hex[:10]}.{ext}'
    upload_dir = _uploads_dir()
    os.makedirs(upload_dir, exist_ok=True)
    with open(os.path.join(upload_dir, name), 'wb') as fh:
        fh.write(file_bytes)
    return name, drive_upload(file_bytes, name, 'payment_slips')


@internal_api_bp.post('/bookings/<int:booking_id>/slip')
@require_bearer
def booking_slip_attach(booking_id):
    """Attach a payment slip to a bot-created booking. Hardened save,
    supersede-never-delete (fresh filename; the old file is KEPT), clears any
    prior soft-reject, and emits `slip.uploaded` (booking-targeted) in the SAME
    transaction so the alert is durable."""
    from ..models import db, Booking
    from ..services import pepper_outbox
    from .public import UploadRejected
    b = Booking.query.get(booking_id)
    if b is None:
        return jsonify(error='not found'), 404
    file = request.files.get('slip') or request.files.get('file')
    if file is None:
        return jsonify(error='no slip file (multipart field "slip")'), 400
    try:
        name, drive_id = _save_booking_slip(file)
    except UploadRejected as exc:
        return jsonify(ok=False, error=str(exc)), 400
    # Supersede: point at the new file, keep the old one on disk (never delete /
    # overwrite). Clear any earlier rejection so the slip re-arms clean.
    b.payment_slip_filename = name
    b.payment_slip_drive_id = drive_id
    b.slip_rejected_at = None
    b.slip_rejected_reason = None
    pepper_outbox.emit('slip.uploaded', booking_id=b.id,
                       payload={'source': 'bot'})
    db.session.commit()
    return jsonify(ok=True, booking_id=b.id, filename=name)


def _lock_booking(booking_id):
    """Row-lock (FOR UPDATE) a single booking — the DB-level idempotency claim
    for verify. On Postgres two racing taps serialize here; only one sees it
    still 'pending_verification'. (FOR UPDATE no-ops on SQLite — the status
    check still makes verify idempotent single-threaded.)"""
    from ..models import Booking
    return (Booking.query.filter(Booking.id == booking_id)
            .with_for_update().first())


@internal_api_bp.post('/bookings/<int:booking_id>/verify')
@require_bearer
def booking_verify(booking_id):
    """✅ Verify a bot-created booking: pending_verification → confirmed.
    DB-idempotent: exactly one caller wins; the loser gets who already verified.

    Two modes, both manager-gated at the bot and idempotent here:
      * bank transfer (default) — the SLIP GUARD applies: refuses a booking with
        no valid (non-rejected) slip. This is the 💵 Cash-received guard's mirror.
      * cash (``cash: true`` / ``require_slip: false``) — a walk-in paying cash has
        no slip and no slip is ever coming, so cash mode SKIPS the slip guard and
        records method=cash. This closes the un-verifiable-forever leak (a cash
        booking would otherwise sit pending_verification with nothing to verify).
    """
    from ..models import db
    from ..services.audit import log_activity
    data = request.get_json(silent=True) or {}
    actor_name = data.get('actor_name') or 'staff'
    # cash mode: explicit cash flag OR require_slip=false. Default = slip required.
    cash = bool(data.get('cash')) or (data.get('require_slip') is False)
    b = _lock_booking(booking_id)
    if b is None:
        return jsonify(error='not found'), 404
    if b.status != 'pending_verification':
        # Already confirmed (or otherwise moved on) — the loser of a race.
        return jsonify(ok=False, already=True,
                       by=_last_actor_by('booking_id', booking_id,
                                         ['pepper.booking_confirmed'])), 409
    if not cash and not (b.payment_slip_filename and not b.slip_rejected_at):
        return jsonify(ok=False, no_slip=True,
                       reasons=['no valid slip on record — cannot verify']), 409
    method = 'cash' if cash else (b.payment_method or 'bank_transfer')
    b.status = 'confirmed'
    how = 'cash received' if cash else 'slip verified'
    log_activity('pepper.booking_confirmed', actor_type='ai_agent',
                 booking_id=b.id, old_value='pending_verification',
                 new_value='confirmed',
                 description=(f'Booking {b.booking_ref} confirmed via Pepper by '
                              f'{actor_name} ({how}).'),
                 metadata={'booking_id': booking_id, 'actor_name': actor_name,
                           'method': method, 'telegram_id': data.get('actor_id')})
    db.session.commit()
    return jsonify(ok=True, by=actor_name, booking_id=b.id, method=method)


@internal_api_bp.post('/bookings/<int:booking_id>/reject')
@require_bearer
def booking_reject(booking_id):
    """❌ Reject a bot-created booking's slip: SOFT reject — mark
    slip_rejected_at/reason, KEEP status='pending_verification' (so staff can
    re-attach) and KEEP the file (never deleted). Idempotent-ish: a booking that
    already left pending_verification (confirmed) can't be rejected."""
    from datetime import datetime
    from ..models import db
    from ..services.audit import log_activity
    data = request.get_json(silent=True) or {}
    actor_name = data.get('actor_name') or 'staff'
    reason = (data.get('reason') or '').strip() or 'no reason given'
    b = _lock_booking(booking_id)
    if b is None:
        return jsonify(error='not found'), 404
    if b.status != 'pending_verification':
        return jsonify(ok=False, already=True,
                       by=_last_actor_by('booking_id', booking_id,
                                         ['pepper.booking_confirmed',
                                          'pepper.slip_rejected'])), 409
    b.slip_rejected_at = datetime.utcnow()
    b.slip_rejected_reason = reason[:255]
    log_activity('pepper.slip_rejected', actor_type='ai_agent',
                 new_value='slip_rejected', booking_id=b.id,
                 description=(f'Slip for booking {b.booking_ref} rejected via '
                              f'Pepper by {actor_name}: {reason}'),
                 metadata={'booking_id': booking_id, 'actor_name': actor_name,
                           'reason': reason, 'telegram_id': data.get('actor_id')})
    db.session.commit()
    return jsonify(ok=True, by=actor_name)


@internal_api_bp.get('/bookings/<int:booking_id>/state')
@require_bearer
def booking_state(booking_id):
    """Authoritative disposition for a bot-created booking — the ↩︎ Cancel /
    reject-timeout re-arm authority AND the anti-stale gate for 💵 Cash received
    (a tap on an OLD alert routes through here first). ``payment_method`` is
    always returned so the caller knows the confirm MODE (cash vs slip).

      pending       -> ARMABLE. bank: pending_verification WITH a valid slip;
                       CASH: pending_verification (no slip is expected for cash).
      slip_rejected -> pending_verification, slip soft-rejected (not armable)
      awaiting_slip -> pending_verification bank booking, no slip yet (not armable)
      confirmed     -> already confirmed (by whom, if known)    (not armable)
      cancelled     -> booking cancelled                        (not armable)
      other         -> any other status                         (not armable)
    """
    from ..models import Booking
    b = Booking.query.get(booking_id)
    if b is None:
        return jsonify(error='not found'), 404
    pm = b.payment_method or 'bank_transfer'
    is_cash = (pm == 'cash')
    if b.status == 'pending_verification':
        # Cash has no slip and none is coming, so pending_verification is directly
        # armable; a soft-reject (unusual for cash) still blocks. Bank needs a
        # valid slip to be armable.
        if is_cash and not b.slip_rejected_at:
            return jsonify(ok=True, state='pending', armable=True, payment_method=pm)
        if b.payment_slip_filename and not b.slip_rejected_at:
            return jsonify(ok=True, state='pending', armable=True, payment_method=pm)
        if b.slip_rejected_at:
            return jsonify(ok=True, state='slip_rejected', armable=False,
                           reason=b.slip_rejected_reason, payment_method=pm)
        return jsonify(ok=True, state='awaiting_slip', armable=False,
                       payment_method=pm)
    if b.status == 'confirmed':
        return jsonify(ok=True, state='confirmed', armable=False, payment_method=pm,
                       by=_last_actor_by('booking_id', booking_id,
                                         ['pepper.booking_confirmed']))
    # cancelled / checked_in / … — the honest current status, never armable.
    return jsonify(ok=True, state=b.status, armable=False, payment_method=pm)


@internal_api_bp.post('/whitelist')
@require_bearer
def whitelist_add():
    """Owner-driven staff onboarding (/authorize). Upsert a pepper_users row."""
    from ..models import db, PepperUser
    data = request.get_json(silent=True) or {}
    try:
        tid = int(data['telegram_id'])
    except (KeyError, ValueError, TypeError):
        return jsonify(error='telegram_id required (integer)'), 400
    role = (data.get('role') or 'staff').lower()
    if role not in ('manager', 'staff'):
        return jsonify(error='role must be manager or staff'), 400
    u = PepperUser.query.get(tid)
    if u is None:
        u = PepperUser(telegram_id=tid)
        db.session.add(u)
    u.role = role
    u.display_name = (data.get('display_name') or u.display_name or '')[:120]
    u.added_by = data.get('added_by')
    u.revoked_at = None                              # (re)activate
    db.session.commit()
    return jsonify(ok=True, telegram_id=tid, role=role, display_name=u.display_name)


@internal_api_bp.post('/whitelist/<int:telegram_id>/revoke')
@require_bearer
def whitelist_revoke(telegram_id):
    from datetime import datetime
    from ..models import db, PepperUser
    u = PepperUser.query.get(telegram_id)
    if u is None:
        return jsonify(ok=False, not_found=True), 404
    if u.revoked_at is None:
        u.revoked_at = datetime.utcnow()
        db.session.commit()
    return jsonify(ok=True, telegram_id=telegram_id)


# ── Flow snapshots (restart recovery) ───────────────────────────────────────
# The bot holds no DB creds, so the per-user /newbooking flow snapshot
# (pepper_flows) round-trips through here. One row per active flow; the bot
# writes on each step and deletes on confirm/cancel, and reads all open flows on
# startup to resume them. Conversational state only — the durable booking state
# lives in bookings/holds.


@internal_api_bp.put('/flows/<int:telegram_id>')
@require_bearer
def flow_upsert(telegram_id):
    from ..models import db, PepperFlow
    data = request.get_json(silent=True) or {}
    f = PepperFlow.query.get(telegram_id)
    if f is None:
        f = PepperFlow(telegram_id=telegram_id)
        db.session.add(f)
    f.chat_id = data.get('chat_id')
    f.thread_id = data.get('thread_id')
    f.step = (data.get('step') or None)
    f.draft_json = data.get('draft_json')
    db.session.commit()
    return jsonify(ok=True, telegram_id=telegram_id)


@internal_api_bp.delete('/flows/<int:telegram_id>')
@require_bearer
def flow_delete(telegram_id):
    from ..models import db, PepperFlow
    f = PepperFlow.query.get(telegram_id)
    if f is not None:
        db.session.delete(f)
        db.session.commit()
    return jsonify(ok=True, telegram_id=telegram_id)


@internal_api_bp.get('/flows')
@require_bearer
def flow_list():
    """All open flow snapshots — the bot reads this once on startup to resume."""
    from ..models import PepperFlow
    rows = PepperFlow.query.order_by(PepperFlow.telegram_id).all()
    return jsonify(flows=[{'telegram_id': f.telegram_id, 'chat_id': f.chat_id,
                           'thread_id': f.thread_id, 'step': f.step,
                           'draft_json': f.draft_json,
                           'updated_at': f.updated_at.isoformat()
                           if f.updated_at else None}
                          for f in rows])


@internal_api_bp.post('/outbox/<int:row_id>/delivered')
@require_bearer
def outbox_mark_delivered(row_id):
    from ..models import db, PepperOutbox
    from datetime import datetime
    r = PepperOutbox.query.get(row_id)
    if r is None:
        return jsonify(error='not found'), 404
    if r.delivered_at is None:
        r.delivered_at = datetime.utcnow()
        db.session.commit()
    return jsonify(ok=True, id=r.id, delivered_at=r.delivered_at.isoformat())
