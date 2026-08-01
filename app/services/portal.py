"""Booking Engine V2 — public portal orchestration (Phase 2).

Thin layer over the Phase 1 services. Renders ONLY from live inventory
functions — no stored counters. Enforces the anti-abuse rules (one active
selection group per session; per-session creation rate-limit).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from . import inventory, holds


def session_token(flask_session):
    """Stable per-browser token; created on first portal visit."""
    tok = flask_session.get('portal_token')
    if not tok:
        tok = secrets.token_urlsafe(24)
        flask_session['portal_token'] = tok
    return tok


def public_reference(tok):
    """Short display code the guest can quote (derived from their token)."""
    return tok[:8].upper()


def gallery_photos():
    """Property photos for the portal, auto-discovered from
    ``static/img/property/``. Drop image files in that folder and they appear
    below the booking interface — no code change. Sorted by filename (prefix
    with 01_, 02_… to order them)."""
    import os
    from flask import current_app
    d = os.path.join(current_app.root_path, 'static', 'img', 'property')
    exts = ('.jpg', '.jpeg', '.png', '.webp', '.avif', '.gif')
    try:
        files = sorted(f for f in os.listdir(d)
                       if f.lower().endswith(exts) and not f.startswith('.'))
    except FileNotFoundError:
        files = []
    return ['/static/img/property/' + f for f in files]


def search(check_in, check_out, guests=1):
    """Per-type cards for the search results — name, price, 'N left'. Never
    exposes room numbers (all figures are type-level)."""
    from ..models import RoomType
    nights = max(1, (check_out - check_in).days)
    cards = []
    for rt in (RoomType.query.filter_by(is_active=True)
               .order_by(RoomType.name).all()):
        av = inventory.available_for_stay(rt.id, check_in, check_out, 1)
        max_qty = max(0, min(av['contiguous_free'], av['min_sellable']))
        price = inventory.price_stay(rt.id, check_in, check_out,
                                     fallback=None)
        cards.append({
            'room_type': rt,
            'available_qty': max_qty,
            'sold_out': max_qty <= 0,
            'nights': nights,
            'price_total_per_room': price['total'],
            'price_per_night': round(price['total'] / nights, 2) if price['total'] else 0,
        })
    return cards


def _rate_limited(tok, now):
    from flask import current_app
    from ..models import Hold
    limit = int(current_app.config.get('HOLD_MAX_PER_SESSION_HOUR', 12))
    since = now - timedelta(hours=1)
    n = Hold.query.filter(Hold.session_token == tok,
                          Hold.created_at >= since).count()
    return n >= limit


def _release_prior_selection(tok, now):
    """One active selection group per session: releasing the old (audited)."""
    from ..models import db
    from .audit import log_activity
    prior = holds.holds_for_session(tok, hold_type='selection',
                                    state='active', now=now)
    for h in prior:
        h.state = 'released'
        h.released_reason = 'superseded by new selection'
        h.released_at = now
    if prior:
        log_activity('hold.selection_superseded', actor_type='guest',
                     description=f'{len(prior)} prior selection hold(s) released.',
                     metadata={'session_token': tok[:32], 'count': len(prior)})
        db.session.commit()


def create_holds(items, check_in, check_out, tok, *, guests=None, now=None):
    """Anti-abuse guarded selection-hold creation (atomic multi-type). `guests`
    (search-bar count) carries onto the holds as adults for the guest step."""
    now = now or datetime.utcnow()
    if _rate_limited(tok, now):
        return {'ok': False, 'reasons':
                ['too many attempts from this session — please wait a minute.']}
    _release_prior_selection(tok, now)
    return holds.acquire_multi_selection(items, check_in, check_out, tok,
                                         guests=guests, now=now)


def group_capacity(tok, *, now=None):
    """Summed max-occupancy of the session's selected room types (max_occupancy
    × qty per type). Returns (capacity, missing_type_ids). max_occupancy is a
    NOT-NULL default-2 column, so `missing` should be empty in practice; the
    fallback treats any absent value as 2 (flagged in the report)."""
    from ..models import RoomType
    live = holds.holds_for_session(tok, hold_type='selection',
                                   state='active', now=now)
    cap = 0
    missing = []
    for h in live:
        rt = RoomType.query.get(h.room_type_id)
        mo = getattr(rt, 'max_occupancy', None)
        if not mo:
            missing.append(h.room_type_id)
            mo = 2
        cap += mo * h.qty
    return cap, missing


def submit(tok, guest_data, *, slip_filename=None, slip_drive_id=None, now=None):
    """Guest form submission → selection group becomes ONE pending group (6h).
    Server re-validates the holds are live (never trusts the client timer) AND
    enforces the guest count ≤ the selected rooms' summed max occupancy."""
    from ..models import db, Guest
    now = now or datetime.utcnow()

    # Guest count (server is authoritative; the client hint is advisory).
    try:
        adults = max(1, int(guest_data.get('adults') or 1))
        children = max(0, int(guest_data.get('children') or 0))
    except (TypeError, ValueError):
        adults, children = 1, 0
    total_guests = adults + children
    # Occupancy validation: G <= Σ(max_occupancy × qty). On violation, tell the
    # guest the per-room cap and the minimum rooms needed (mixed-type aware).
    from . import occupancy
    live_sel = holds.holds_for_session(tok, hold_type='selection',
                                       state='active', now=now)
    if not live_sel:
        return {'ok': False, 'reasons': ['your hold expired — please start over.']}
    items = [{'room_type_id': h.room_type_id, 'qty': h.qty} for h in live_sel]
    nights = (live_sel[0].check_out_date - live_sel[0].check_in_date).days
    occ = occupancy.compute(items, total_guests, nights)
    if occ['over_capacity']:
        return {'ok': False, 'reasons': [occupancy.block_message(
            total_guests, occ['max_per_room'], occ['min_rooms'])]}
    # Nationality is REQUIRED (Pepper Phase 0 / Green-Tax correctness) — enforced
    # server-side here so it holds regardless of the client form.
    nationality = (guest_data.get('nationality') or '').strip()
    if not nationality:
        return {'ok': False, 'reasons': ['Nationality is required.']}
    g = Guest(first_name=(guest_data.get('first_name') or '').strip(),
              last_name=(guest_data.get('last_name') or '').strip(),
              email=(guest_data.get('email') or '').strip(),
              phone=(guest_data.get('phone') or '').strip(),
              nationality=nationality,
              id_type=(guest_data.get('id_type') or '').strip() or None,
              id_number=(guest_data.get('id_number') or '').strip() or None)
    db.session.add(g)
    db.session.flush()
    res = holds.promote_group_to_pending(
        tok, guest_name=f'{g.first_name} {g.last_name}'.strip(),
        contact=g.phone, lead_guest_id=g.id,
        slip_filename=slip_filename, slip_drive_id=slip_drive_id,
        adults=adults, children=children, now=now)
    if not res['ok']:
        db.session.rollback()
        return res
    res['reference'] = public_reference(tok)
    return res


