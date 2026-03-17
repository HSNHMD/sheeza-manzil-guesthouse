from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from ..models import db, Room
from datetime import date

rooms_bp = Blueprint('rooms', __name__, url_prefix='/rooms')

ROOM_TYPES = ['Single', 'Double', 'Twin', 'Suite', 'Deluxe', 'Family']
ROOM_STATUSES = ['available', 'occupied', 'maintenance', 'cleaning']


@rooms_bp.route('/')
@login_required
def index():
    status_filter = request.args.get('status', '')
    type_filter = request.args.get('type', '')
    floor_filter = request.args.get('floor', '')

    query = Room.query.filter_by(is_active=True)
    if status_filter:
        query = query.filter_by(status=status_filter)
    if type_filter:
        query = query.filter_by(room_type=type_filter)
    if floor_filter:
        query = query.filter_by(floor=int(floor_filter))

    rooms = query.order_by(Room.floor, Room.number).all()
    floors = db.session.query(Room.floor).distinct().order_by(Room.floor).all()
    floors = [f[0] for f in floors]

    stats = {
        'total': Room.query.filter_by(is_active=True).count(),
        'available': Room.query.filter_by(is_active=True, status='available').count(),
        'occupied': Room.query.filter_by(is_active=True, status='occupied').count(),
        'maintenance': Room.query.filter_by(is_active=True, status='maintenance').count(),
        'cleaning': Room.query.filter_by(is_active=True, status='cleaning').count(),
    }

    return render_template('rooms/index.html', rooms=rooms, stats=stats,
                           room_types=ROOM_TYPES, floors=floors,
                           status_filter=status_filter, type_filter=type_filter,
                           floor_filter=floor_filter)


@rooms_bp.route('/new', methods=['GET', 'POST'])
@login_required
def new():
    if not current_user.is_admin:
        flash('Admin access required.', 'error')
        return redirect(url_for('rooms.index'))

    if request.method == 'POST':
        number = request.form.get('number', '').strip()
        if Room.query.filter_by(number=number).first():
            flash(f'Room {number} already exists.', 'error')
            return render_template('rooms/form.html', room=None, room_types=ROOM_TYPES)

        room = Room(
            number=number,
            name=request.form.get('name', '').strip(),
            room_type=request.form.get('room_type'),
            floor=int(request.form.get('floor', 1)),
            capacity=int(request.form.get('capacity', 1)),
            price_per_night=float(request.form.get('price_per_night', 0)),
            description=request.form.get('description', '').strip(),
            amenities=request.form.get('amenities', '').strip(),
        )
        db.session.add(room)
        db.session.commit()
        flash(f'Room {room.number} created successfully.', 'success')
        return redirect(url_for('rooms.index'))

    return render_template('rooms/form.html', room=None, room_types=ROOM_TYPES)


@rooms_bp.route('/<int:room_id>/edit', methods=['GET', 'POST'])
@login_required
def edit(room_id):
    if not current_user.is_admin:
        flash('Admin access required.', 'error')
        return redirect(url_for('rooms.index'))

    room = Room.query.get_or_404(room_id)

    if request.method == 'POST':
        room.name = request.form.get('name', '').strip()
        room.room_type = request.form.get('room_type')
        room.floor = int(request.form.get('floor', 1))
        room.capacity = int(request.form.get('capacity', 1))
        room.price_per_night = float(request.form.get('price_per_night', 0))
        room.description = request.form.get('description', '').strip()
        room.amenities = request.form.get('amenities', '').strip()
        db.session.commit()
        flash(f'Room {room.number} updated.', 'success')
        return redirect(url_for('rooms.index'))

    return render_template('rooms/form.html', room=room, room_types=ROOM_TYPES)


@rooms_bp.route('/<int:room_id>/status', methods=['POST'])
@login_required
def update_status(room_id):
    room = Room.query.get_or_404(room_id)
    new_status = request.form.get('status')
    if new_status in ROOM_STATUSES:
        room.status = new_status
        db.session.commit()
        flash(f'Room {room.number} status updated to {new_status}.', 'success')
    return redirect(url_for('rooms.index'))


@rooms_bp.route('/<int:room_id>/delete', methods=['POST'])
@login_required
def delete(room_id):
    if not current_user.is_admin:
        flash('Admin access required.', 'error')
        return redirect(url_for('rooms.index'))

    room = Room.query.get_or_404(room_id)
    room.is_active = False
    db.session.commit()
    flash(f'Room {room.number} removed.', 'success')
    return redirect(url_for('rooms.index'))
