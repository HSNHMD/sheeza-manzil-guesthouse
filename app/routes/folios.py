"""Guest Folio (V1) — admin-only routes.

Two POST endpoints:

    POST /bookings/<int:booking_id>/folio/items
        — Add a charge / discount / payment / adjustment to a booking.
          Validates input, applies sign convention, audits, redirects
          back to the booking detail page.

    POST /bookings/<int:booking_id>/folio/items/<int:item_id>/void
        — Void an open folio item. Items are NEVER hard-deleted; voiding
          marks status='voided' and stamps voided_at + voided_by_user_id.

Hard rules enforced here + by the test suite:
  - All routes require @login_required + @admin_required.
  - There is NO DELETE route. Hard-delete is forbidden.
  - Voiding an already-voided item is rejected (no-op + flash error).
  - Adding a folio item NEVER mutates booking.status, invoice.payment_status,
    invoice.amount_paid, or room.status.
  - Audit metadata is a strict whitelist (see _audit_meta below).
  - Body / passport / payment-slip data are NEVER written to audit.
  - No WhatsApp / email / Gemini side effects.
"""

from __future__ import annotations

from datetime import datetime

from flask import (
    Blueprint, request, redirect, url_for, flash, abort,
)
from flask_login import login_required, current_user

from ..models import db, Booking, FolioItem
from ..decorators import admin_required
from ..services.audit import log_activity
from ..services.folio import (
    ITEM_TYPES,
    NEGATIVE_ITEM_TYPES,
    SIGNED_ITEM_TYPES,
    SOURCE_MODULES,
    STATUSES,
    signed_total,
    validate_folio_item,
)


folios_bp = Blueprint('folios', __name__)


# ── Helpers ──────────────────────────────────────────────────────────

def _audit_meta(item: FolioItem, *, voided: bool = False) -> dict:
    """Return the strict-whitelist metadata dict for an audit row.

    Allowed keys per spec: booking_id, booking_ref, folio_item_id,
    item_type, source_module, amount, status, voided.
    """
    booking = item.booking if item.booking_id else None
    return {
        'booking_id':     item.booking_id,
        'booking_ref':    getattr(booking, 'booking_ref', None) if booking else None,
        'folio_item_id':  item.id,
        'item_type':      item.item_type,
        'source_module':  item.source_module,
        'amount':         item.total_amount,
        'status':         item.status,
        'voided':         bool(voided),
    }


# ── POST /bookings/<id>/folio/items — add a folio item ───────────────

@folios_bp.route('/bookings/<int:booking_id>/folio/items', methods=['POST'])
@login_required
@admin_required
def add_item(booking_id):
    """Post a new folio item to a booking.

    Form inputs:
        item_type             — one of ITEM_TYPES
        description           — required, ≤ 255 chars
        quantity              — required, > 0
        unit_price            — required, ≥ 0 for non-signed types
        tax_amount            — optional, ≥ 0
        service_charge_amount — optional, ≥ 0

    The handler computes amount = quantity * unit_price (snapshotted),
    then total_amount = signed_total(item_type, amount, tax, sc) so the
    DB stores a SIGNED total — payments and discounts are negative.
    """
    booking = Booking.query.get_or_404(booking_id)

    form = request.form
    validation = validate_folio_item(
        item_type=form.get('item_type'),
        description=form.get('description'),
        quantity=form.get('quantity'),
        unit_price=form.get('unit_price'),
        tax_amount=form.get('tax_amount', 0),
        service_charge_amount=form.get('service_charge_amount', 0),
    )

    if validation['errors']:
        for err in validation['errors']:
            flash(f'Folio: {err}', 'error')
        return redirect(url_for('bookings.detail', booking_id=booking_id))

    cleaned = validation['cleaned']
    item_type = cleaned['item_type']
    quantity   = cleaned['quantity']
    unit_price = cleaned['unit_price']
    tax_amount = cleaned['tax_amount']
    service_charge_amount = cleaned['service_charge_amount']

    # Snapshot amount and signed total at post time.
    amount = round(quantity * unit_price, 2)
    total_amount = round(
        signed_total(item_type, amount, tax_amount, service_charge_amount),
        2,
    )

    item = FolioItem(
        booking_id=booking.id,
        guest_id=booking.guest_id,
        item_type=item_type,
        description=cleaned['description'],
        quantity=quantity,
        unit_price=unit_price,
        amount=amount,
        tax_amount=tax_amount,
        service_charge_amount=service_charge_amount,
        total_amount=total_amount,
        status='open',
        source_module='manual',
        posted_by_user_id=getattr(current_user, 'id', None),
    )
    db.session.add(item)
    db.session.flush()  # populate item.id for audit metadata

    log_activity(
        'folio.item.created',
        booking=booking, invoice=getattr(booking, 'invoice', None),
        description=(
            f'Folio item posted: {item_type} ({item.description[:50]}) '
            f'total {total_amount:.2f}.'
        ),
        metadata=_audit_meta(item),
    )
    db.session.commit()

    flash(
        f'Folio item added ({item_type}) — total {total_amount:.2f} MVR.',
        'success',
    )
    return redirect(url_for('bookings.detail', booking_id=booking_id))


# ── POST /bookings/<id>/folio/items/<id>/void — void an item ─────────

@folios_bp.route(
    '/bookings/<int:booking_id>/folio/items/<int:item_id>/void',
    methods=['POST'],
)
@login_required
@admin_required
def void_item(booking_id, item_id):
    """Void an open folio item.

    Sets status='voided' and stamps voided_at, voided_by_user_id, and the
    optional ``void_reason`` form field. Refuses if the item is already
    voided. Returns 404 if the item does not belong to this booking
    (defense against URL-tampering).
    """
    booking = Booking.query.get_or_404(booking_id)
    item = FolioItem.query.get_or_404(item_id)
    if item.booking_id != booking.id:
        abort(404)

    if item.status == 'voided':
        flash('This folio item is already voided.', 'error')
        return redirect(url_for('bookings.detail', booking_id=booking_id))

    reason = (request.form.get('void_reason') or '').strip()
    if len(reason) > 255:
        reason = reason[:255]

    item.status = 'voided'
    item.voided_at = datetime.utcnow()
    item.voided_by_user_id = getattr(current_user, 'id', None)
    item.void_reason = reason or None

    log_activity(
        'folio.item.voided',
        booking=booking, invoice=getattr(booking, 'invoice', None),
        description=(
            f'Folio item voided: {item.item_type} '
            f'(was {item.total_amount:.2f}).'
        ),
        metadata=_audit_meta(item, voided=True),
    )
    db.session.commit()

    flash('Folio item voided.', 'success')
    return redirect(url_for('bookings.detail', booking_id=booking_id))
