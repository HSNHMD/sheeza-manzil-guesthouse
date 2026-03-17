import random
import string
from datetime import datetime, date
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from ..models import db, Booking, Room, Guest, Invoice
from ..services.whatsapp import (
    send_booking_confirmation,
    send_checkin_reminder,
    send_checkout_invoice_summary,
)

bookings_bp = Blueprint('bookings', __name__, url_prefix='/bookings')


def generate_booking_ref():
    chars = string.ascii_uppercase + string.digits
    while True:
        ref = 'BK' + ''.join(random.choices(chars, k=6))
        if not Booking.query.filter_by(booking_ref=ref).first():
            return ref


def check_room_availability(room_id, check_in, check_out, exclude_booking_id=None):
    query = Booking.query.filter(
        Booking.room_id == room_id,
        Booking.status.in_(['confirmed', 'checked_in']),
        Booking.check_in_date < check_out,
        Booking.check_out_date > check_in
    )
    if exclude_booking_id:
        query = query.filter(Booking.id != exclude_booking_id)
    return query.first() is None


@bookings_bp.route('/')
@login_required
def index():
    status_filter = request.args.get('status', '')
    date_filter = request.args.get('date', '')
    search = request.args.get('search', '').strip()

    query = Booking.query.join(Guest).join(Room)

    if status_filter == 'unpaid':
        # bookings with no invoice or invoice.payment_status == 'unpaid', active only
        query = query.outerjoin(Invoice, Invoice.booking_id == Booking.id).filter(
            Booking.status.in_(['confirmed', 'checked_in']),
            db.or_(Invoice.id == None, Invoice.payment_status == 'unpaid')
        )
    elif status_filter:
        query = query.filter(Booking.status == status_filter)
    if date_filter:
        filter_date = date.fromisoformat(date_filter)
        query = query.filter(
            Booking.check_in_date <= filter_date,
            Booking.check_out_date > filter_date
        )
    if search:
        query = query.filter(
            db.or_(
                Guest.first_name.ilike(f'%{search}%'),
                Guest.last_name.ilike(f'%{search}%'),
                Guest.email.ilike(f'%{search}%'),
                Booking.booking_ref.ilike(f'%{search}%'),
                Room.number.ilike(f'%{search}%')
            )
        )

    bookings = query.order_by(Booking.check_in_date.desc()).all()

    today = date.today()
    arrivals_today = Booking.query.filter_by(check_in_date=today, status='confirmed').count()
    departures_today = Booking.query.filter_by(check_out_date=today, status='checked_in').count()
    in_house = Booking.query.filter_by(status='checked_in').count()

    return render_template('bookings/index.html', bookings=bookings,
                           arrivals_today=arrivals_today,
                           departures_today=departures_today,
                           in_house=in_house,
                           status_filter=status_filter,
                           date_filter=date_filter,
                           search=search,
                           today=today)


@bookings_bp.route('/new', methods=['GET', 'POST'])
@login_required
def new():
    rooms = Room.query.filter_by(is_active=True, status='available').order_by(Room.number).all()
    guests = Guest.query.order_by(Guest.last_name).all()

    if request.method == 'POST':
        room_id = int(request.form.get('room_id'))
        check_in = date.fromisoformat(request.form.get('check_in_date'))
        check_out = date.fromisoformat(request.form.get('check_out_date'))

        if check_out <= check_in:
            flash('Check-out must be after check-in.', 'error')
            return render_template('bookings/form.html', rooms=rooms, guests=guests, booking=None)

        if not check_room_availability(room_id, check_in, check_out):
            flash('Room is not available for selected dates.', 'error')
            return render_template('bookings/form.html', rooms=rooms, guests=guests, booking=None)

        # Handle new guest creation inline
        guest_id = request.form.get('guest_id')
        if guest_id == 'new':
            guest = Guest(
                first_name=request.form.get('first_name', '').strip(),
                last_name=request.form.get('last_name', '').strip(),
                email=request.form.get('email', '').strip(),
                phone=request.form.get('phone', '').strip(),
                id_type=request.form.get('id_type', '').strip(),
                id_number=request.form.get('id_number', '').strip(),
                nationality=request.form.get('nationality', '').strip(),
            )
            db.session.add(guest)
            db.session.flush()
        else:
            guest = Guest.query.get_or_404(int(guest_id))

        room = Room.query.get_or_404(room_id)
        nights = (check_out - check_in).days
        total = nights * room.price_per_night

        booking = Booking(
            booking_ref=generate_booking_ref(),
            room_id=room_id,
            guest_id=guest.id,
            check_in_date=check_in,
            check_out_date=check_out,
            num_guests=int(request.form.get('num_guests', 1)),
            special_requests=request.form.get('special_requests', '').strip(),
            total_amount=total,
            created_by=current_user.id,
            status='confirmed'
        )
        db.session.add(booking)
        db.session.commit()
        send_booking_confirmation(booking)
        flash(f'Booking {booking.booking_ref} created successfully.', 'success')
        return redirect(url_for('bookings.detail', booking_id=booking.id))

    return render_template('bookings/form.html', rooms=rooms, guests=guests, booking=None)


