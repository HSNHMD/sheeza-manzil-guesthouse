"""Reservation Board prototype — admin-only, staging-friendly.

A new top-level route ``/board`` that renders a tape-chart view of
rooms × dates with bookings as horizontal bars. The existing /rooms
view is left untouched so this can be evaluated side-by-side without
risk to the established workflow.

Hard rules enforced:
- @login_required + @admin_required on every route in this module.
- Read-only: no booking / room / invoice / payment status mutation.
- No WhatsApp / email / Gemini / R2 calls in any route here.
- All data comes from the existing models — no schema additions.
"""

from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, render_template, request
from flask_login import login_required
from sqlalchemy import or_

from ..models import Room, Booking, ActivityLog
from ..decorators import admin_required
from ..services.board import (
    DEFAULT_VIEW,
    VIEW_SPANS,
    BookingBar,
    date_range,
    filter_state_summary,
    group_rooms,
    in_house_today,
    make_booking_bar,
    normalize_density,
    normalize_grouping,
    normalize_view,
    parse_start_date,
    pending_payment,
    room_status_badge,
    shift_range,
    todays_arrivals,
    todays_departures,
    view_day_width_px,
    view_span_days,
)


board_bp = Blueprint('board', __name__)


# ── Helper: rooms with optional filtering ───────────────────────────

def _filter_rooms(query, *, floor=None, room_type=None):
    if floor is not None and floor != '':
        try:
            query = query.filter(Room.floor == int(floor))
        except (ValueError, TypeError):
            pass
    if room_type:
        query = query.filter(Room.room_type == room_type)
    return query


def _filter_bookings(query, *, booking_status=None, payment_status=None,
                     search=None):
    if booking_status:
        query = query.filter(Booking.status == booking_status)
    if search:
        like = f'%{search}%'
        # Filter at SQL level; guest name handled in Python so we cover
        # both first / last name combinations cleanly.
        query = query.filter(or_(
            Booking.booking_ref.ilike(like),
        ))
    return query


def _booking_matches_search(booking, term: str) -> bool:
    if not term:
        return True
    t = term.lower()
    if booking.booking_ref and t in booking.booking_ref.lower():
        return True
    g = booking.guest
    if g:
        full = f'{g.first_name or ""} {g.last_name or ""}'.lower()
        if t in full:
            return True
    if booking.room and booking.room.number:
        if t in booking.room.number.lower():
            return True
    return False


# ── GET /board — main board view ────────────────────────────────────

