"""Booking Engine V1 — public routes mounted at /book/*.

Endpoints (public, no login required):

    GET  /book/                            — search landing page
    GET  /book/results                     — availability results
    GET  /book/select                      — chosen room type → guest form
    POST /book/confirm                     — atomic create
    GET  /book/confirmation/<booking_ref>  — confirmation + manual payment instructions

The legacy /availability + /submit flow remains untouched at the
public_bp blueprint. New direct-booking traffic should land on /book/.

Hard rules:
  - No login_required.
  - No WhatsApp / email / Gemini side effects.
  - Re-validates availability AND re-quotes price at every step.
    Cannot overbook — see services.booking_engine._pick_physical_room.
  - ActivityLog wired for: search_performed, booking_created,
    booking_failed.
"""

from __future__ import annotations

from datetime import date, timedelta

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
    current_app,
)

from ..models import db, Booking
from ..services.audit import log_activity
from ..services.booking_engine import (
    search_availability, quote_stay, create_direct_booking,
    parse_iso_date, validate_search_input,
)


booking_engine_bp = Blueprint('booking_engine', __name__, url_prefix='/book')


# ── Helpers ─────────────────────────────────────────────────────────

def _today_iso():
    return date.today().isoformat()


def _tomorrow_iso():
    return (date.today() + timedelta(days=1)).isoformat()


def _form_int(name, default=None):
    raw = (request.values.get(name) or '').strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ── GET /book/ ──────────────────────────────────────────────────────

@booking_engine_bp.route('/', methods=['GET'])
def search_form():
    """Landing — date picker + guest count."""
    return render_template(
        'booking_engine/search.html',
        today_iso=_today_iso(),
        tomorrow_iso=_tomorrow_iso(),
        prefill={
            'check_in':  request.args.get('check_in') or _today_iso(),
            'check_out': request.args.get('check_out') or _tomorrow_iso(),
            'guests':    request.args.get('guests') or '2',
        },
    )


# ── GET /book/results ────────────────────────────────────────────────

@booking_engine_bp.route('/results', methods=['GET'])
def results():
    check_in  = parse_iso_date(request.args.get('check_in'))
    check_out = parse_iso_date(request.args.get('check_out'))
    guests = _form_int('guests', 1) or 1

    err = validate_search_input(check_in, check_out, guests)
    if err:
        flash(err, 'error')
        return redirect(url_for('booking_engine.search_form'))

    result = search_availability(check_in, check_out, guests)

    log_activity(
        'booking_engine.search_performed',
        actor_type='guest',
        description=(
            f'Public search for {guests} guest'
            f'{"s" if guests != 1 else ""} '
            f'{check_in} → {check_out}.'
        ),
        metadata={
            'check_in':    check_in.isoformat(),
            'check_out':   check_out.isoformat(),
            'guest_count': guests,
            'option_count': len(result['options']),
            'bookable_count': sum(1 for o in result['options'] if o.bookable),
        },
        capture_request=False,
    )
    db.session.commit()

    return render_template(
        'booking_engine/results.html',
        check_in=check_in, check_out=check_out, guests=guests,
        nights=result['nights'],
        options=result['options'],
    )


# ── GET /book/select ────────────────────────────────────────────────

@booking_engine_bp.route('/select', methods=['GET'])
def select():
    """Show chosen room type with re-quote + guest details form."""
    rt_id     = _form_int('room_type_id')
    plan_id   = _form_int('rate_plan_id')
    check_in  = parse_iso_date(request.args.get('check_in'))
    check_out = parse_iso_date(request.args.get('check_out'))
    guests    = _form_int('guests', 1) or 1

    if rt_id is None:
        flash('Please choose a room type.', 'error')
        return redirect(url_for('booking_engine.search_form'))

    err = validate_search_input(check_in, check_out, guests)
    if err:
        flash(err, 'error')
        return redirect(url_for('booking_engine.search_form'))

    quote = quote_stay(rt_id, check_in, check_out, guests,
                        rate_plan_id=plan_id)
    if not quote['ok']:
        flash(quote.get('error') or 'No longer available — try other dates.',
              'error')
        return redirect(url_for('booking_engine.results',
                                 check_in=check_in.isoformat(),
                                 check_out=check_out.isoformat(),
                                 guests=guests))

    return render_template(
        'booking_engine/details.html',
        quote=quote,
        check_in=check_in, check_out=check_out, guests=guests,
        room_type_id=rt_id, rate_plan_id=plan_id,
    )


