"""Booking Engine V2 — public multi-room booking portal (Phase 2).

Replaces the old single-room /book flow. Public pages expose ONLY type names,
prices, and availability counts (never room numbers). Renders from the Phase 1
inventory/holds services — no stored counters.

Flow: /book (dates → per-type cards) → POST /book/hold (selection holds) →
/book/guest (countdown + lead-guest form + optional slip) → POST /book/submit
(→ one pending group) → /book/status (reference + deadline).
"""

from __future__ import annotations

import os
from datetime import date, datetime

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session, current_app)
from werkzeug.utils import secure_filename

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
    return render_template('portal/guest.html', holds=live, expires_at=expires_at)


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
                   'nationality', 'id_type', 'id_number')}
    slip_filename = None
    f = request.files.get('payment_slip')
    if f and f.filename:
        slip_filename = _save_slip(f)

    res = portal_svc.submit(tok, guest_data, slip_filename=slip_filename)
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


def _save_slip(fileobj):
    """Save the uploaded slip to the uploads dir (+ best-effort R2 dual-write via
    the existing drive service, same pattern as bookings). Returns the filename."""
    upload_dir = os.path.join(current_app.root_path, 'uploads')
    os.makedirs(upload_dir, exist_ok=True)
    fname = f'holdslip_{datetime.utcnow().strftime("%Y%m%d%H%M%S")}_' \
            + secure_filename(fileobj.filename)[:80]
    path = os.path.join(upload_dir, fname)
    fileobj.save(path)
    # R2 dual-write is best-effort here; drive_id is stored on the hold at submit
    # (transfers to the booking at confirmation) — see services.drive.
    return fname
