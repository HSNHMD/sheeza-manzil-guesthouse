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

    res = group_booking.create_group_booking(
        items, ci, co, lead_guest=guest, adults=adults, children=children,
        created_by=None, status=data.get('status', 'confirmed'),
        force_group=bool(data.get('force_group', False)))
    if not res['ok']:
        return jsonify(ok=False, reasons=res['reasons']), 409
    # Outbox alert joins THIS request's transaction (the create committed inside
    # create_group_booking; emit + commit here so the alert is durable).
    from ..services import pepper_outbox
    bid = res['booking_ids'][0]
    pepper_outbox.emit('booking.created', booking_id=bid,
                       payload={'source': 'bot', 'group_id': res['group_id']})
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
        return {'source': 'bot', 'ref': b.booking_ref,
                'guest_name': (g.full_name if g else '—') or '—',
                'nationality': nat or '—', 'green_tax': _green_tax(nat),
                'check_in': b.check_in_date.isoformat(),
                'check_out': b.check_out_date.isoformat(),
                'nights': (b.check_out_date - b.check_in_date).days,
                'rooms': '—', 'adults': b.adults, 'children': b.children,
                'total': float(b.total_amount or 0), 'deadline': None,
                'has_slip': bool(b.payment_slip_filename)}
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
                'has_slip': any(h.payment_slip_filename for h in holds)}
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
    from flask import send_file, current_app
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
    path = os.path.join(current_app.root_path, 'uploads', filename)
    if not os.path.exists(path):
        return jsonify(error='slip file missing'), 404
    return send_file(path)


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
