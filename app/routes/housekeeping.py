"""Housekeeping V1 — board, status changes, assignment.

Endpoints:

    GET  /housekeeping/                — board (filterable by view + floor)
    POST /housekeeping/update/<room_id>     — single-room status change
    POST /housekeeping/assign/<room_id>     — assign cleaning task
    POST /housekeeping/bulk                 — bulk status change

Hard rules:

  - login_required on all endpoints. Staff users are also allowed
    (the `_staff_guard` whitelist now includes `/housekeeping`),
    because housekeeping IS the primary staff workflow.
  - All status mutations go through services.housekeeping so the
    canonical 5-state vocabulary + ActivityLog wiring + HousekeepingLog
    legacy-log are always written.
  - The route handlers do NOT mutate Room.status. Operational status
    is owned by Front Office.
  - No WhatsApp / email / Gemini side effects.
"""

from __future__ import annotations

from datetime import date, timedelta

from flask import (
    Blueprint, render_template, redirect, url_for, flash, request,
    jsonify,
)
from flask_login import login_required, current_user

from ..models import db, Room, HousekeepingLog, User, Booking
from ..services.housekeeping import (
    set_room_status, assign_task,
    HK_STATUSES, HK_STATUS_LABELS, hk_badge, is_valid_status,
)


housekeeping_bp = Blueprint('housekeeping', __name__,
                             url_prefix='/housekeeping')


# ── GET /housekeeping/ ──────────────────────────────────────────────

@housekeeping_bp.route('/', methods=['GET'])
@login_required
def index():
    """Mobile-friendly housekeeping board.

    Query params:
      view    — all | dirty | clean | inspected | out_of_order | mine
      floor   — integer floor filter
    """
    view  = (request.args.get('view') or 'all').strip().lower()
    floor = (request.args.get('floor') or '').strip()

    query = Room.query.filter_by(is_active=True)

    if view in HK_STATUSES:
        query = query.filter(Room.housekeeping_status == view)
    elif view == 'mine':
        query = query.filter(Room.assigned_to_user_id == current_user.id)
    # 'all' or anything else → no status filter

    if floor:
        try:
            query = query.filter(Room.floor == int(floor))
        except ValueError:
            pass

    rooms = query.order_by(Room.floor, Room.number).all()

    # Stats — always over the full active fleet, ignoring filters
    fleet = Room.query.filter_by(is_active=True).all()
    stats = {
        'total':        len(fleet),
        'clean':        sum(1 for r in fleet if r.housekeeping_status == 'clean'),
        'dirty':        sum(1 for r in fleet if r.housekeeping_status == 'dirty'),
        'in_progress':  sum(1 for r in fleet if r.housekeeping_status == 'in_progress'),
        'inspected':    sum(1 for r in fleet if r.housekeeping_status == 'inspected'),
        'out_of_order': sum(1 for r in fleet if r.housekeeping_status == 'out_of_order'),
        'mine':         sum(1 for r in fleet
                            if r.assigned_to_user_id == current_user.id),
    }

    floors = sorted({r.floor for r in fleet})

    # Today's checkouts → "due out" hint
    from ..services.night_audit import current_business_date
    bd = current_business_date()
    due_out_room_ids = {
        b.room_id for b in
        Booking.query
        .filter(Booking.check_out_date == bd)
        .filter(Booking.status.in_(('checked_in', 'confirmed', 'payment_verified')))
        .all()
    }
    just_out_room_ids = {
        b.room_id for b in
        Booking.query
        .filter(Booking.check_out_date == bd - timedelta(days=1))
        .filter(Booking.status == 'checked_out')
        .all()
    }

    # Dropdown of staff to assign cleaning to. Include admins too —
    # in practice an admin sometimes does a same-day inspection.
    assignable_users = (
        User.query
        .filter(User.is_active.is_(True))
        .filter(User.role.in_(('staff', 'admin')))
        .order_by(User.username)
        .all()
    )

    recent_logs = (
        HousekeepingLog.query
        .order_by(HousekeepingLog.created_at.desc())
        .limit(20)
        .all()
    )

    return render_template(
        'housekeeping/index.html',
        rooms=rooms,
        stats=stats,
        floors=floors,
        view=view,
        floor_filter=floor,
        recent_logs=recent_logs,
        hk_statuses=HK_STATUSES,
        hk_status_labels=HK_STATUS_LABELS,
        hk_badge=hk_badge,
        due_out_room_ids=due_out_room_ids,
        just_out_room_ids=just_out_room_ids,
        assignable_users=assignable_users,
        business_date=bd,
    )