@bookings_bp.route('/<int:booking_id>')
@login_required
def detail(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    return render_template('bookings/detail.html', booking=booking)


@bookings_bp.route('/<int:booking_id>/checkin', methods=['POST'])
@login_required
def checkin(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if booking.status != 'confirmed':
        flash('Only confirmed bookings can be checked in.', 'error')
        return redirect(url_for('bookings.detail', booking_id=booking_id))

    if not booking.invoice or booking.invoice.payment_status == 'unpaid':
        flash('Payment required before check-in. Please record a payment first.', 'error')
        return redirect(url_for('bookings.detail', booking_id=booking_id))

    booking.status = 'checked_in'
    booking.actual_check_in = datetime.utcnow()
    booking.room.status = 'occupied'
    db.session.commit()
    send_checkin_reminder(booking)
    flash(f'Guest {booking.guest.full_name} checked in to Room {booking.room.number}.', 'success')
    return redirect(url_for('bookings.detail', booking_id=booking_id))


@bookings_bp.route('/<int:booking_id>/checkout', methods=['POST'])
@login_required
def checkout(booking_id):
    from .invoices import generate_invoice
    booking = Booking.query.get_or_404(booking_id)
    if booking.status != 'checked_in':
        flash('Only checked-in bookings can be checked out.', 'error')
        return redirect(url_for('bookings.detail', booking_id=booking_id))

    booking.status = 'checked_out'
    booking.actual_check_out = datetime.utcnow()
    booking.room.status = 'cleaning'
    booking.room.housekeeping_status = 'dirty'

    invoice = generate_invoice(booking)
    db.session.commit()
    send_checkout_invoice_summary(booking, invoice)
    flash(f'Guest {booking.guest.full_name} checked out. Invoice {invoice.invoice_number} generated.', 'success')
    return redirect(url_for('invoices.detail', invoice_id=invoice.id))


@bookings_bp.route('/<int:booking_id>/payment', methods=['POST'])
@login_required
def record_payment(booking_id):
    """Record advance payment on a confirmed booking, creating invoice if needed."""
    from .invoices import generate_invoice
    booking = Booking.query.get_or_404(booking_id)

    if not booking.invoice:
        generate_invoice(booking)

    amount = float(request.form.get('amount', 0))
    method = request.form.get('payment_method', 'cash')
    inv = booking.invoice
    inv.amount_paid = min(inv.amount_paid + amount, inv.total_amount)
    inv.payment_method = method
    if inv.amount_paid >= inv.total_amount:
        inv.payment_status = 'paid'
    elif inv.amount_paid > 0:
        inv.payment_status = 'partial'

    db.session.commit()
    flash(f'Payment of MVR {amount:.0f} recorded ({method}). Status: {inv.payment_status}.', 'success')
    return redirect(url_for('bookings.detail', booking_id=booking_id))


@bookings_bp.route('/<int:booking_id>/cancel', methods=['POST'])
@login_required
def cancel(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if booking.status in ('checked_out', 'cancelled'):
        flash('Cannot cancel this booking.', 'error')
        return redirect(url_for('bookings.detail', booking_id=booking_id))

    booking.status = 'cancelled'
    if booking.room.status == 'occupied':
        booking.room.status = 'available'
    db.session.commit()
    flash(f'Booking {booking.booking_ref} cancelled.', 'success')
    return redirect(url_for('bookings.index'))
