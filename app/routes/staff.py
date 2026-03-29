"""
Staff portal blueprint — minimal front-desk operations.

Routes:
    GET  /staff/login                   — staff login page
    POST /staff/login                   — authenticate staff, redirect to dashboard
    GET  /staff/logout                  — log out, redirect to staff login
    GET  /staff/dashboard               — 8-room occupancy overview (card grid)
    GET  /staff/room/<id>               — full-page room detail
    POST /staff/checkin/<id>            — walk-in form: create Guest+Booking+Invoice, check in
    POST /staff/checkout/<id>           — AJAX: check out guest (JSON)
    POST /staff/housekeeping/<id>       — AJAX: update HK status (JSON)
    POST /staff/note/<id>               — AJAX: save room notes (JSON)
    POST /staff/maintenance/<id>        — AJAX: set room maintenance status (JSON)

    Legacy AJAX routes (kept for backwards compatibility):
    POST /staff/room/<id>/checkin       — AJAX: check in confirmed booking (JSON)
    POST /staff/room/<id>/checkout      — AJAX: check out guest (JSON)
    POST /staff/room/<id>/housekeeping  — AJAX: update HK status (JSON)
"""

import time
from datetime import datetime, date
from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, jsonify)
from flask_login import login_user, logout_user, login_required, current_user
from ..models import db, User, Room, Booking, Guest, Invoice
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
    rooms = Room.query.filter(Room.is_active.isnot(False)).order_by(Room.number).all()
    today = hotel_date()

    return render_template('staff/dashboard.html', rooms=rooms, today=today)


# ── Room Detail ───────────────────────────────────────────────────────────────

@staff_bp.route('/room/<int:room_id>')
@login_required
def room_detail(room_id):
    room = Room.query.get_or_404(room_id)
    today = hotel_date()

    booking = room.current_booking
    if not booking:
        booking = Booking.query.filter(
            Booking.room_id == room_id,
            Booking.status == 'confirmed',
            Booking.check_in_date <= today,
            Booking.check_out_date > today,
        ).first()

    return render_template('staff/room.html', room=room, booking=booking, today=today)


# ── Walk-in Check-in (form submit → redirect) ─────────────────────────────────

@staff_bp.route('/checkin/<int:room_id>', methods=['POST'])
@login_required
def do_checkin(room_id):
    room = Room.query.get_or_404(room_id)

    guest_name = request.form.get('guest_name', '').strip()
    check_in_str = request.form.get('check_in_date', '').strip()
    check_out_str = request.form.get('check_out_date', '').strip()
    payment_status = request.form.get('payment_status', 'unpaid').strip()

    if not guest_name:
        flash('Guest name is required.', 'error')
        return redirect(url_for('staff.room_detail', room_id=room_id))

    try:
        check_in_date = datetime.strptime(check_in_str, '%Y-%m-%d').date()
        check_out_date = datetime.strptime(check_out_str, '%Y-%m-%d').date()
    except ValueError:
        flash('Invalid date format.', 'error')
        return redirect(url_for('staff.room_detail', room_id=room_id))

    if check_out_date <= check_in_date:
        flash('Check-out must be after check-in.', 'error')
        return redirect(url_for('staff.room_detail', room_id=room_id))

    # Split name
    parts = guest_name.split(None, 1)
    first_name = parts[0]
    last_name = parts[1] if len(parts) > 1 else '.'

    ts = int(time.time())
    booking_ref = f'WI{ts}'
    invoice_number = f'INV-WI{ts}'

    try:
        guest = Guest(first_name=first_name, last_name=last_name)
        db.session.add(guest)
        db.session.flush()  # get guest.id

        nights = (check_out_date - check_in_date).days
        total = nights * room.price_per_night

        booking = Booking(
            booking_ref=booking_ref,
            room_id=room_id,
            guest_id=guest.id,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            status='checked_in',
            actual_check_in=datetime.utcnow(),
            total_amount=total,
            created_by=current_user.id,
        )
        db.session.add(booking)
        db.session.flush()  # get booking.id

        invoice = Invoice(
            invoice_number=invoice_number,
            booking_id=booking.id,
            subtotal=total,
            total_amount=total,
            payment_status=payment_status,
            amount_paid=total if payment_status == 'paid' else 0.0,
        )
        db.session.add(invoice)

        room.status = 'occupied'
        db.session.commit()

    except Exception:
        db.session.rollback()
        flash('Failed to create walk-in booking. Please try again.', 'error')
        return redirect(url_for('staff.room_detail', room_id=room_id))

    try:
        send_checkin_reminder(booking)
    except Exception:
        pass

    flash(f'Walk-in check-in for {guest.full_name} completed.', 'success')
    return redirect(url_for('staff.dashboard'))


# ── Room Detail AJAX Actions ───────────────────────────────────────────────────

@staff_bp.route('/checkout/<int:room_id>', methods=['POST'])
@login_required
def do_checkout(room_id):
    room = Room.query.get_or_404(room_id)

    booking = Booking.query.filter_by(room_id=room_id, status='checked_in').first()
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
        pass

    return jsonify(success=True)


@staff_bp.route('/housekeeping/<int:room_id>', methods=['POST'])
@login_required
def do_housekeeping(room_id):
    room = Room.query.get_or_404(room_id)
    status = request.form.get('status', '').strip()

    valid = {'clean', 'dirty', 'in_progress'}
    if status not in valid:
        return jsonify(success=False, error='Invalid status.')

    room.housekeeping_status = status
    if status == 'clean' and room.status == 'cleaning':
        room.status = 'available'
    db.session.commit()

    return jsonify(success=True, housekeeping_status=status, room_status=room.status)


@staff_bp.route('/note/<int:room_id>', methods=['POST'])
@login_required
def save_note(room_id):
    room = Room.query.get_or_404(room_id)
    note = request.form.get('note', '').strip()
    room.notes = note
    db.session.commit()
    return jsonify(success=True)


@staff_bp.route('/maintenance/<int:room_id>', methods=['POST'])
@login_required
def report_maintenance(room_id):
    room = Room.query.get_or_404(room_id)
    if room.status == 'maintenance':
        room.status = 'vacant'
    else:
        room.status = 'maintenance'
    db.session.commit()
    return jsonify(success=True, status=room.status)


# ── Legacy AJAX room actions (kept for backwards compatibility) ────────────────

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
        pass

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

    booking = Booking.query.filter_by(room_id=room_id, status='checked_in').first()
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
        pass

    return jsonify(success=True, room_status='cleaning', housekeeping_status='dirty')


@staff_bp.route('/room/<int:room_id>/housekeeping', methods=['POST'])
@login_required
def housekeeping(room_id):
    room = Room.query.get_or_404(room_id)
    status = request.form.get('status', '').strip()

    valid = {'clean', 'dirty', 'in_progress'}
    if status not in valid:
        return jsonify(success=False, error=f'Invalid status. Use: {", ".join(valid)}')

    room.housekeeping_status = status
    if status == 'clean' and room.status == 'cleaning':
        room.status = 'available'
    db.session.commit()

    return jsonify(success=True, housekeeping_status=status, room_status=room.status)
