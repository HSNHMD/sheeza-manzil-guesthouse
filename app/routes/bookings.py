import random
import string
from datetime import datetime, date
from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app
from ..utils import hotel_date
from flask_login import login_required, current_user
from ..models import db, Booking, Room, Guest, Invoice
from ..decorators import admin_required
from ..services.audit import log_activity
from ..services.whatsapp import (
    send_booking_acknowledgment,
    send_booking_confirmation,
    send_staff_new_booking_notification,
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
        Booking.status.in_(['unconfirmed', 'pending_verification', 'confirmed', 'checked_in']),
        Booking.check_in_date < check_out,
        Booking.check_out_date > check_in
    )
    if exclude_booking_id:
        query = query.filter(Booking.id != exclude_booking_id)
    return query.first() is None


@bookings_bp.route('/')
@login_required
def index():
    # Filter params:
    #   ?status=...           legacy single-axis filter (kept for back-compat
    #                         with existing bookmarks; also accepts the magic
    #                         value 'unpaid' as before)
    #   ?booking_status=...   new explicit booking-status axis
    #   ?payment_status=...   new explicit payment-status axis (joins Invoice)
    status_filter = request.args.get('status', '')
    booking_status_filter = request.args.get('booking_status', '')
    payment_status_filter = request.args.get('payment_status', '')
    date_filter = request.args.get('date', '')
    search = request.args.get('search', '').strip()

    query = Booking.query.join(Guest).join(Room)

    if status_filter == 'unpaid':
        # Bookings with no invoice or invoice.payment_status indicating
        # money is still owed, active only. Includes legacy 'unpaid' AND
        # new-vocab 'not_received'/'pending_review' to catch new submissions.
        query = query.outerjoin(Invoice, Invoice.booking_id == Booking.id).filter(
            Booking.status.in_(['confirmed', 'checked_in']),
            db.or_(
                Invoice.id == None,
                Invoice.payment_status.in_(['unpaid', 'not_received', 'pending_review']),
            ),
        )
    elif status_filter:
        # Legacy single-status filter still works:
        query = query.filter(Booking.status == status_filter)

    if booking_status_filter:
        query = query.filter(Booking.status == booking_status_filter)

    if payment_status_filter:
        query = query.outerjoin(Invoice, Invoice.booking_id == Booking.id).filter(
            Invoice.payment_status == payment_status_filter
        )

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

    today = hotel_date()
    arrivals_today = Booking.query.filter_by(check_in_date=today, status='confirmed').count()
    departures_today = Booking.query.filter_by(check_out_date=today, status='checked_in').count()
    in_house = Booking.query.filter_by(status='checked_in').count()

    return render_template('bookings/index.html', bookings=bookings,
                           arrivals_today=arrivals_today,
                           departures_today=departures_today,
                           in_house=in_house,
                           status_filter=status_filter,
                           booking_status_filter=booking_status_filter,
                           payment_status_filter=payment_status_filter,
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
        db.session.flush()  # get booking.id before generating invoice
        from .invoices import generate_invoice
        invoice = generate_invoice(
            booking,
            invoice_to=request.form.get('invoice_to', '').strip() or None,
            company_name=request.form.get('company_name', '').strip() or None,
            billing_address=request.form.get('billing_address', '').strip() or None,
        )
        log_activity(
            'booking.created',
            booking=booking, invoice=invoice,
            new_value='confirmed',
            description=f'Admin booking {booking.booking_ref} created for room {room.number}.',
            metadata={
                'booking_ref': booking.booking_ref,
                'room_number': room.number,
                'nights': nights,
                'total_amount': total,
                'source': 'admin_form',
            },
        )
        db.session.commit()
        send_booking_confirmation(booking)
        flash(f'Booking {booking.booking_ref} created. Invoice generated.', 'success')
        return redirect(url_for('bookings.detail', booking_id=booking.id))

    return render_template('bookings/form.html', rooms=rooms, guests=guests, booking=None)


@bookings_bp.route('/<int:booking_id>')
@login_required
def detail(booking_id):
    from ..models import ActivityLog
    from ..services.ai_drafts import DRAFT_TYPES, DRAFT_LABELS, can_draft
    booking = Booking.query.get_or_404(booking_id)
    activity_entries = (
        ActivityLog.query
        .filter(ActivityLog.booking_id == booking.id)
        .order_by(ActivityLog.created_at.desc())
        .limit(50)
        .all()
    )
    return render_template('bookings/detail.html',
                           booking=booking,
                           activity_entries=activity_entries,
                           ai_draft=None,
                           ai_draft_types=DRAFT_TYPES,
                           ai_draft_labels=DRAFT_LABELS,
                           can_draft=can_draft)


@bookings_bp.route('/<int:booking_id>/ai-draft', methods=['POST'])
@login_required
@admin_required
def ai_draft(booking_id):
    """Admin-only AI draft generator.

    Renders the booking detail template directly (NOT redirect-after-POST)
    so the draft preview is preserved without round-tripping through a flash
    or query param. Refreshing the resulting page re-POSTs and re-generates
    — non-destructive (one extra Anthropic API call).

    Hard rules enforced here:
      • No booking/invoice/room mutation
      • No WhatsApp / email send (helper does not import _send / _send_template)
      • Draft body NEVER passed to log_activity — only metadata
      • Draft body NEVER persisted server-side — returned to admin only
    """
    from ..models import ActivityLog
    from ..services.ai_drafts import (
        DRAFT_TYPES, DRAFT_LABELS, can_draft, generate_draft,
        uses_payment_instructions,
    )
    booking = Booking.query.get_or_404(booking_id)
    draft_type = (request.form.get('draft_type') or '').strip()

    # Hard allow-list (defense-in-depth even though UI is gated).
    if draft_type not in DRAFT_TYPES:
        flash('Invalid draft type.', 'error')
        return redirect(url_for('bookings.detail', booking_id=booking_id))

    result = generate_draft(draft_type, booking)

    # Audit: log the EVENT and sanitized metadata, never the body or prompt.
    # NOTE: when the draft type embeds the official Sheeza Manzil bank
    # transfer block, we record only a `payment_instructions_used: True`
    # boolean — the block text itself is NEVER persisted to the audit log.
    if result.get('success'):
        log_activity(
            'ai.draft.created',
            booking=booking, invoice=booking.invoice,
            description=f'Admin generated AI draft (type: {draft_type}).',
            metadata={
                'draft_type':  draft_type,
                'booking_ref': booking.booking_ref,
                'provider':    result.get('provider'),
                'model':       result.get('model'),
                'length_chars': result.get('length_chars'),
                'payment_instructions_used': uses_payment_instructions(draft_type),
                'success':     True,
            },
        )
    else:
        log_activity(
            'ai.draft.failed',
            booking=booking, invoice=booking.invoice,
            description=f'AI draft generation failed (type: {draft_type}).',
            metadata={
                'draft_type':  draft_type,
                'booking_ref': booking.booking_ref,
                'provider':    result.get('provider'),
                'error':       result.get('error', 'unknown'),
                'success':     False,
            },
        )
    db.session.commit()

    activity_entries = (
        ActivityLog.query
        .filter(ActivityLog.booking_id == booking.id)
        .order_by(ActivityLog.created_at.desc())
        .limit(50)
        .all()
    )

    return render_template(
        'bookings/detail.html',
        booking=booking,
        activity_entries=activity_entries,
        ai_draft=result,
        ai_draft_selected=draft_type,
        ai_draft_types=DRAFT_TYPES,
        ai_draft_labels=DRAFT_LABELS,
        can_draft=can_draft,
    )


@bookings_bp.route('/<int:booking_id>/ai-draft/send-whatsapp', methods=['POST'])
@login_required
@admin_required
def ai_draft_send_whatsapp(booking_id):
    """Admin-approved send of an EDITED AI draft via WhatsApp.

    The admin saw the AI-generated draft on the booking detail page,
    edited it in a textarea, and clicked "Send via WhatsApp". This
    handler receives the EDITED `message_body` and dispatches it via
    the existing free-form WhatsApp transport (`send_text_message`).

    Hard rules enforced here:
      • No booking.status / invoice.payment_status / room.status mutation
      • No Gemini call (this handler does NOT import ai_drafts)
      • No email send (no SMTP integration exists in the codebase)
      • Audit metadata is a strict whitelist:
          booking_ref, draft_type, provider='whatsapp',
          recipient_phone_last4, message_length,
          whatsapp_message_id (success only),
          error_class (failure only)
        Body and full phone are NEVER logged.
      • Two audit rows per click: an `attempt` BEFORE the API call, and
        a `sent` or `failed` AFTER. This makes "did the send actually
        happen?" a trivial DB query.
      • WhatsApp's 24-hour free-form window: if Meta returns 131047 the
        wrapper maps it to error_class='meta_window_closed' and we show
        a friendly message suggesting the wa.me deeplink fallback. We
        never auto-substitute an approved template, because that would
        silently change the message content.
    """
    import re
    from ..services.whatsapp import send_text_message

    booking = Booking.query.get_or_404(booking_id)
    message_body = (request.form.get('message_body') or '').strip()
    draft_type = (request.form.get('draft_type') or '').strip() or None

    phone = (booking.guest.phone or '').strip()
    phone_last4 = re.sub(r'\D', '', phone)[-4:] if phone else None

    # ── Validation: fail fast WITHOUT auditing or calling the API ──
    if not phone:
        flash('Cannot send: guest has no phone number on file.', 'error')
        return redirect(url_for('bookings.detail', booking_id=booking_id))

    if not message_body:
        flash('Cannot send: message is empty.', 'error')
        return redirect(url_for('bookings.detail', booking_id=booking_id))

    # Reject drafts that still contain AI placeholder markers like
    # "[admin: please paste current bank details]" — these should be
    # filled in by the admin BEFORE sending.
    if '[admin:' in message_body.lower():
        flash(
            'Cannot send: draft still contains "[admin: …]" placeholder text. '
            'Please replace the placeholders with real values before sending.',
            'error',
        )
        return redirect(url_for('bookings.detail', booking_id=booking_id))

    if len(message_body) > 1500:
        flash(
            f'Cannot send: message is {len(message_body)} characters; '
            f'the limit is 1500. Trim the message and try again.',
            'error',
        )
        return redirect(url_for('bookings.detail', booking_id=booking_id))

    # ── Audit attempt BEFORE the API call ──
    log_activity(
        'ai.draft.whatsapp_send_attempt',
        booking=booking, invoice=booking.invoice,
        description=f'Admin attempting WhatsApp send (type: {draft_type or "—"}).',
        metadata={
            'booking_ref': booking.booking_ref,
            'draft_type': draft_type,
            'provider': 'whatsapp',
            'recipient_phone_last4': phone_last4,
            'message_length': len(message_body),
        },
    )
    db.session.commit()

    # ── The actual send (mocked in tests; never hits real Meta API) ──
    result = send_text_message(phone, message_body)

    # ── Audit outcome ──
    if result.get('success'):
        log_activity(
            'ai.draft.whatsapp_sent',
            booking=booking, invoice=booking.invoice,
            description=(
                f'WhatsApp message delivered to guest '
                f'(type: {draft_type or "—"}).'
            ),
            metadata={
                'booking_ref': booking.booking_ref,
                'draft_type': draft_type,
                'provider': 'whatsapp',
                'recipient_phone_last4': phone_last4,
                'message_length': len(message_body),
                'whatsapp_message_id': result.get('message_id'),
                'success': True,
            },
        )
        db.session.commit()
        flash(
            f'Message sent to guest WhatsApp ending in {phone_last4}.',
            'success',
        )
        return redirect(url_for('bookings.detail', booking_id=booking_id))

    # Failure path — map error_class to friendly UX message.
    error_class = result.get('error_class') or 'unknown'
    log_activity(
        'ai.draft.whatsapp_failed',
        booking=booking, invoice=booking.invoice,
        description=(
            f'WhatsApp send failed '
            f'(type: {draft_type or "—"}; error_class: {error_class}).'
        ),
        metadata={
            'booking_ref': booking.booking_ref,
            'draft_type': draft_type,
            'provider': 'whatsapp',
            'recipient_phone_last4': phone_last4,
            'message_length': len(message_body),
            'error_class': error_class,
            'success': False,
        },
    )
    db.session.commit()

    msg_by_class = {
        'config_disabled': (
            'WhatsApp sending is not enabled in server config '
            '(WHATSAPP_ENABLED=true required).'
        ),
        'config_invalid': (
            'WhatsApp config is incomplete on the server '
            '(missing token or phone number ID).'
        ),
        'meta_token_invalid': (
            'WhatsApp authentication failed — the access token may have '
            'expired or been revoked. Ask the operator to rotate WHATSAPP_TOKEN.'
        ),
        'meta_window_closed': (
            "WhatsApp would not deliver: the guest hasn't messaged us in "
            "the last 24 hours, so Meta blocks free-form replies. Use the "
            '"Open in WhatsApp" deeplink below to send manually, or wait '
            'for the guest to message first.'
        ),
        'meta_other': (
            'WhatsApp returned an error. Try again, or use the '
            '"Open in WhatsApp" deeplink as a fallback.'
        ),
        'network_error': (
            'Network error reaching WhatsApp. Try again in a moment.'
        ),
    }
    flash(
        msg_by_class.get(
            error_class,
            'WhatsApp send failed. Try again, or use the "Open in WhatsApp" '
            'deeplink as a fallback.',
        ),
        'error',
    )
    return redirect(url_for('bookings.detail', booking_id=booking_id))


@bookings_bp.route('/<int:booking_id>/checkin', methods=['POST'])
@login_required
@admin_required
def checkin(booking_id):
    from ..booking_lifecycle import can_check_in
    booking = Booking.query.get_or_404(booking_id)
    if not can_check_in(booking):
        # Provide a precise reason in the flash. Two distinct failure modes:
        if booking.status != 'confirmed':
            flash(f'Only confirmed bookings can be checked in (status: {booking.status}).', 'error')
        else:
            flash('Payment required before check-in (must be verified or partially recorded). '
                  'Record a payment or run payment verification first.', 'error')
        return redirect(url_for('bookings.detail', booking_id=booking_id))

    prev_status = booking.status
    booking.status = 'checked_in'
    booking.actual_check_in = datetime.utcnow()
    booking.room.status = 'occupied'
    log_activity(
        'booking.checked_in',
        booking=booking, invoice=booking.invoice,
        old_value=prev_status, new_value='checked_in',
        description=f'Guest checked in to room {booking.room.number}.',
        metadata={'booking_ref': booking.booking_ref, 'room_number': booking.room.number},
    )
    db.session.commit()
    send_checkin_reminder(booking)
    flash(f'Guest {booking.guest.full_name} checked in to Room {booking.room.number}.', 'success')
    return redirect(url_for('bookings.detail', booking_id=booking_id))


@bookings_bp.route('/<int:booking_id>/checkout', methods=['POST'])
@login_required
@admin_required
def checkout(booking_id):
    from .invoices import generate_invoice
    from ..booking_lifecycle import can_check_out
    booking = Booking.query.get_or_404(booking_id)
    if not can_check_out(booking.status):
        flash(f'Only checked-in bookings can be checked out (status: {booking.status}).', 'error')
        return redirect(url_for('bookings.detail', booking_id=booking_id))

    prev_status = booking.status
    booking.status = 'checked_out'
    booking.actual_check_out = datetime.utcnow()
    booking.room.status = 'cleaning'
    booking.room.housekeeping_status = 'dirty'

    invoice = generate_invoice(booking)  # no-op if already exists from booking creation
    log_activity(
        'booking.checked_out',
        booking=booking, invoice=invoice,
        old_value=prev_status, new_value='checked_out',
        description=f'Guest checked out of room {booking.room.number}.',
        metadata={'booking_ref': booking.booking_ref, 'room_number': booking.room.number},
    )
    db.session.commit()
    send_checkout_invoice_summary(booking, invoice)
    flash(f'Guest {booking.guest.full_name} checked out. Invoice {invoice.invoice_number} updated.', 'success')
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
    prev_payment_status = inv.payment_status
    inv.amount_paid = min(inv.amount_paid + amount, inv.total_amount)
    inv.payment_method = method
    if inv.amount_paid >= inv.total_amount:
        inv.payment_status = 'paid'
    elif inv.amount_paid > 0:
        inv.payment_status = 'partial'

    log_activity(
        'invoice.payment_recorded',
        booking=booking, invoice=inv,
        old_value=prev_payment_status, new_value=inv.payment_status,
        description=f'Payment of MVR {amount:.0f} recorded via {method}.',
        metadata={
            'booking_ref': booking.booking_ref,
            'invoice_number': inv.invoice_number,
            'amount': amount,
            'method': method,
            'amount_paid_total': inv.amount_paid,
        },
    )
    db.session.commit()
    flash(f'Payment of MVR {amount:.0f} recorded ({method}). Status: {inv.payment_status}.', 'success')
    return redirect(url_for('bookings.detail', booking_id=booking_id))


@bookings_bp.route('/<int:booking_id>/edit', methods=['GET', 'POST'])
@login_required
def edit(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if booking.status != 'confirmed':
        flash('Only confirmed bookings can be edited.', 'error')
        return redirect(url_for('bookings.detail', booking_id=booking_id))

    rooms = Room.query.filter_by(is_active=True).order_by(Room.number).all()

    if request.method == 'POST':
        room_id = int(request.form.get('room_id'))
        check_in = date.fromisoformat(request.form.get('check_in_date'))
        check_out = date.fromisoformat(request.form.get('check_out_date'))

        if check_out <= check_in:
            flash('Check-out must be after check-in.', 'error')
            return render_template('bookings/edit.html', booking=booking, rooms=rooms)

        if not check_room_availability(room_id, check_in, check_out, exclude_booking_id=booking_id):
            flash('Room is not available for the selected dates.', 'error')
            return render_template('bookings/edit.html', booking=booking, rooms=rooms)

        room = Room.query.get_or_404(room_id)
        nights = (check_out - check_in).days
        new_total = nights * room.price_per_night

        booking.room_id = room_id
        booking.check_in_date = check_in
        booking.check_out_date = check_out
        booking.num_guests = int(request.form.get('num_guests', 1))
        booking.special_requests = request.form.get('special_requests', '').strip()
        booking.total_amount = new_total

        # Keep invoice in sync
        if booking.invoice:
            inv = booking.invoice
            inv.subtotal = new_total
            inv.total_amount = new_total
            # Clamp amount_paid if total dropped below it
            if inv.amount_paid > new_total:
                inv.amount_paid = new_total
            if inv.amount_paid >= inv.total_amount:
                inv.payment_status = 'paid'
            elif inv.amount_paid > 0:
                inv.payment_status = 'partial'
            else:
                inv.payment_status = 'unpaid'
            # Update invoice billing fields
            invoice_to = request.form.get('invoice_to', '').strip() or None
            inv.invoice_to = invoice_to
            inv.company_name = request.form.get('company_name', '').strip() or None
            inv.billing_address = request.form.get('billing_address', '').strip() or None

        log_activity(
            'booking.edited',
            booking=booking, invoice=booking.invoice,
            description=f'Admin edited booking {booking.booking_ref} — total now MVR {new_total:.0f}.',
            metadata={
                'booking_ref': booking.booking_ref,
                'room_number': room.number,
                'nights': nights,
                'new_total': new_total,
            },
        )
        db.session.commit()
        flash(f'Booking {booking.booking_ref} updated.', 'success')
        return redirect(url_for('bookings.detail', booking_id=booking_id))

    return render_template('bookings/edit.html', booking=booking, rooms=rooms)


@bookings_bp.route('/<int:booking_id>/confirm', methods=['POST'])
@login_required
@admin_required
def confirm(booking_id):
    """Admin transitions a booking into 'confirmed'.

    Uses the full confirmation business rule
    `app.booking_lifecycle.can_confirm_booking(booking)`, which combines:
      1. status must be a pre-confirmation state, AND
      2. invoice (if any) must not be in {'rejected', 'mismatch'}, AND
      3. payment evidence must exist (slip on file, payment_status indicating
         trust, or amount_paid > 0) — UNLESS booking is already at
         'payment_verified' (which is the "evidence reviewed" state).

    This prevents the previously-permitted unsafe transition
        pending_payment + not_received  →  confirmed
    where a booking could be confirmed with zero payment evidence.

    Post-confirmation and terminal states (confirmed, checked_in,
    checked_out, cancelled, rejected) are refused as before.

    TODO(future): a separate explicit admin-override route may be added
    later for manual-confirm of no-evidence bookings (e.g. corporate
    post-stay billing). DO NOT relax the rule here.
    """
    from .invoices import generate_invoice
    from ..booking_lifecycle import can_confirm, can_confirm_booking
    booking = Booking.query.get_or_404(booking_id)

    # Status-only sanity check first so the error message can be specific.
    if not can_confirm(booking.status):
        flash(
            f'Booking is in status "{booking.status}" — only pre-confirmation '
            f'states can be confirmed (new_request, pending_payment, '
            f'payment_uploaded, payment_verified, or legacy unconfirmed/pending_verification).',
            'error',
        )
        return redirect(url_for('bookings.detail', booking_id=booking_id))

    # Full business-rule check (status + payment evidence).
    if not can_confirm_booking(booking):
        flash(
            f'Cannot confirm booking {booking.booking_ref} — payment evidence is required '
            f'(payment slip on file, recorded payment, or verified status). '
            f'Ask the guest to upload a slip, record a cash/card payment first, or use '
            f'the dedicated payment-verification action.',
            'error',
        )
        return redirect(url_for('bookings.detail', booking_id=booking_id))
    prev_booking_status = booking.status
    prev_payment_status = booking.invoice.payment_status if booking.invoice else None
    booking.status = 'confirmed'

    # Auto-mark payment if guest uploaded a payment slip — legacy behavior preserved.
    # NOTE on payment_status='paid': this writes the LEGACY value rather than the
    # new vocabulary 'verified', because 7 accounting queries still filter on
    # `Invoice.payment_status.in_(['paid', 'partial'])` (see app/routes/accounting.py
    # and app/routes/invoices.py). Coordinated migration is Phase 2 of the dashboard
    # plan in docs/admin_dashboard_plan.md. Display layer normalizes 'paid' →
    # 'verified' transparently via app.booking_lifecycle.normalize_legacy_payment_status,
    # so badges + labels render correctly regardless.
    if booking.payment_slip_filename:
        if not booking.invoice:
            db.session.flush()
            generate_invoice(booking)
        inv = booking.invoice
        inv.amount_paid = inv.total_amount
        inv.payment_status = 'paid'
        inv.payment_method = 'bank_transfer'
        flash(f'Booking {booking.booking_ref} confirmed. Payment marked as received (bank transfer).', 'success')
    else:
        flash(f'Booking {booking.booking_ref} confirmed. Record payment separately.', 'success')

    log_activity(
        'booking.confirmed',
        booking=booking, invoice=booking.invoice,
        old_value=prev_booking_status, new_value='confirmed',
        description=f'Booking {booking.booking_ref} confirmed by admin.',
        metadata={
            'booking_ref': booking.booking_ref,
            'prev_payment_status': prev_payment_status,
            'auto_marked_paid': bool(booking.payment_slip_filename),
        },
    )
    db.session.commit()
    send_booking_confirmation(booking)
    return redirect(url_for('bookings.detail', booking_id=booking_id))


@bookings_bp.route('/uploads/<path:filename>')
@login_required
def download_upload(filename):
    from flask import send_from_directory, current_app
    from ..services.drive import view_url as drive_view_url
    import os

    # Prefer Drive redirect when a drive_id is stored for this filename.
    booking = Booking.query.filter(
        db.or_(
            Booking.id_card_filename == filename,
            Booking.payment_slip_filename == filename,
        )
    ).first()
    if booking:
        drive_id = (
            booking.id_card_drive_id
            if booking.id_card_filename == filename
            else booking.payment_slip_drive_id
        )
        if drive_id:
            return redirect(drive_view_url(drive_id))

    # Fall back to local file.
    upload_dir = os.path.join(current_app.root_path, 'uploads')
    full_path = os.path.join(upload_dir, filename)
    if not os.path.isfile(full_path):
        flash('File not found. It may have been lost after a server restart — ask the guest to re-upload.', 'error')
        return redirect(request.referrer or url_for('bookings.index'))
    return send_from_directory(upload_dir, filename)


@bookings_bp.route('/<int:booking_id>/delete', methods=['POST'])
@login_required
def delete(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if booking.status == 'checked_in':
        flash('Cannot delete a booking while the guest is checked in.', 'error')
        return redirect(url_for('bookings.detail', booking_id=booking_id))
    ref = booking.booking_ref
    if booking.invoice:
        db.session.delete(booking.invoice)
    db.session.delete(booking)
    db.session.commit()
    flash(f'Booking {ref} deleted.', 'success')
    return redirect(url_for('bookings.index'))


@bookings_bp.route('/<int:booking_id>/cancel', methods=['POST'])
@login_required
@admin_required
def cancel(booking_id):
    from ..booking_lifecycle import can_cancel
    booking = Booking.query.get_or_404(booking_id)
    if not can_cancel(booking.status):
        flash(f'Cannot cancel — booking is already in terminal state ({booking.status}).', 'error')
        return redirect(url_for('bookings.detail', booking_id=booking_id))

    prev_status = booking.status
    booking.status = 'cancelled'
    if booking.room.status == 'occupied':
        booking.room.status = 'available'
    log_activity(
        'booking.cancelled',
        booking=booking, invoice=booking.invoice,
        old_value=prev_status, new_value='cancelled',
        description=f'Booking {booking.booking_ref} cancelled.',
        metadata={'booking_ref': booking.booking_ref},
    )
    db.session.commit()
    flash(f'Booking {booking.booking_ref} cancelled.', 'success')
    return redirect(url_for('bookings.index'))


# ─────────────────────────────────────────────────────────────────────────
# Admin payment-lifecycle actions
# ─────────────────────────────────────────────────────────────────────────
# Each action gates on the lifecycle helper to keep state-machine knowledge
# in one place (app/booking_lifecycle.py). Routes are POST-only and
# admin-only. They write the new vocabulary into Invoice.payment_status
# (no auto-mark-paid back-compat shim — these are the new clean paths).
#
# TODO(audit): no booking-lifecycle audit log exists yet. For now we use
#   app.logger.info(...) + flash() messages. When a proper audit/activity
#   log table lands, replace these calls with append-only audit writes.

@bookings_bp.route('/<int:booking_id>/payment/pending-review', methods=['POST'])
@login_required
@admin_required
def payment_pending_review(booking_id):
    """Revert a previously-marked mismatch back to pending review.
    Used when the guest tops up the missing amount or admin re-examines."""
    from ..booking_lifecycle import can_mark_pending_review
    booking = Booking.query.get_or_404(booking_id)
    if not can_mark_pending_review(booking):
        flash('Cannot mark pending review — only previously-mismatched payments can be re-queued for review.', 'error')
        return redirect(url_for('bookings.detail', booking_id=booking_id))
    prev_payment_status = booking.invoice.payment_status
    booking.invoice.payment_status = 'pending_review'
    log_activity(
        'payment.pending_review',
        booking=booking, invoice=booking.invoice,
        old_value=prev_payment_status, new_value='pending_review',
        description=f'Payment for {booking.booking_ref} re-queued for review.',
        metadata={'booking_ref': booking.booking_ref,
                  'invoice_number': booking.invoice.invoice_number},
    )
    db.session.commit()
    current_app.logger.info(
        '[BookingLifecycle] payment_pending_review on booking_id=%s ref=%s by user_id=%s',
        booking.id, booking.booking_ref, getattr(current_user, 'id', None),
    )
    flash(f'Payment for {booking.booking_ref} marked as pending review.', 'success')
    return redirect(url_for('bookings.detail', booking_id=booking_id))


@bookings_bp.route('/<int:booking_id>/payment/verify', methods=['POST'])
@login_required
@admin_required
def payment_verify(booking_id):
    """Admin verifies the payment evidence. Transitions:
        booking.status        → 'payment_verified'
        invoice.payment_status → 'verified'
    """
    from ..booking_lifecycle import can_verify_payment
    booking = Booking.query.get_or_404(booking_id)
    if not can_verify_payment(booking):
        flash('Cannot verify payment — booking must be in payment_uploaded state with a slip on file '
              '(or legacy pending_verification + slip / amount_paid > 0).', 'error')
        return redirect(url_for('bookings.detail', booking_id=booking_id))
    prev_booking_status = booking.status
    prev_payment_status = booking.invoice.payment_status
    booking.status = 'payment_verified'
    booking.invoice.payment_status = 'verified'
    log_activity(
        'payment.verified',
        booking=booking, invoice=booking.invoice,
        old_value=prev_payment_status, new_value='verified',
        description=f'Payment evidence verified for {booking.booking_ref}.',
        metadata={
            'booking_ref': booking.booking_ref,
            'invoice_number': booking.invoice.invoice_number,
            'prev_booking_status': prev_booking_status,
        },
    )
    db.session.commit()
    current_app.logger.info(
        '[BookingLifecycle] payment_verified on booking_id=%s ref=%s by user_id=%s',
        booking.id, booking.booking_ref, getattr(current_user, 'id', None),
    )
    flash(f'Payment verified for {booking.booking_ref}. Booking is ready to confirm.', 'success')
    return redirect(url_for('bookings.detail', booking_id=booking_id))


@bookings_bp.route('/<int:booking_id>/payment/mismatch', methods=['POST'])
@login_required
@admin_required
def payment_mismatch(booking_id):
    """Admin flags the payment as amount-mismatched. Booking_status stays
    at 'payment_uploaded'; admin can later top-up-then-verify or reject."""
    from ..booking_lifecycle import can_mark_mismatch
    booking = Booking.query.get_or_404(booking_id)
    if not can_mark_mismatch(booking):
        flash('Cannot mark mismatch — booking must be at payment_uploaded with a slip pending review.', 'error')
        return redirect(url_for('bookings.detail', booking_id=booking_id))
    prev_payment_status = booking.invoice.payment_status
    booking.invoice.payment_status = 'mismatch'
    log_activity(
        'payment.mismatch',
        booking=booking, invoice=booking.invoice,
        old_value=prev_payment_status, new_value='mismatch',
        description=f'Payment for {booking.booking_ref} flagged as amount mismatch.',
        metadata={'booking_ref': booking.booking_ref,
                  'invoice_number': booking.invoice.invoice_number},
    )
    db.session.commit()
    current_app.logger.info(
        '[BookingLifecycle] payment_mismatch on booking_id=%s ref=%s by user_id=%s',
        booking.id, booking.booking_ref, getattr(current_user, 'id', None),
    )
    flash(f'Payment for {booking.booking_ref} marked as amount mismatch. Guest will need to clarify or top up.', 'warning')
    return redirect(url_for('bookings.detail', booking_id=booking_id))


@bookings_bp.route('/<int:booking_id>/payment/reject', methods=['POST'])
@login_required
@admin_required
def payment_reject(booking_id):
    """Admin rejects the payment. Transitions:
        booking.status        → 'rejected'
        invoice.payment_status → 'rejected'
    Also releases the room hold."""
    from ..booking_lifecycle import can_reject_payment
    booking = Booking.query.get_or_404(booking_id)
    if not can_reject_payment(booking):
        flash('Cannot reject payment — booking must be at payment_uploaded with a slip currently '
              'pending_review or mismatch.', 'error')
        return redirect(url_for('bookings.detail', booking_id=booking_id))
    prev_booking_status = booking.status
    prev_payment_status = booking.invoice.payment_status
    booking.status = 'rejected'
    booking.invoice.payment_status = 'rejected'
    if booking.room and booking.room.status == 'occupied':
        booking.room.status = 'available'
    log_activity(
        'payment.rejected',
        booking=booking, invoice=booking.invoice,
        old_value=prev_payment_status, new_value='rejected',
        description=f'Payment for {booking.booking_ref} rejected; booking marked rejected.',
        metadata={
            'booking_ref': booking.booking_ref,
            'invoice_number': booking.invoice.invoice_number,
            'prev_booking_status': prev_booking_status,
        },
    )
    db.session.commit()
    current_app.logger.info(
        '[BookingLifecycle] payment_rejected on booking_id=%s ref=%s by user_id=%s',
        booking.id, booking.booking_ref, getattr(current_user, 'id', None),
    )
    flash(f'Payment for {booking.booking_ref} rejected. Booking marked as rejected.', 'warning')
    return redirect(url_for('bookings.detail', booking_id=booking_id))
