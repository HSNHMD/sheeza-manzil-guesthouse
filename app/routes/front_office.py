"""Front Office operational module.

Four routes that give the front desk a clean operational view of
today's hotel state, separate from the bookings list (which is the
all-time index) and the reservation board (which is the timeline).

    GET /front-office/                — overview dashboard
    GET /front-office/arrivals        — today's check-ins
    GET /front-office/departures      — today's check-outs
    GET /front-office/in-house        — currently checked-in guests

Hard rules enforced:
    - login_required on every route
    - read-only — no booking / payment / room status mutations.
      All check-in / check-out / payment actions live on the booking
      detail page; this module just lists what to act on.
    - No WhatsApp / email / Gemini / R2 calls.
    - Pages render cleanly on phone / tablet / desktop.
"""

from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, render_template, request
from flask_login import login_required
from sqlalchemy import or_

from ..models import db, Booking, Guest, Room, Invoice
from ..utils import hotel_date


front_office_bp = Blueprint('front_office', __name__,
                            url_prefix='/front-office')


# ── Helpers ──────────────────────────────────────────────────────────

# Booking statuses that count as "active in house" or "active arrival".
_ACTIVE_STATUSES = (
    'new_request', 'pending_payment', 'payment_uploaded',
    'payment_verified', 'confirmed', 'checked_in',
)
_INACTIVE_STATUSES = ('cancelled', 'rejected')


def _parse_date_arg(name: str, default):
    """Parse a YYYY-MM-DD query-string param, falling back to default."""
    raw = (request.args.get(name) or '').strip()
    if not raw:
        return default
    try:
        return date.fromisoformat(raw)
    except (ValueError, TypeError):
        return default


def _matches_search(b: Booking, term: str) -> bool:
    """Case-insensitive match across booking_ref, guest name, room number."""
    if not term:
        return True
    t = term.lower()
    if b.booking_ref and t in b.booking_ref.lower():
        return True
    if b.guest:
        full = f'{b.guest.first_name or ""} {b.guest.last_name or ""}'.lower()
        if t in full:
            return True
    if b.room and b.room.number and t in b.room.number.lower():
        return True
    return False


def _payment_status_for(b: Booking) -> str:
    """Resolve a booking's payment-status string from its invoice, with
    a sensible 'not_received' default for bookings that have no invoice."""
    inv = getattr(b, 'invoice', None)
    if inv is None:
        return 'not_received'
    return (getattr(inv, 'payment_status', None) or 'not_received')


# ── Index / dashboard ────────────────────────────────────────────────

@front_office_bp.route('/')
@login_required
def index():
    """Front Office dashboard — at-a-glance counts + quick links."""
    today = hotel_date()

    arrivals_q = (
        Booking.query
        .filter(
            Booking.check_in_date == today,
            ~Booking.status.in_(_INACTIVE_STATUSES),
        )
    )
    departures_q = (
        Booking.query
        .filter(
            Booking.check_out_date == today,
            Booking.status.in_(('checked_in', 'confirmed', 'payment_verified')),
        )
    )
    in_house_q = (
        Booking.query
        .filter(
            Booking.check_in_date  <= today,
            Booking.check_out_date >  today,
            Booking.status.in_(('checked_in', 'confirmed', 'payment_verified')),
        )
    )

    # Counts only — keep the dashboard fast even at 100+ bookings/day.
    stats = {
        'arrivals_today':   arrivals_q.count(),
        'departures_today': departures_q.count(),
        'in_house':         in_house_q.count(),
    }

    return render_template(
        'front_office/index.html',
        today=today,
        stats=stats,
    )


# ── Arrivals ─────────────────────────────────────────────────────────

@front_office_bp.route('/arrivals')
@login_required
def arrivals():
    """Today's check-ins. Defaults to ``hotel_date()`` but accepts
    ?date=YYYY-MM-DD so the operator can preview tomorrow's arrivals."""
    today = hotel_date()
    target_date = _parse_date_arg('date', today)
    search = (request.args.get('search') or '').strip()

    q = (
        Booking.query.join(Guest).join(Room)
        .filter(Booking.check_in_date == target_date)
        .filter(~Booking.status.in_(_INACTIVE_STATUSES))
        .order_by(Room.number.asc())
    )
    bookings = q.all()
    if search:
        bookings = [b for b in bookings if _matches_search(b, search)]

    return render_template(
        'front_office/list_view.html',
        page_kind='arrivals',
        page_title='Arrivals',
        page_icon='arrivals',
        empty_message=(f'No arrivals on {target_date.strftime("%a %b %-d")}.'
                       if not search else
                       f'No arrivals match "{search}" on {target_date}.'),
        target_date=target_date,
        is_today=(target_date == today),
        today=today,
        bookings=bookings,
        search=search,
        payment_status_for=_payment_status_for,
    )


# ── Departures ───────────────────────────────────────────────────────

@front_office_bp.route('/departures')
@login_required
def departures():
    """Today's check-outs."""
    today = hotel_date()
    target_date = _parse_date_arg('date', today)
    search = (request.args.get('search') or '').strip()

    q = (
        Booking.query.join(Guest).join(Room)
        .filter(Booking.check_out_date == target_date)
        .filter(Booking.status.in_(
            ('checked_in', 'confirmed', 'payment_verified', 'checked_out'),
        ))
        .order_by(Room.number.asc())
    )
    bookings = q.all()
    if search:
        bookings = [b for b in bookings if _matches_search(b, search)]

    return render_template(
        'front_office/list_view.html',
        page_kind='departures',
        page_title='Departures',
        page_icon='departures',
        empty_message=(f'No departures on {target_date.strftime("%a %b %-d")}.'
                       if not search else
                       f'No departures match "{search}" on {target_date}.'),
        target_date=target_date,
        is_today=(target_date == today),
        today=today,
        bookings=bookings,
        search=search,
        payment_status_for=_payment_status_for,
    )


# ── In House ─────────────────────────────────────────────────────────

@front_office_bp.route('/in-house')
@login_required
def in_house():
    """Currently in-house guests (checked-in + confirmed-and-overlapping today)."""
    today = hotel_date()
    target_date = _parse_date_arg('date', today)
    search = (request.args.get('search') or '').strip()

    q = (
        Booking.query.join(Guest).join(Room)
        .filter(Booking.check_in_date  <= target_date)
        .filter(Booking.check_out_date >  target_date)
        .filter(Booking.status.in_(
            ('checked_in', 'confirmed', 'payment_verified'),
        ))
        .order_by(Room.number.asc())
    )
    bookings = q.all()
    if search:
        bookings = [b for b in bookings if _matches_search(b, search)]

    return render_template(
        'front_office/list_view.html',
        page_kind='in_house',
        page_title='In House',
        page_icon='in_house',
        empty_message=(f'No guests in-house on {target_date.strftime("%a %b %-d")}.'
                       if not search else
                       f'No guests match "{search}".'),
        target_date=target_date,
        is_today=(target_date == today),
        today=today,
        bookings=bookings,
        search=search,
        payment_status_for=_payment_status_for,
    )
