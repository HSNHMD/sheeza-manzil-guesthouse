"""Cashiering V1 routes — admin-only payment posting + void.

Two POST endpoints:

    POST /bookings/<int:booking_id>/cashier/post-payment
        — Record a payment received from the guest.

    POST /bookings/<int:booking_id>/cashier/void-transaction/<int:txn_id>
        — Soft-void a posted transaction (requires reason).

Hard rules enforced here + by the test suite:
  - Both routes require @login_required + @admin_required.
  - Posting a payment writes BOTH a CashierTransaction row AND a
    linked FolioItem (item_type='payment', signed-negative total).
    Folio balance math is unchanged.
  - Booking.status / Invoice.payment_status / Room.status are NEVER
    mutated by these endpoints.
  - Voiding a transaction soft-removes the txn AND its linked
    folio_item. Both rows preserved for audit.
  - Audit metadata is a strict whitelist (see _audit_meta below).
  - Reference numbers / notes / cashier identity are stored on the
    transaction; reference number is also passed to the linked folio
    item's metadata_json for trace-back.
  - No WhatsApp / email / Gemini side-effects.
"""

from __future__ import annotations

import json as _json
from datetime import datetime

from flask import (
    Blueprint, request, redirect, url_for, flash, abort,
)
from flask_login import login_required, current_user

from ..models import db, Booking, FolioItem, CashierTransaction
from ..decorators import admin_required
from ..services.audit import log_activity
from ..services.cashiering import (
    validate_payment_input,
    PAYMENT_METHODS,
    PAYMENT_METHOD_LABELS,
    transaction_label,
)
from ..services.folio import signed_total


cashiering_bp = Blueprint('cashiering', __name__)


# ── Helpers ─────────────────────────────────────────────────────────

def _audit_meta(txn: CashierTransaction, *, voided: bool = False) -> dict:
    """Return strict-whitelist metadata for ActivityLog.

    Allowed keys per spec: booking_id, booking_ref, cashier_transaction_id,
    payment_method, amount, status. Plus folio_item_id for cross-reference,
    transaction_type for clarity, reference_number_present (bool, never
    the value itself in case of card last-4 etc.), voided flag.
    """
    booking = txn.booking if txn.booking_id else None
    return {
        'booking_id':              txn.booking_id,
        'booking_ref':             getattr(booking, 'booking_ref', None) if booking else None,
        'cashier_transaction_id':  txn.id,
        'folio_item_id':           txn.folio_item_id,
        'payment_method':          txn.payment_method,
        'amount':                  txn.amount,
        'status':                  txn.status,
        'transaction_type':        txn.transaction_type,
        'reference_number_present': bool(txn.reference_number),
        'voided':                  bool(voided),
    }


def _booking_redirect(booking_id):
    return redirect(url_for('bookings.detail', booking_id=booking_id))


# ── POST /bookings/<id>/cashier/post-payment ─────────────────────────

