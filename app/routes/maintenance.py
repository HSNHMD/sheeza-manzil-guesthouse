"""Maintenance / Work Orders V1 — route handlers.

GET /maintenance               — list view with filters
GET /maintenance/<id>          — detail page + lifecycle actions
POST /maintenance              — create
POST /maintenance/<id>/assign  — assign / unassign
POST /maintenance/<id>/status  — status transition (incl. resolve)
POST /maintenance/<id>/mark-room-ooo — flip linked room to OOO

All routes are admin-only at the route level. Non-admin staff with
department='housekeeping' or 'front_office' bounce off the
staff_guard since /maintenance isn't whitelisted there — V1 keeps
maintenance an admin/manager surface. (Per-department permissions
are flagged for a later sprint.)
"""

from __future__ import annotations

from datetime import datetime
from flask import (Blueprint, render_template, redirect, url_for, flash,
                   request, abort)
from flask_login import login_required, current_user

from ..decorators import admin_required
from ..models import db, WorkOrder, Room, User, Booking
from ..services import maintenance as svc


maintenance_bp = Blueprint('maintenance', __name__,
                           url_prefix='/maintenance')


@maintenance_bp.route('/', methods=['GET'])
@login_required
@admin_required
def index():
    """Filtered list of work orders + KPI tiles."""
    open_only = request.args.get('open') == '1'
    priority  = (request.args.get('priority') or '').strip() or None
    status    = (request.args.get('status') or '').strip() or None
    room_id_raw = (request.args.get('room_id') or '').strip()
    room_id = int(room_id_raw) if room_id_raw.isdigit() else None
    assigned_raw = (request.args.get('assigned_to') or '').strip()
    assigned_to_user_id = int(assigned_raw) if assigned_raw.isdigit() else None

    rows = svc.list_work_orders(
        open_only=open_only, priority=priority, status=status,
        room_id=room_id, assigned_to_user_id=assigned_to_user_id,
    )
    summary = svc.summary_counts()
    rooms = Room.query.filter_by(is_active=True).order_by(Room.number).all()
    users = (User.query
             .filter(User.is_active.is_(True))
             .order_by(User.username).all())

    return render_template(
        'maintenance/index.html',
        rows=rows, summary=summary,
        rooms=rooms, users=users,
        # Echo current filters so the form keeps state
        open_only=open_only, priority=priority, status=status,
        room_id=room_id, assigned_to_user_id=assigned_to_user_id,
        # Vocabulary
        categories=WorkOrder.CATEGORIES,
        priorities=WorkOrder.PRIORITIES,
        statuses=WorkOrder.STATUSES,
    )


@maintenance_bp.route('/', methods=['POST'])
@login_required
@admin_required
def create():
    """Create a work order from the list-page modal."""
    title = request.form.get('title', '').strip()
    category = request.form.get('category', 'general')
    priority = request.form.get('priority', 'medium')
    description = request.form.get('description', '').strip() or None

    room_id_raw = request.form.get('room_id') or ''
    room_id = int(room_id_raw) if room_id_raw.isdigit() else None
    booking_id_raw = request.form.get('booking_id') or ''
    booking_id = int(booking_id_raw) if booking_id_raw.isdigit() else None
    assigned_raw = request.form.get('assigned_to_user_id') or ''
    assigned_to_user_id = int(assigned_raw) if assigned_raw.isdigit() else None

    due_raw = (request.form.get('due_date') or '').strip()
    due_date = None
    if due_raw:
        try:
            due_date = datetime.strptime(due_raw, '%Y-%m-%d').date()
        except ValueError:
            flash('Due date must be YYYY-MM-DD.', 'error')
            return redirect(url_for('maintenance.index'))

    result = svc.create_work_order(
        title=title, category=category, priority=priority,
        description=description, room_id=room_id, booking_id=booking_id,
        assigned_to_user_id=assigned_to_user_id,
        reported_by_user_id=current_user.id,
        due_date=due_date,
        actor_user_id=current_user.id,
    )
    flash(result.message, 'success' if result.ok else 'error')
    if result.ok and result.work_order:
        return redirect(url_for('maintenance.detail',
                                wo_id=result.work_order.id))
    return redirect(url_for('maintenance.index'))


@maintenance_bp.route('/<int:wo_id>', methods=['GET'])
@login_required
@admin_required
def detail(wo_id):
    wo = WorkOrder.query.get(wo_id)
    if wo is None:
        abort(404)
    rooms = Room.query.filter_by(is_active=True).order_by(Room.number).all()
    users = (User.query
             .filter(User.is_active.is_(True))
             .order_by(User.username).all())
    # Recent activity for this work order (audit trail)
    from ..models import ActivityLog
    activity = (ActivityLog.query
                .filter(ActivityLog.action.in_(
                    ('maintenance.created', 'maintenance.assigned',
                     'maintenance.status_changed', 'maintenance.resolved',
                     'maintenance.room_out_of_order')))
                .order_by(ActivityLog.created_at.desc())
                .limit(50).all())
    # Filter to entries that mention this work_order_id in metadata.
    # Cheap client-side filter — there are typically a handful of rows.
    import json
    relevant = []
    for a in activity:
        try:
            md = json.loads(a.metadata_json or '{}')
        except (TypeError, ValueError):
            md = {}
        if md.get('work_order_id') == wo.id:
            relevant.append(a)
    return render_template(
        'maintenance/detail.html',
        wo=wo, rooms=rooms, users=users,
        activity=relevant,
        categories=WorkOrder.CATEGORIES,
        priorities=WorkOrder.PRIORITIES,
        statuses=WorkOrder.STATUSES,
    )


@maintenance_bp.route('/<int:wo_id>/assign', methods=['POST'])
@login_required
@admin_required
def assign(wo_id):
    raw = request.form.get('assigned_to_user_id') or ''
    user_id = int(raw) if raw.isdigit() else None
    result = svc.assign(work_order_id=wo_id, user_id=user_id,
                        actor_user_id=current_user.id)
    flash(result.message, 'success' if result.ok else 'error')
    return redirect(url_for('maintenance.detail', wo_id=wo_id))


@maintenance_bp.route('/<int:wo_id>/status', methods=['POST'])
@login_required
@admin_required
def update_status(wo_id):
    new_status = request.form.get('status', '').strip()
    notes = request.form.get('resolution_notes', '').strip() or None
    result = svc.update_status(
        work_order_id=wo_id, new_status=new_status,
        resolution_notes=notes,
        actor_user_id=current_user.id,
    )
    flash(result.message, 'success' if result.ok else 'error')
    return redirect(url_for('maintenance.detail', wo_id=wo_id))


@maintenance_bp.route('/<int:wo_id>/mark-room-ooo', methods=['POST'])
@login_required
@admin_required
def mark_room_ooo(wo_id):
    result = svc.mark_room_out_of_order(
        work_order_id=wo_id, actor_user_id=current_user.id,
    )
    flash(result.message, 'success' if result.ok else 'error')
    return redirect(url_for('maintenance.detail', wo_id=wo_id))
