"""Cashiering V1 — payment posting + void helpers.

Pure-function helpers that the route handlers use to validate input
and perform the dual write (CashierTransaction + linked FolioItem).

Design contract (binding, see docs/accounts_business_date_night_audit_plan.md §2):

  - Posting a payment writes BOTH a CashierTransaction row AND a
    FolioItem row with item_type='payment'. They are linked via
    CashierTransaction.folio_item_id. Folio balance math is
    unchanged — it still reads folio_items only.
  - Voiding a transaction soft-removes BOTH rows. The
    CashierTransaction row keeps full audit trail (voided_at +
    voided_by_user_id + void_reason). The linked FolioItem also
    transitions to status='voided' so the folio_balance excludes it.
  - V1 does NOT modify Invoice.amount_paid or Invoice.payment_status.
    The legacy invoice payment flow stays untouched. This is the
    operational/staging-only payment path.
  - amount stored positive; direction encoded in transaction_type
    ('payment' / 'refund' / 'adjustment').
  - This module never sends WhatsApp / email / Gemini calls.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional


PAYMENT_METHODS = ('cash', 'bank_transfer', 'card', 'wallet', 'other')

PAYMENT_METHOD_LABELS = {
    'cash':          'Cash',
    'bank_transfer': 'Bank transfer',
    'card':          'Card',
    'wallet':        'Wallet / online',
    'other':         'Other',
}

TRANSACTION_TYPES = ('payment', 'refund', 'adjustment')

TRANSACTION_STATUSES = ('posted', 'voided', 'refunded')

# Active = "counts toward cash flow". Voided does not.
ACTIVE_STATUSES = frozenset(('posted', 'refunded'))


# ── Validation ──────────────────────────────────────────────────────

def _safe_amount(value, default=None) -> Optional[float]:
    """Parse to a positive float. Returns None on garbage; default on
    blank input (so callers can distinguish missing from invalid)."""
    if value is None or value == '':
        return default
    try:
        n = float(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return n


def normalize_payment_method(value) -> Optional[str]:
    """Lowercase + whitelist check. Returns None for unknown methods."""
    if not value:
        return None
    norm = str(value).strip().lower()
    return norm if norm in PAYMENT_METHODS else None


def validate_payment_input(*,
                           amount,
                           payment_method,
                           reference_number=None,
                           notes=None,
                           transaction_type='payment') -> dict:
    """Validate a Post Payment form payload.

    Returns ``{'errors': [...], 'cleaned': {...}}``. Pure function,
    no DB access.
    """
    errors = []
    cleaned = {}

    # amount
    amt = _safe_amount(amount, default=None)
    if amt is None:
        errors.append('amount must be a number')
    elif amt <= 0:
        errors.append('amount must be greater than zero')
    cleaned['amount'] = amt

    # payment_method
    pm = normalize_payment_method(payment_method)
    if pm is None:
        errors.append(f'payment_method must be one of: '
                      f'{", ".join(PAYMENT_METHODS)}')
    cleaned['payment_method'] = pm

    # transaction_type
    tt = (transaction_type or '').strip().lower()
    if tt not in TRANSACTION_TYPES:
        errors.append(f'transaction_type must be one of: '
                      f'{", ".join(TRANSACTION_TYPES)}')
    cleaned['transaction_type'] = tt

    # reference_number — optional, max 80
    ref = (reference_number or '').strip() or None
    if ref and len(ref) > 80:
        ref = ref[:80]
    cleaned['reference_number'] = ref

    # notes — optional, max 500
    n = (notes or '').strip() or None
    if n and len(n) > 500:
        n = n[:500]
    # Reject placeholder text leaking from AI drafts (same guard as
    # ai_draft_send_whatsapp uses).
    if n and '[admin:' in n.lower():
        errors.append('notes contain "[admin: ...]" placeholder; '
                      'replace with real values before posting')
    cleaned['notes'] = n

    return {'errors': errors, 'cleaned': cleaned}


# ── Aggregates / display helpers ─────────────────────────────────────

def cashier_summary_for(booking) -> dict:
    """Per-booking cash-flow summary. Reads CashierTransaction rows
    only (folio balance math stays in app/services/folio.py).

    Returns ``{by_method: {...}, total_received, total_refunded,
    txn_count_active, txn_count_voided}``.
    """
    from ..models import CashierTransaction

    txns = (
        CashierTransaction.query
        .filter(CashierTransaction.booking_id == booking.id)
        .all()
    )
    by_method = {}
    total_received = 0.0
    total_refunded = 0.0
    active = 0
    voided = 0
    for t in txns:
        if t.status == 'voided':
            voided += 1
            continue
        active += 1
        if t.transaction_type == 'refund':
            total_refunded += t.amount
        else:
            total_received += t.amount
        by_method[t.payment_method] = (
            by_method.get(t.payment_method, 0.0) + t.amount
            if t.transaction_type == 'payment' else
            by_method.get(t.payment_method, 0.0) - t.amount
        )

    return {
        'by_method':         {k: round(v, 2) for k, v in by_method.items()},
        'total_received':    round(total_received, 2),
        'total_refunded':    round(total_refunded, 2),
        'net_received':      round(total_received - total_refunded, 2),
        'txn_count_active':  active,
        'txn_count_voided':  voided,
    }


def transaction_label(method: str) -> str:
    return PAYMENT_METHOD_LABELS.get(method, method or 'Other')