# ── POST /book/confirm ──────────────────────────────────────────────

@booking_engine_bp.route('/confirm', methods=['POST'])
def confirm():
    """Atomic: re-validate, re-quote, create guest + booking + invoice.

    On race-loss (no rooms left for the type) bounces the user back
    to /book/results with a flash message.
    """
    rt_id     = _form_int('room_type_id')
    plan_id   = _form_int('rate_plan_id')
    check_in  = parse_iso_date(request.form.get('check_in'))
    check_out = parse_iso_date(request.form.get('check_out'))
    guests    = _form_int('guests', 1) or 1

    first_name = (request.form.get('first_name') or '').strip()
    last_name  = (request.form.get('last_name')  or '').strip()
    phone      = (request.form.get('phone')      or '').strip()
    email      = (request.form.get('email')      or '').strip() or None
    nationality = (request.form.get('nationality') or '').strip() or None
    special_requests = (request.form.get('special_requests') or '').strip() or None

    result = create_direct_booking(
        room_type_id=rt_id,
        rate_plan_id=plan_id,
        check_in=check_in, check_out=check_out,
        guests=guests,
        first_name=first_name, last_name=last_name,
        phone=phone, email=email,
        nationality=nationality,
        special_requests=special_requests,
    )

    if not result['ok']:
        log_activity(
            'booking_engine.booking_failed',
            actor_type='guest',
            description=(
                f'Direct booking attempt failed: {result["error"]}'
            ),
            metadata={
                'check_in':    check_in.isoformat() if check_in else None,
                'check_out':   check_out.isoformat() if check_out else None,
                'guest_count': guests,
                'room_type_id': rt_id,
                'rate_plan_id': plan_id,
                'reason':       result['error'],
            },
            capture_request=False,
        )
        db.session.commit()

        flash(result['error'] or 'Could not create booking.', 'error')
        if check_in and check_out:
            return redirect(url_for(
                'booking_engine.results',
                check_in=check_in.isoformat(),
                check_out=check_out.isoformat(),
                guests=guests,
            ))
        return redirect(url_for('booking_engine.search_form'))

    booking = result['booking']

    log_activity(
        'booking_engine.booking_created',
        actor_type='guest',
        booking=booking, invoice=booking.invoice,
        new_value=booking.status,
        description=(
            f'Direct booking {booking.booking_ref} created for room '
            f'{result["room_number"]} ({result["nights"]} nights).'
        ),
        metadata={
            'check_in':    check_in.isoformat(),
            'check_out':   check_out.isoformat(),
            'guest_count': guests,
            'room_type_id': rt_id,
            'rate_plan_id': plan_id,
            'booking_id':  booking.id,
            'booking_ref': booking.booking_ref,
            'room_number': result['room_number'],
            'total':       result['total'],
            'source':      'booking_engine',
        },
        capture_request=False,
    )
    db.session.commit()

    return redirect(url_for(
        'booking_engine.confirmation',
        booking_ref=booking.booking_ref,
    ))


# ── GET /book/confirmation/<ref> ────────────────────────────────────

@booking_engine_bp.route('/confirmation/<booking_ref>', methods=['GET'])
def confirmation(booking_ref):
    booking = (Booking.query
               .filter_by(booking_ref=booking_ref)
               .first_or_404())
    return render_template(
        'booking_engine/confirmation.html',
        booking=booking,
    )