def status(tok, *, now=None):
    """Guest-facing status for their session: pending / confirmed / expired."""
    from ..models import Hold
    now = now or datetime.utcnow()
    hs = (Hold.query.filter(Hold.session_token == tok)
          .order_by(Hold.id).all())
    if not hs:
        return {'state': 'none'}
    if any(h.state == 'converted' for h in hs):
        return {'state': 'confirmed', 'reference': public_reference(tok)}
    live = [h for h in hs if h.hold_type == 'pending'
            and h.state == 'active' and h.expires_at > now]
    if live:
        # Grand total (rooms + extra-person fee) so the confirmation can tell the
        # guest exactly what to transfer. Same math as the guest step; display only.
        from . import occupancy
        ci, co = live[0].check_in_date, live[0].check_out_date
        nights = (co - ci).days
        items = [{'room_type_id': h.room_type_id, 'qty': h.qty} for h in live]
        room_total = sum(inventory.price_stay(h.room_type_id, ci, co)['total'] * h.qty
                         for h in live)
        guests = (live[0].adults or 1) + (live[0].children or 0)
        occ = occupancy.compute(items, guests, nights)
        first = live[0]
        slip_state = ('rejected' if first.slip_rejected_at else
                      'uploaded' if first.payment_slip_filename else 'awaiting')
        return {'state': 'pending', 'reference': public_reference(tok),
                'expires_at': min(h.expires_at for h in live), 'holds': live,
                'total': room_total + occ['fee_total'],
                'slip_state': slip_state, 'slip_reason': first.slip_rejected_reason}
    if any(h.hold_type == 'pending' for h in hs):
        return {'state': 'expired', 'reference': public_reference(tok)}
    sel = [h for h in hs if h.hold_type == 'selection'
           and h.state == 'active' and h.expires_at > now]
    return {'state': 'selection' if sel else 'expired',
            'reference': public_reference(tok)}