@cashiering_bp.route(
    '/bookings/<int:booking_id>/cashier/post-payment',
    methods=['POST'],
)
@login_required
@admin_required
def post_payment(booking_id):
    """Record a payment received from the guest.

    Form inputs:
        amount             — required, > 0
        payment_method     — one of PAYMENT_METHODS
        reference_number   — optional, ≤80 chars
        transaction_type   — payment (default) | refund
        notes              — optional, ≤500 chars; rejects "[admin: ...]"

    Side effects (atomic — both happen or neither):
      1. Create CashierTransaction row (status='posted', cashier=current_user).
      2. Create linked FolioItem (item_type='payment', signed-negative
         total = -amount; refund creates a positive folio_item with
         item_type='payment' since refund INCREASES guest balance).
      3. Stamp CashierTransaction.folio_item_id with the new folio row.
      4. Two audit rows: cashier.payment_posted (or cashier.refund_posted)
         + folio.item.created.

    Booking / invoice / room status are NEVER touched.
    """
    booking = Booking.query.get_or_404(booking_id)

    form = request.form
    transaction_type = (form.get('transaction_type') or 'payment').strip().lower()
    validation = validate_payment_input(
        amount=form.get('amount'),
        payment_method=form.get('payment_method'),
        reference_number=form.get('reference_number'),
        notes=form.get('notes'),
        transaction_type=transaction_type,
    )

    if validation['errors']:
        for err in validation['errors']:
            flash(f'Cashier: {err}', 'error')
        return _booking_redirect(booking_id)

    cleaned = validation['cleaned']
    amount = cleaned['amount']
    method = cleaned['payment_method']
    txn_type = cleaned['transaction_type']
    reference = cleaned['reference_number']
    notes = cleaned['notes']

    # ── Build the linked FolioItem first (we need its id later) ──
    # For a 'payment' txn: folio_item is signed-negative (reduces balance).
    # For a 'refund' txn: folio_item is signed-positive (increases balance).
    # signed_total('payment', amount, 0, 0) → -amount (auto-negate).
    folio_total = signed_total('payment', amount, 0, 0)
    if txn_type == 'refund':
        folio_total = -folio_total  # flip back to positive

    folio_item = FolioItem(
        booking_id=booking.id,
        guest_id=booking.guest_id,
        item_type='payment',
        description=(
            f'{transaction_label(method)} '
            f'{"refund" if txn_type == "refund" else "payment"}'
            f'{" · " + reference if reference else ""}'
        )[:255],
        quantity=1.0,
        unit_price=amount,
        amount=amount,
        tax_amount=0.0,
        service_charge_amount=0.0,
        total_amount=round(folio_total, 2),
        status='open',
        source_module='manual',  # cashiering V1 is operator-driven
        posted_by_user_id=getattr(current_user, 'id', None),
    )
    db.session.add(folio_item)
    db.session.flush()  # populate folio_item.id

    # ── Build the CashierTransaction ──
    txn = CashierTransaction(
        booking_id=booking.id,
        guest_id=booking.guest_id,
        folio_item_id=folio_item.id,
        invoice_id=getattr(getattr(booking, 'invoice', None), 'id', None),
        amount=amount,
        currency='MVR',
        payment_method=method,
        reference_number=reference,
        received_by_user_id=getattr(current_user, 'id', None),
        transaction_type=txn_type,
        status='posted',
        notes=notes,
    )
    db.session.add(txn)
    db.session.flush()

    # Cross-reference back to the txn from folio_item via metadata_json
    folio_item.metadata_json = _json.dumps({
        'cashier_transaction_id': txn.id,
        'payment_method':         method,
    })

    # ── Audit (two rows: cashier event + folio creation) ──
    log_activity(
        'cashier.refund_posted' if txn_type == 'refund' else 'cashier.payment_posted',
        booking=booking, invoice=getattr(booking, 'invoice', None),
        description=(
            f'{transaction_label(method)} {txn_type} of '
            f'{amount:.2f} {txn.currency} '
            f'received by {getattr(current_user, "username", "system")}.'
        ),
        metadata=_audit_meta(txn),
    )
    log_activity(
        'folio.item.created',
        booking=booking, invoice=getattr(booking, 'invoice', None),
        description=(
            f'Folio item posted via cashiering: payment '
            f'({method}) {folio_total:+.2f}.'
        ),
        metadata={
            'booking_id':     booking.id,
            'booking_ref':    booking.booking_ref,
            'folio_item_id':  folio_item.id,
            'item_type':      'payment',
            'source_module':  'manual',
            'amount':         folio_item.total_amount,
            'status':         'open',
            'voided':         False,
        },
    )
    db.session.commit()

    if txn_type == 'refund':
        flash(
            f'Refund of {amount:.2f} MVR posted via {transaction_label(method)}. '
            f'Folio balance increased.',
            'success',
        )
    else:
        flash(
            f'Payment of {amount:.2f} MVR posted via {transaction_label(method)}. '
            f'Folio balance decreased.',
            'success',
        )
    return _booking_redirect(booking_id)


# ── POST /bookings/<id>/cashier/void-transaction/<id> ────────────────

@cashiering_bp.route(
    '/bookings/<int:booking_id>/cashier/void-transaction/<int:txn_id>',
    methods=['POST'],
)
@login_required
@admin_required
def void_transaction(booking_id, txn_id):
    """Soft-void a posted transaction.

    Sets status='voided' on the CashierTransaction AND the linked
    FolioItem (so the folio balance excludes it). Both rows are
    preserved for audit.

    Form input:
        void_reason — optional but encouraged
    """
    booking = Booking.query.get_or_404(booking_id)
    txn = CashierTransaction.query.get_or_404(txn_id)

    if txn.booking_id != booking.id:
        abort(404)
    if txn.status == 'voided':
        flash('That transaction is already voided.', 'info')
        return _booking_redirect(booking_id)

    reason = (request.form.get('void_reason') or '').strip()
    if len(reason) > 255:
        reason = reason[:255]

    txn.status = 'voided'
    txn.voided_at = datetime.utcnow()
    txn.voided_by_user_id = getattr(current_user, 'id', None)
    txn.void_reason = reason or None

    # Also void the linked folio_item so the balance excludes it.
    folio_item = FolioItem.query.get(txn.folio_item_id) if txn.folio_item_id else None
    if folio_item is not None and folio_item.status != 'voided':
        folio_item.status = 'voided'
        folio_item.voided_at = datetime.utcnow()
        folio_item.voided_by_user_id = getattr(current_user, 'id', None)
        folio_item.void_reason = reason or 'cashier transaction voided'

    log_activity(
        'cashier.payment_voided',
        booking=booking, invoice=getattr(booking, 'invoice', None),
        description=(
            f'Cashier transaction voided '
            f'({txn.payment_method}, was {txn.amount:.2f} {txn.currency}).'
        ),
        metadata=_audit_meta(txn, voided=True),
    )
    if folio_item is not None:
        log_activity(
            'folio.item.voided',
            booking=booking, invoice=getattr(booking, 'invoice', None),
            description=(
                f'Folio item voided via cashier transaction void '
                f'(folio_item_id={folio_item.id}).'
            ),
            metadata={
                'booking_id':     booking.id,
                'booking_ref':    booking.booking_ref,
                'folio_item_id':  folio_item.id,
                'item_type':      'payment',
                'source_module':  'manual',
                'amount':         folio_item.total_amount,
                'status':         'voided',
                'voided':         True,
            },
        )
    db.session.commit()

    flash('Transaction voided. Folio balance updated.', 'success')
    return _booking_redirect(booking_id)
