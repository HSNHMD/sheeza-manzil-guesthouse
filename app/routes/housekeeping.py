from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from ..models import db, Room, HousekeepingLog

housekeeping_bp = Blueprint('housekeeping', __name__, url_prefix='/housekeeping')

HK_STATUSES = ['clean', 'dirty', 'in_progress']
HK_ACTIONS = ['started_cleaning', 'completed', 'inspected', 'maintenance_request']


@housekeeping_bp.route('/')
@login_required
def index():
    status_filter = request.args.get('status', '')
    floor_filter = request.args.get('floor', '')

    query = Room.query.filter_by(is_active=True)
    if status_filter:
        query = query.filter_by(housekeeping_status=status_filter)
    if floor_filter:
        query = query.filter_by(floor=int(floor_filter))

    rooms = query.order_by(Room.floor, Room.number).all()
    floors = db.session.query(Room.floor).distinct().order_by(Room.floor).all()
    floors = [f[0] for f in floors]

    stats = {
        'clean': Room.query.filter_by(is_active=True, housekeeping_status='clean').count(),
        'dirty': Room.query.filter_by(is_active=True, housekeeping_status='dirty').count(),
        'in_progress': Room.query.filter_by(is_active=True, housekeeping_status='in_progress').count(),
    }

    recent_logs = HousekeepingLog.query.order_by(
        HousekeepingLog.created_at.desc()).limit(20).all()

    return render_template('housekeeping/index.html', rooms=rooms, stats=stats,
                           floors=floors, status_filter=status_filter,
                           floor_filter=floor_filter, recent_logs=recent_logs,
                           hk_statuses=HK_STATUSES)


@housekeeping_bp.route('/update/<int:room_id>', methods=['POST'])
@login_required
def update(room_id):
    room = Room.query.get_or_404(room_id)
    action = request.form.get('action')
    notes = request.form.get('notes', '').strip()

    if action == 'started_cleaning':
        room.housekeeping_status = 'in_progress'
        if room.status == 'cleaning':
            pass  # keep status as cleaning until inspected
    elif action == 'completed':
        room.housekeeping_status = 'clean'
        if room.status == 'cleaning':
            room.status = 'available'
    elif action == 'inspected':
        room.housekeeping_status = 'clean'
        if room.status == 'cleaning':
            room.status = 'available'
    elif action == 'maintenance_request':
        room.status = 'maintenance'
        room.housekeeping_status = 'dirty'

    log = HousekeepingLog(
        room_id=room.id,
        staff_id=current_user.id,
        action=action,
        notes=notes
    )
    db.session.add(log)
    db.session.commit()
    flash(f'Room {room.number}: {action.replace("_", " ").title()}', 'success')
    return redirect(url_for('housekeeping.index'))


@housekeeping_bp.route('/bulk', methods=['POST'])
@login_required
def bulk_update():
    room_ids = request.form.getlist('room_ids')
    action = request.form.get('action')
    notes = request.form.get('notes', '').strip()

    for room_id in room_ids:
        room = Room.query.get(room_id)
        if not room:
            continue
        if action == 'mark_clean':
            room.housekeeping_status = 'clean'
            if room.status == 'cleaning':
                room.status = 'available'
        elif action == 'mark_dirty':
            room.housekeeping_status = 'dirty'
        log = HousekeepingLog(room_id=room.id, staff_id=current_user.id,
                              action=action, notes=notes)
        db.session.add(log)

    db.session.commit()
    flash(f'{len(room_ids)} rooms updated.', 'success')
    return redirect(url_for('housekeeping.index'))
