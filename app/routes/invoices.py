import random
import string
from datetime import date
from flask import Blueprint, render_template, redirect, url_for, flash, request, make_response
from flask_login import login_required, current_user
from ..models import db, Invoice, Booking
from ..services.audit import log_activity
from ..utils import hotel_date
from ..booking_lifecycle import OUTSTANDING_PAYMENT_STATUSES

invoices_bp = Blueprint('invoices', __name__, url_prefix='/invoices')

TAX_RATE = 0.0  # No tax applied


def generate_invoice_number():
    while True:
        num = 'INV' + hotel_date().strftime('%Y%m') + ''.join(random.choices(string.digits, k=4))
        if not Invoice.query.filter_by(invoice_number=num).first():
            return num


def generate_invoice(booking, invoice_to=None, company_name=None, billing_address=None):
    """Create invoice for a booking. Safe to call multiple times — returns existing invoice if present."""
    if booking.invoice:
        return booking.invoice

    subtotal = booking.total_amount
    total = round(subtotal, 2)

    invoice = Invoice(
        invoice_number=generate_invoice_number(),
        booking_id=booking.id,
        issue_date=hotel_date(),
        subtotal=subtotal,
        tax_rate=0,
        tax_amount=0,
        total_amount=total,
        payment_status='unpaid',
        invoice_to=invoice_to or None,
        company_name=company_name or None,
        billing_address=billing_address or None,
    )
    db.session.add(invoice)
    db.session.flush()  # ensure invoice.id is available for the audit row
    log_activity(
        'invoice.created',
        booking=booking, invoice=invoice,
        new_value='unpaid',
        description=f'Invoice {invoice.invoice_number} generated for booking {booking.booking_ref}.',
        metadata={
            'booking_ref': booking.booking_ref,
            'invoice_number': invoice.invoice_number,
            'total_amount': total,
        },
    )
    return invoice


@invoices_bp.route('/')
@login_required
def index():
    status_filter = request.args.get('status', '')
    search = request.args.get('search', '').strip()

    query = Invoice.query.join(Booking)

    if status_filter:
        query = query.filter(Invoice.payment_status == status_filter)
    if search:
        query = query.filter(
            db.or_(
                Invoice.invoice_number.ilike(f'%{search}%'),
                Booking.booking_ref.ilike(f'%{search}%')
            )
        )

    invoices = query.order_by(Invoice.created_at.desc()).all()
    # Outstanding sum spans both legacy (unpaid/partial) and new vocab
    # (not_received/pending_review) so admins see all owed money — see
    # app.booking_lifecycle.OUTSTANDING_PAYMENT_STATUSES for the canonical list.
    total_outstanding = sum(i.balance_due for i in Invoice.query.filter(
        Invoice.payment_status.in_(OUTSTANDING_PAYMENT_STATUSES)).all())

    return render_template('invoices/index.html', invoices=invoices,
                           status_filter=status_filter, search=search,
                           total_outstanding=total_outstanding)


@invoices_bp.route('/<int:invoice_id>')
@login_required
def detail(invoice_id):
    from ..models import ActivityLog
    invoice = Invoice.query.get_or_404(invoice_id)
    activity_entries = (
        ActivityLog.query
        .filter(db.or_(
            ActivityLog.invoice_id == invoice.id,
            ActivityLog.booking_id == invoice.booking_id,
        ))
        .order_by(ActivityLog.created_at.desc())
        .limit(50)
        .all()
    )
    return render_template('invoices/detail.html',
                           invoice=invoice,
                           activity_entries=activity_entries)


@invoices_bp.route('/<int:invoice_id>/pdf')
@login_required
def download_pdf(invoice_id):
    from ..services.pdf import generate_invoice_pdf
    invoice = Invoice.query.get_or_404(invoice_id)
    buf = generate_invoice_pdf(invoice)
    response = make_response(buf.read())
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = (
        f'attachment; filename="{invoice.invoice_number}.pdf"'
    )
    return response


@invoices_bp.route('/<int:invoice_id>/edit', methods=['GET', 'POST'])
@login_required
def edit(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)

    if request.method == 'POST':
        invoice.invoice_to      = request.form.get('invoice_to', '').strip() or None
        invoice.company_name    = request.form.get('company_name', '').strip() or None
        invoice.billing_address = request.form.get('billing_address', '').strip() or None
        invoice.notes           = request.form.get('notes', '').strip() or None
        issue_date_str = request.form.get('issue_date', '').strip()
        if issue_date_str:
            invoice.issue_date = date.fromisoformat(issue_date_str)
        db.session.commit()
        flash(f'Invoice {invoice.invoice_number} updated.', 'success')
        return redirect(url_for('invoices.detail', invoice_id=invoice_id))

    return render_template('invoices/edit.html', invoice=invoice)


@invoices_bp.route('/<int:invoice_id>/payment', methods=['POST'])
@login_required
def record_payment(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    amount = float(request.form.get('amount', 0))
    method = request.form.get('payment_method', 'cash')

    prev_payment_status = invoice.payment_status
    invoice.amount_paid = min(invoice.amount_paid + amount, invoice.total_amount)
    invoice.payment_method = method

    if invoice.amount_paid >= invoice.total_amount:
        invoice.payment_status = 'paid'
    elif invoice.amount_paid > 0:
        invoice.payment_status = 'partial'

    log_activity(
        'invoice.payment_recorded',
        booking=invoice.booking, invoice=invoice,
        old_value=prev_payment_status, new_value=invoice.payment_status,
        description=f'Payment of MVR {amount:.0f} recorded on invoice {invoice.invoice_number}.',
        metadata={
            'invoice_number': invoice.invoice_number,
            'amount': amount,
            'method': method,
            'amount_paid_total': invoice.amount_paid,
        },
    )
    db.session.commit()
    flash(f'Payment of {amount:.2f} recorded. Status: {invoice.payment_status}.', 'success')
    return redirect(url_for('invoices.detail', invoice_id=invoice_id))
