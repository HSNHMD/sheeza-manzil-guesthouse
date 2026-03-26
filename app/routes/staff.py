"""
Staff portal blueprint — minimal front-desk operations.

Routes:
    GET  /staff/login           — staff login page
    POST /staff/login           — authenticate staff, redirect to dashboard
    GET  /staff/logout          — log out, redirect to staff login
    GET  /staff/dashboard       — 8-room occupancy overview
    POST /staff/room/<id>/checkin       — AJAX: check in guest (JSON)
    POST /staff/room/<id>/checkout      — AJAX: check out guest (JSON)
    POST /staff/room/<id>/housekeeping  — AJAX: update HK status (JSON)
"""

from datetime import datetime
from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, jsonify)
from flask_login import login_user, logout_user, login_required, current_user
from ..models import db, User, Room, Booking
from ..utils import hotel_date
from ..services.whatsapp import send_checkin_reminder, send_checkout_invoice_summary

staff_bp = Blueprint('staff', __name__, url_prefix='/staff')


# ── Auth ────────────────────────────────────────────────────────────────────

@staff_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        if current_user.is_admin:
            return redirect(url_for('rooms.index'))
        return redirect(url_for('staff.dashboard'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()
        if user and user.is_active and user.check_password(password):
            login_user(user)
            if user.is_admin:
                return redirect(url_for('rooms.index'))
            return redirect(url_for('staff.dashboard'))
        flash('Invalid username or password.', 'error')

    return render_template('staff/login.html')


@staff_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('staff.login'))


# ── Dashboard ────────────────────────────────────────────────────────────────

@staff_bp.route('/dashboard')
@login_required
def dashboard():
    rooms = Room.query.filter_by(is_active=True).order_by(Room.number).all()
    today = hotel_date()

    # For each room, attach the most relevant booking (checked_in OR arriving today)
    room_data = []
    for room in rooms:
        booking = room.current_booking  # checked_in booking
        if not booking:
            # Look for a confirmed arrival today
            booking = Booking.query.filter(
                Booking.room_id == room.id,
                Booking.status == 'confirmed',
                Booking.check_in_date == today,
            ).first()
        room_data.append({'room': room, 'booking': booking})

    return render_template('staff/dashboard.html', room_data=room_data, today=today)


# ── AJAX room actions ─────────────────────────────────────────────────────────

@staff_bp.route('/room/<int:room_id>/checkin', methods=['POST'])
@login_required
def checkin(room_id):
    room = Room.query.get_or_404(room_id)
    today = hotel_date()

    booking = Booking.query.filter(
        Booking.room_id == room_id,
        Booking.status == 'confirmed',
        Booking.check_in_date <= today,
        Booking.check_out_date > today,
    ).first()

    if not booking:
        return jsonify(success=False, error='No confirmed booking found for today.')

    if not booking.invoice or booking.invoice.payment_status == 'unpaid':
        return jsonify(success=False, error='Payment required before check-in.')

    booking.status = 'checked_in'
    booking.actual_check_in = datetime.utcnow()
    room.status = 'occupied'
    db.session.commit()

    try:
        send_checkin_reminder(booking)
    except Exception:
        pass  # WhatsApp failure must not block a completed check-in

    return jsonify(
        success=True,
        room_status='occupied',
        booking_status='checked_in',
        guest_name=booking.guest.full_name,
        checkout_date=booking.check_out_date.strftime('%d %b %Y'),
        booking_ref=booking.booking_ref,
    )


@staff_bp.route('/room/<int:room_id>/checkout', methods=['POST'])
@login_required
def checkout(room_id):
    room = Room.query.get_or_404(room_id)

    booking = Booking.query.filter_by(
        room_id=room_id, status='checked_in'
    ).first()

    if not booking:
        return jsonify(success=False, error='No checked-in booking found for this room.')

    booking.status = 'checked_out'
    booking.actual_check_out = datetime.utcnow()
    room.status = 'cleaning'
    room.housekeeping_status = 'dirty'
    db.session.commit()

    try:
        if booking.invoice:
            send_checkout_invoice_summary(booking, booking.invoice)
    except Exception:
        pass  # WhatsApp failure must not block a completed check-out

    return jsonify(
        success=True,
        room_status='cleaning',
        housekeeping_status='dirty',
    )


@staff_bp.route('/room/<int:room_id>/housekeeping', methods=['POST'])
@login_required
def housekeeping(room_id):
    room = Room.query.get_or_404(room_id)
    status = request.form.get('status', '').strip()

    valid = {'clean', 'dirty', 'in_progress'}
    if status not in valid:
        return jsonify(success=False, error=f'Invalid status. Use: {", ".join(valid)}')

    room.housekeeping_status = status
    # When marked clean, update room status to available if it was cleaning
    if status == 'clean' and room.status == 'cleaning':
        room.status = 'available'
    db.session.commit()

    return jsonify(success=True, housekeeping_status=status, room_status=room.status)