@board_bp.route('/board')
@login_required
@admin_required
def index():
    """Render the reservation tape-chart board.

    Query string:
        view  = day | 7d | 14d | 30d (default 14d)
        start = YYYY-MM-DD (default today − 1)
        floor, room_type, booking_status, search — optional filters
    """
    view  = normalize_view(request.args.get('view'))
    span  = view_span_days(view)
    today = date.today()
    start = parse_start_date(request.args.get('start'),
                             default=today - timedelta(days=1))
    end   = start + timedelta(days=span)
    days  = date_range(start, span)

    floor          = (request.args.get('floor') or '').strip() or None
    room_type      = (request.args.get('room_type') or '').strip() or None
    booking_status = (request.args.get('booking_status') or '').strip() or None
    payment_filter = (request.args.get('payment_status') or '').strip() or None
    search         = (request.args.get('search') or '').strip() or None
    grouping       = normalize_grouping(request.args.get('group'))
    density        = normalize_density(request.args.get('density'))

    # ── Rooms (sort order depends on grouping) ──
    rooms_q = Room.query
    rooms_q = _filter_rooms(rooms_q, floor=floor, room_type=room_type)
    if grouping == 'room_type':
        rooms_q = rooms_q.order_by(Room.room_type.asc(),
                                   Room.number.asc())
    else:
        rooms_q = rooms_q.order_by(Room.floor.asc(),
                                   Room.number.asc())
    rooms = rooms_q.all()
    room_ids = {r.id for r in rooms}

    # ── Bookings overlapping the window ──
    bookings_q = Booking.query.filter(
        Booking.check_in_date  < end,
        Booking.check_out_date > start,
        Booking.room_id.in_(room_ids) if room_ids else False,
    )
    bookings_q = _filter_bookings(
        bookings_q,
        booking_status=booking_status,
        payment_status=payment_filter,
        search=search,
    )
    bookings = bookings_q.all()
    if search:
        bookings = [b for b in bookings if _booking_matches_search(b, search)]
    if payment_filter:
        bookings = [
            b for b in bookings
            if (getattr(getattr(b, 'invoice', None), 'payment_status', None) or
                'not_received') == payment_filter
        ]

    # ── Convert to grid bars per room ──
    import re as _re
    bars_by_room = {r.id: [] for r in rooms}
    drawer_data = {}     # booking_id → JSON-safe dict for the drawer JS
    rooms_by_id = {r.id: r for r in rooms}
    for b in bookings:
        bar = make_booking_bar(b, start, end)
        if bar is not None and b.room_id in bars_by_room:
            bars_by_room[b.room_id].append(bar)
            phone_digits = ''
            if b.guest and b.guest.phone:
                phone_digits = _re.sub(r'\D', '', b.guest.phone)
            r_for_b = rooms_by_id.get(b.room_id)

            # Invoice summary (None-safe — many test bookings have no invoice)
            inv = getattr(b, 'invoice', None)
            invoice_data = None
            if inv is not None:
                total = float(getattr(inv, 'total_amount', 0.0) or 0.0)
                paid  = float(getattr(inv, 'amount_paid',   0.0) or 0.0)
                invoice_data = {
                    'total':     round(total, 2),
                    'paid':      round(paid,  2),
                    'balance':   round(total - paid, 2),
                    'number':    getattr(inv, 'invoice_number', None) or '',
                }

            drawer_data[b.id] = {
                'id':            b.id,
                'ref':           bar.booking_ref,
                'guest':         bar.guest_name,
                'guestId':       (b.guest.id if b.guest else None),
                'phoneDigits':   phone_digits,
                'guests':        bar.num_guests,
                'nights':        bar.nights,
                'checkIn':       bar.check_in.isoformat(),
                'checkOut':      bar.check_out.isoformat(),
                'room':          (r_for_b.number if r_for_b else ''),
                'roomType':      (r_for_b.room_type if r_for_b else ''),
                'floor':         (r_for_b.floor if r_for_b else None),
                'totalAmount':   float(getattr(b, 'total_amount', 0.0) or 0.0),
                'status':        bar.booking_status,
                'paymentStatus': bar.payment_status,
                'invoice':       invoice_data,
                'activity':      [],   # populated below in a single batched query
            }
    for r in rooms:
        bars_by_room[r.id].sort(key=lambda x: x.grid_col_start)

    # ── Recent activity (batched) ──
    # Fetch up to ~3 audit rows per booking for the drawer "Recent
    # activity" section. One query covers the whole window.
    if drawer_data:
        booking_ids = list(drawer_data.keys())
        recent_rows = (
            ActivityLog.query
            .filter(ActivityLog.booking_id.in_(booking_ids))
            .order_by(ActivityLog.created_at.desc())
            .limit(max(50, len(booking_ids) * 4))
            .all()
        )
        per_booking_count = {bid: 0 for bid in booking_ids}
        for row in recent_rows:
            bid = row.booking_id
            if bid not in drawer_data:
                continue
            if per_booking_count.get(bid, 0) >= 3:
                continue
            per_booking_count[bid] += 1
            drawer_data[bid]['activity'].append({
                'action':      row.action or '',
                'description': (row.description or '')[:140],
                'createdAt':   row.created_at.isoformat() if row.created_at else None,
                'actor':       row.actor_type or 'system',
            })

    # ── Room status badges (clean / dirty / occupied / etc.) ──
    # Compute against ALL of the room's bookings today (not just window-clipped),
    # so the badge reflects current occupancy regardless of window start.
    today_bookings = Booking.query.filter(
        Booking.room_id.in_(room_ids) if room_ids else False,
        Booking.check_in_date  <= today,
        Booking.check_out_date >  today,
    ).all() if room_ids else []
    badges = {r.id: room_status_badge(r, today, today_bookings)
              for r in rooms}

    # ── Stats for the toolbar / mobile card view ──
    all_window_bookings = bookings
    # For the “today” counters, query separately — ignores window filter.
    all_today_bookings = today_bookings + Booking.query.filter(
        Booking.room_id.in_(room_ids) if room_ids else False,
        or_(Booking.check_in_date == today,
            Booking.check_out_date == today),
    ).all() if room_ids else []

    arrivals_list   = todays_arrivals(all_today_bookings, today)
    departures_list = todays_departures(all_today_bookings, today)
    inhouse_list    = in_house_today(all_today_bookings, today)
    stats = {
        'arrivals_today':   len({b.id for b in arrivals_list}),
        'departures_today': len({b.id for b in departures_list}),
        'in_house_today':   len({b.id for b in inhouse_list}),
        'pending_payment':  len({b.id for b in pending_payment(all_window_bookings)}),
        'rooms_total':      len(rooms),
    }
    # De-duplicate by id since today_bookings + match-on-date can overlap
    def _uniq(seq):
        seen = set(); out = []
        for x in seq:
            if x.id not in seen:
                seen.add(x.id); out.append(x)
        return out
    mobile_arrivals   = _uniq(arrivals_list)
    mobile_departures = _uniq(departures_list)

    # ── Distinct values for filter dropdowns ──
    floors_available = sorted({r.floor for r in Room.query.all()
                               if r.floor is not None})
    types_available  = sorted({r.room_type for r in Room.query.all()
                               if r.room_type})

    # ── Prev / next start dates for nav arrows ──
    prev_start = shift_range(start, span, -1)
    next_start = shift_range(start, span, 1)

    # ── Build the row-plan: ordered list of group headers + rooms,
    #    each carrying its grid-row index. Row 1 = day header, body
    #    starts at row 2.
    groups = group_rooms(rooms, grouping)
    row_plan = []
    row_n = 2
    room_index = 0  # for zebra striping (resets per room only)
    for group_label, group_rooms_list in groups:
        if grouping != 'none' and group_label:
            row_plan.append({
                'type':       'group',
                'label':      group_label,
                'count':      len(group_rooms_list),
                'row_n':      row_n,
            })
            row_n += 1
        for r in group_rooms_list:
            row_plan.append({
                'type':       'room',
                'room':       r,
                'row_n':      row_n,
                'badge':      badges[r.id],
                'bars':       bars_by_room.get(r.id, []),
                'zebra':      (room_index % 2 == 1),
            })
            row_n += 1
            room_index += 1
    total_body_rows = row_n - 2  # excluding day header

    # ── Active filters summary (for "clear all" affordance) ──
    filter_summary = filter_state_summary(
        floor=floor, room_type=room_type,
        booking_status=booking_status,
        payment_status=payment_filter, search=search,
    )

    return render_template(
        'board/index.html',
        view=view,
        span=span,
        start=start,
        end=end,
        end_inclusive=end - timedelta(days=1),
        today=today,
        days=days,
        day_width_px=view_day_width_px(view),
        rooms=rooms,
        bars_by_room=bars_by_room,
        badges=badges,
        stats=stats,
        floors_available=floors_available,
        types_available=types_available,
        # Layout state
        grouping=grouping,
        density=density,
        row_plan=row_plan,
        total_body_rows=total_body_rows,
        filter_summary=filter_summary,
        drawer_data=drawer_data,
        mobile_arrivals=mobile_arrivals,
        mobile_departures=mobile_departures,
        # Form-state echo
        floor=floor,
        room_type=room_type,
        booking_status=booking_status,
        payment_status=payment_filter,
        search=search or '',
        prev_start=prev_start,
        next_start=next_start,
        # Filter dropdown vocabularies
        all_booking_statuses=(
            'new_request', 'pending_payment', 'payment_uploaded',
            'payment_verified', 'confirmed', 'checked_in',
            'checked_out', 'cancelled', 'rejected',
        ),
        all_payment_statuses=(
            'not_received', 'pending_review', 'verified',
            'rejected', 'mismatch',
        ),
    )