# ── POST /housekeeping/update/<room_id> ─────────────────────────────

@housekeeping_bp.route('/update/<int:room_id>', methods=['POST'])
@login_required
def update(room_id):
    """Change a single room's housekeeping status.

    Form input:
        new_status — one of HK_STATUSES (required)
        notes      — optional, ≤500 chars
    """
    room = Room.query.get_or_404(room_id)
    new_status = (request.form.get('new_status') or '').strip()
    notes = (request.form.get('notes') or '').strip() or None
    if notes and len(notes) > 500:
        notes = notes[:500]

    if not is_valid_status(new_status):
        flash(f'Invalid housekeeping status: {new_status!r}.', 'error')
        return redirect(url_for('housekeeping.index'))

    result = set_room_status(room, new_status, user=current_user, notes=notes)
    if not result['ok']:
        flash(result['error'], 'error')
        return redirect(url_for('housekeeping.index'))

    db.session.commit()
    flash(
        f'Room {room.number}: '
        f'{result["old_status"]} → {result["new_status"]}.',
        'success',
    )
    return redirect(url_for('housekeeping.index',
                            view=request.args.get('view') or 'all'))


# ── POST /housekeeping/assign/<room_id> ─────────────────────────────

@housekeeping_bp.route('/assign/<int:room_id>', methods=['POST'])
@login_required
def assign(room_id):
    """Assign (or clear) a cleaning task on a room.

    Form input:
        assignee_user_id — int, or empty/'0' to clear
        notes            — optional
    """
    room = Room.query.get_or_404(room_id)
    raw = (request.form.get('assignee_user_id') or '').strip()

    assignee = None
    if raw and raw != '0':
        try:
            assignee = User.query.get(int(raw))
        except ValueError:
            assignee = None
        if assignee is None:
            flash('Invalid assignee.', 'error')
            return redirect(url_for('housekeeping.index'))

    assign_task(room, assignee=assignee, by_user=current_user)
    db.session.commit()

    if assignee:
        flash(f'Room {room.number} assigned to {assignee.username}.', 'success')
    else:
        flash(f'Room {room.number} assignment cleared.', 'success')
    return redirect(url_for('housekeeping.index',
                            view=request.args.get('view') or 'all'))


# ── POST /housekeeping/bulk ─────────────────────────────────────────

@housekeeping_bp.route('/bulk', methods=['POST'])
@login_required
def bulk_update():
    """Apply the same housekeeping status change to many rooms.

    Form input:
        room_ids[]  — multiple room ids
        new_status  — one of HK_STATUSES
        notes       — optional
    """
    room_ids = request.form.getlist('room_ids')
    new_status = (request.form.get('new_status') or '').strip()
    notes = (request.form.get('notes') or '').strip() or None

    if not is_valid_status(new_status):
        flash(f'Invalid housekeeping status: {new_status!r}.', 'error')
        return redirect(url_for('housekeeping.index'))

    n = 0
    for rid in room_ids:
        try:
            room = Room.query.get(int(rid))
        except ValueError:
            continue
        if not room:
            continue
        result = set_room_status(room, new_status,
                                 user=current_user, notes=notes)
        if result['ok']:
            n += 1

    db.session.commit()
    flash(f'{n} room{"s" if n != 1 else ""} updated to {new_status}.',
          'success')
    return redirect(url_for('housekeeping.index'))
