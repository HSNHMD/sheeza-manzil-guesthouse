import random
import string
from datetime import date
from flask import Blueprint, render_template, redirect, url_for, flash, request, make_response
from flask_login import login_required, current_user
from ..models import db, Invoice, Booking

invoices_bp = Blueprint('invoices', __name__, url_prefix='/invoices')

TAX_RATE = 0.10  # 10% tax


def generate_invoice_number():
    while True:
        num = 'INV' + date.today().strftime('%Y%m') + ''.join(random.choices(string.digits, k=4))
        if not Invoice.query.filter_by(invoice_number=num).first():
            return num


def generate_invoice(booking):
    """Create invoice for a booking. Called on checkout."""
    if booking.invoice:
        return booking.invoice

    subtotal = booking.total_amount
    tax = round(subtotal * TAX_RATE, 2)
    total = round(subtotal + tax, 2)

    invoice = Invoice(
        invoice_number=generate_invoice_number(),
        booking_id=booking.id,
        issue_date=date.today(),
        subtotal=subtotal,
        tax_rate=TAX_RATE * 100,
        tax_amount=tax,
        total_amount=total,
        payment_status='unpaid'
    )
    db.session.add(invoice)
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
    total_outstanding = sum(i.balance_due for i in Invoice.query.filter(
        Invoice.payment_status.in_(['unpaid', 'partial'])).all())

    return render_template('invoices/index.html', invoices=invoices,
                           status_filter=status_filter, search=search,
                           total_outstanding=total_outstanding)


@invoices_bp.route('/<int:invoice_id>')
@login_required
def detail(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    return render_template('invoices/detail.html', invoice=invoice)


@invoices_bp.route('/<int:invoice_id>/payment', methods=['POST'])
@login_required
def record_payment(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    amount = float(request.form.get('amount', 0))
    method = request.form.get('payment_method', 'cash')

    invoice.amount_paid = min(invoice.amount_paid + amount, invoice.total_amount)
    invoice.payment_method = method

    if invoice.amount_paid >= invoice.total_amount:
        invoice.payment_status = 'paid'
    elif invoice.amount_paid > 0:
        invoice.payment_status = 'partial'

    db.session.commit()
    flash(f'Payment of {amount:.2f} recorded. Status: {invoice.payment_status}.', 'success')
    return redirect(url_for('invoices.detail', invoice_id=invoice_id))
