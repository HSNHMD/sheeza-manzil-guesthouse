import os
import uuid
from datetime import date
from flask import (Blueprint, render_template, request, jsonify,
                   redirect, url_for, current_app)
from werkzeug.utils import secure_filename
from ..models import db, Booking, Room, Guest
from ..routes.invoices import generate_invoice
from ..routes.bookings import generate_booking_ref
from ..utils import hotel_date

public_bp = Blueprint('public', __name__, url_prefix='')

ALLOWED = {'jpg', 'jpeg', 'png', 'pdf'}


def _allowed(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED


def _save_file(file, prefix, folder_type):
    """Save uploaded file locally and to Google Drive.

    Returns (filename, drive_id).  drive_id is None when Drive is not
    configured or the upload fails — the app falls back to local serving.
    """
    from ..services.drive import upload_file as drive_upload
    ext = file.filename.rsplit('.', 1)[1].lower()
    name = f'{prefix}_{uuid.uuid4().hex[:10]}.{ext}'
    upload_dir = os.path.join(current_app.root_path, 'uploads')
    os.makedirs(upload_dir, exist_ok=True)
    file_bytes = file.read()
    with open(os.path.join(upload_dir, name), 'wb') as fh:
        fh.write(file_bytes)
    drive_id = drive_upload(file_bytes, name, folder_type)
    return name, drive_id


@public_bp.route('/')
def index():
    return render_template('public/book.html', today=hotel_date().isoformat())


@public_bp.route('/availability')
def availability():
    ci = request.args.get('check_in', '')
    co = request.args.get('check_out', '')
    if not ci or not co:
        return jsonify({'rooms': [], 'error': 'Please select both dates.'})
    try:
        check_in = date.fromisoformat(ci)
        check_out = date.fromisoformat(co)
    except ValueError:
        return jsonify({'rooms': [], 'error': 'Invalid dates.'})
    if check_out <= check_in:
        return jsonify({'rooms': [], 'error': 'Check-out must be after check-in.'})

    nights = (check_out - check_in).days
    rooms = Room.query.filter_by(is_active=True).order_by(Room.number).all()
    available = []
    for room in rooms:
        conflict = Booking.query.filter(
            Booking.room_id == room.id,
            Booking.status.in_(['unconfirmed', 'pending_verification', 'confirmed', 'checked_in']),
            Booking.check_in_date < check_out,
            Booking.check_out_date > check_in,
        ).first()
        if not conflict:
            available.append({
                'id': room.id,
                'number': room.number,
                'type': room.room_type,
                'capacity': room.capacity,
                'price': int(room.price_per_night),
                'total': int(nights * room.price_per_night),
                'nights': nights,
            })
    return jsonify({'rooms': available, 'nights': nights})


@public_bp.route('/submit', methods=['POST'])
def submit():
    try:
        room_id   = int(request.form.get('room_id', 0))
        check_in  = date.fromisoformat(request.form.get('check_in_date', ''))
        check_out = date.fromisoformat(request.form.get('check_out_date', ''))
    except (ValueError, TypeError):
        return render_template('public/book.html', today=hotel_date().isoformat(),
                               error='Invalid submission. Please try again.')

    room = Room.query.get_or_404(room_id)

    # Re-validate availability
    conflict = Booking.query.filter(
        Booking.room_id == room_id,
        Booking.status.in_(['unconfirmed', 'pending_verification', 'confirmed', 'checked_in']),
        Booking.check_in_date < check_out,
        Booking.check_out_date > check_in,
    ).first()
    if conflict:
        return render_template('public/book.html', today=hotel_date().isoformat(),
                               error='Sorry, this room is not available for your selected dates. Please choose different dates or a different room.')

    # ID card (required)
    id_file = request.files.get('id_card')
    if not id_file or not id_file.filename or not _allowed(id_file.filename):
        return render_template('public/book.html', today=hotel_date().isoformat(),
                               error='ID card / passport upload is required.')

    first_name = request.form.get('first_name', '').strip()
    last_name  = request.form.get('last_name', '').strip()
    prefix     = secure_filename(f'{first_name}_{last_name}')

    id_card_filename, id_card_drive_id = _save_file(id_file, f'id_{prefix}', 'id_card')

    # Payment slip (optional)
    slip_file = request.files.get('payment_slip')
    payment_slip_filename = None
    payment_slip_drive_id = None
    if slip_file and slip_file.filename and _allowed(slip_file.filename):
        payment_slip_filename, payment_slip_drive_id = _save_file(slip_file, f'slip_{prefix}', 'payment_slip')

    # ── Initial lifecycle states (per booking_lifecycle.VALID_STATUS_PAIRS) ──
    # Guest uploaded a slip → booking_status='payment_uploaded', invoice.payment_status='pending_review'
    # No slip uploaded     → booking_status='pending_payment',   invoice.payment_status='not_received'
    if payment_slip_filename:
        booking_status = 'payment_uploaded'
        invoice_payment_status = 'pending_review'
    else:
        booking_status = 'pending_payment'
        invoice_payment_status = 'not_received'
    nights = (check_out - check_in).days

    guest = Guest(
        first_name=first_name,
        last_name=last_name,
        phone=request.form.get('phone', '').strip(),
        nationality=request.form.get('nationality', '').strip(),
    )
    db.session.add(guest)
    db.session.flush()

    booking = Booking(
        booking_ref=generate_booking_ref(),
        room_id=room_id,
        guest_id=guest.id,
        check_in_date=check_in,
        check_out_date=check_out,
        num_guests=int(request.form.get('num_guests', 1)),
        special_requests=request.form.get('special_requests', '').strip(),
        total_amount=nights * room.price_per_night,
        status=booking_status,
        id_card_filename=id_card_filename,
        id_card_drive_id=id_card_drive_id,
        payment_slip_filename=payment_slip_filename,
        payment_slip_drive_id=payment_slip_drive_id,
    )
    db.session.add(booking)
    db.session.flush()

    generate_invoice(
        booking,
        invoice_to=request.form.get('invoice_to', '').strip() or None,
        company_name=request.form.get('company_name', '').strip() or None,
        billing_address=request.form.get('billing_address', '').strip() or None,
    )
    # Override invoice's default 'unpaid' to the appropriate new-vocab value
    if booking.invoice is not None:
        booking.invoice.payment_status = invoice_payment_status
    db.session.commit()

    # WhatsApp: acknowledge to guest + notify staff (non-blocking)
    try:
        from ..services.whatsapp import send_booking_acknowledgment, send_staff_new_booking_notification
        send_booking_acknowledgment(booking)
        send_staff_new_booking_notification(booking)
    except Exception:
        pass

    return redirect(url_for('public.confirmation', booking_ref=booking.booking_ref))


@public_bp.route('/confirmation/<booking_ref>')
def confirmation(booking_ref):
    booking = Booking.query.filter_by(booking_ref=booking_ref).first_or_404()
    return render_template('public/confirmation.html', booking=booking)
