"""Booking Engine V2 — public multi-room booking portal (Phase 2).

Replaces the old single-room /book flow. Public pages expose ONLY type names,
prices, and availability counts (never room numbers). Renders from the Phase 1
inventory/holds services — no stored counters.

Flow: /book (dates → per-type cards) → POST /book/hold (selection holds) →
/book/guest (countdown + lead-guest form + optional slip) → POST /book/submit
(→ one pending group) → /book/status (reference + deadline).
"""

from __future__ import annotations

from datetime import date, datetime

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session)

from ..services import portal as portal_svc
from ..services import holds as holds_svc

portal_bp = Blueprint('portal', __name__, url_prefix='/book')


def _parse_dates():
    try:
        ci = date.fromisoformat(request.values.get('check_in', ''))
        co = date.fromisoformat(request.values.get('check_out', ''))
    except ValueError:
        return None, None
    return ci, co


@portal_bp.route('/', methods=['GET'])
def index():
    ci, co = _parse_dates()
    guests = request.values.get('guests', 1, type=int)
    cards = None
    error = None
    if ci and co:
        if co <= ci:
            error = 'Check-out must be after check-in.'
        elif ci < date.today():
            error = 'Check-in cannot be in the past.'
        else:
            cards = portal_svc.search(ci, co, guests)
    return render_template('portal/search.html', cards=cards, error=error,
                           check_in=ci, check_out=co, guests=guests)


@portal_bp.route('/hold', methods=['POST'])
def hold():
    ci, co = _parse_dates()
    if not (ci and co) or co <= ci:
        flash('Please choose valid dates.', 'error')
        return redirect(url_for('portal.index'))
    items = []
    for key, val in request.form.items():
        if key.startswith('qty_'):
            try:
                qty = int(val)
            except ValueError:
                qty = 0
            if qty > 0:
                items.append({'room_type_id': int(key[4:]), 'qty': qty})
    if not items:
        flash('Select at least one room.', 'error')
        return redirect(url_for('portal.index', check_in=ci, check_out=co))

    tok = portal_svc.session_token(session)
    res = portal_svc.create_holds(items, ci, co, tok)
    if not res['ok']:
        # someone likely just took the last room — friendly re-query, not an error page
        flash('Someone just grabbed those — here are the latest numbers.', 'info')
        return redirect(url_for('portal.index', check_in=ci, check_out=co))
    return redirect(url_for('portal.guest'))


@portal_bp.route('/guest', methods=['GET'])
def guest():
    tok = portal_svc.session_token(session)
    live = holds_svc.holds_for_session(tok, hold_type='selection',
                                       state='active', now=datetime.utcnow())
    if not live:
        flash('Your hold expired — please choose your dates again.', 'error')
        return redirect(url_for('portal.index'))
    expires_at = min(h.expires_at for h in live)
    # Booking summary + capacity for the guest-details step.
    from ..services import inventory
    from ..models import RoomType
    ci, co = live[0].check_in_date, live[0].check_out_date
    nights = (co - ci).days
    rooms = sum(h.qty for h in live)
    total = sum(inventory.price_stay(h.room_type_id, ci, co)['total'] * h.qty
                for h in live)
    # Per-type rooms breakdown (e.g. "2× Standard, 1× Deluxe") so the guest step
    # keeps showing WHAT was selected, not just a room count. Aggregated by type.
    rt_names = {rt.id: rt.name for rt in RoomType.query
                .filter(RoomType.id.in_([h.room_type_id for h in live])).all()}
    agg = {}
    for h in live:
        agg[h.room_type_id] = agg.get(h.room_type_id, 0) + h.qty
    breakdown = [{'name': rt_names.get(rid, 'Room'), 'qty': qty}
                 for rid, qty in sorted(agg.items(),
                                        key=lambda kv: rt_names.get(kv[0], ''))]
    # Occupancy: capacity + extra-person fee. Single source of truth shared with
    # portal.submit (validation) and group_booking (folio). Initial render uses
    # the counts already on the hold (default 1 adult / 0 children); the client
    # hint recomputes the fee live from slot_fees as the guest edits the counts.
    from ..services import occupancy
    items = [{'room_type_id': h.room_type_id, 'qty': h.qty} for h in live]
    init_guests = (live[0].adults or 1) + (live[0].children or 0)
    occ = occupancy.compute(items, init_guests, nights)
    summary = {'rooms': rooms, 'breakdown': breakdown, 'nights': nights,
               'total': total, 'capacity': occ['capacity'],
               'base_total': occ['base_total'], 'slot_fees': occ['slot_fees'],
               'extra_fee': occ['fee_total'], 'grand_total': total + occ['fee_total'],
               'check_in': ci, 'check_out': co}
    return render_template('portal/guest.html', holds=live,
                           expires_at=expires_at, summary=summary)


@portal_bp.route('/submit', methods=['POST'])
def submit():
    tok = portal_svc.session_token(session)
    # Server re-validates the hold is still live — never trust the client timer.
    live = holds_svc.holds_for_session(tok, hold_type='selection',
                                       state='active', now=datetime.utcnow())
    if not live:
        flash('Your hold expired before submission — please start over.', 'error')
        return redirect(url_for('portal.index'))

    guest_data = {k: request.form.get(k) for k in
                  ('first_name', 'last_name', 'email', 'phone',
                   'nationality', 'id_type', 'id_number', 'adults', 'children')}
    slip_filename = slip_drive_id = None
    f = request.files.get('payment_slip')
    if f and f.filename:
        # Reuse the exact guest-upload semantics: local write ALWAYS + R2
        # dual-write; drive_id is None when R2 is unconfigured/fails (local
        # fallback) — same as public._save_file / booking uploads.
        from .public import _save_file
        slip_filename, slip_drive_id = _save_file(f, 'holdslip', 'payment_slip')

    res = portal_svc.submit(tok, guest_data, slip_filename=slip_filename,
                            slip_drive_id=slip_drive_id)
    if not res['ok']:
        flash('; '.join(res.get('reasons', ['Could not submit — please retry.'])),
              'error')
        return redirect(url_for('portal.guest'))
    return redirect(url_for('portal.status'))


@portal_bp.route('/status', methods=['GET'])
def status():
    tok = portal_svc.session_token(session)
    st = portal_svc.status(tok)
    return render_template('portal/status.html', st=st)
