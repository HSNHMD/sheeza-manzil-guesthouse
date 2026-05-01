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


def reconciliation_summary(*, lookback_days: int = 30,
                           posted_limit: int = 200,
                           voided_limit: int = 50) -> dict:
    """Portfolio-level cashiering reconciliation snapshot.

    Returns a dict the /accounting/reconciliation/payments view
    renders directly. Built to answer four operator questions:

      1. What payments were posted recently? Are they linked to the
         right folio?
      2. Which folios still have a balance after their payments?
      3. Which payments lack a reference number we can chase to a
         bank statement?
      4. Which payments were voided (audit trail)?

    NEVER counts payments as revenue — the totals are sums of MONEY
    RECEIVED, not earned. Revenue belongs in services/reports.py.

    Pure function with respect to Flask context — needs db.session
    but not request/url_for.
    """
    from datetime import datetime, timedelta
    from sqlalchemy import or_
    from ..models import CashierTransaction, Invoice, BankTransaction

    cutoff = datetime.utcnow() - timedelta(days=lookback_days)

    # ── Posted payments (lookback window) ───────────────────────
    posted = (
        CashierTransaction.query
        .filter(CashierTransaction.status == 'posted',
                CashierTransaction.created_at >= cutoff)
        .order_by(CashierTransaction.created_at.desc())
        .limit(posted_limit)
        .all()
    )

    # ── Voided payments (audit trail) ───────────────────────────
    voided = (
        CashierTransaction.query
        .filter(CashierTransaction.status == 'voided')
        .order_by(CashierTransaction.created_at.desc())
        .limit(voided_limit)
        .all()
    )

    # ── Missing reference (bank-transfer / online with no ref) ──
    # These are the high-risk rows: a card terminal slip number or a
    # bank-transfer ref is the only way to chase the money to a real
    # statement line. Cash payments don't need refs.
    missing_ref = (
        CashierTransaction.query
        .filter(CashierTransaction.status == 'posted',
                CashierTransaction.payment_method.in_(
                    ('bank_transfer', 'online', 'card')
                ),
                or_(CashierTransaction.reference_number.is_(None),
                    CashierTransaction.reference_number == ''))
        .order_by(CashierTransaction.created_at.desc())
        .limit(posted_limit)
        .all()
    )

    # ── Outstanding invoices (balance still due) ────────────────
    open_invoices = []
    for inv in (Invoice.query
                .filter(Invoice.payment_status != 'paid')
                .order_by(Invoice.issue_date.desc())
                .limit(posted_limit)
                .all()):
        balance = round(float(inv.total_amount or 0)
                        - float(inv.amount_paid or 0), 2)
        if balance <= 0.005:
            continue  # rounding noise — don't surface
        open_invoices.append({
            'invoice':  inv,
            'balance':  balance,
            'booking':  inv.booking if hasattr(inv, 'booking') else None,
        })

    # ── Bank-statement unmatched rows (cross-link to existing page) ─
    bank_unmatched_count = (
        BankTransaction.query
        .filter(BankTransaction.match_type == 'unmatched')
        .count()
    )

    # ── Totals (money received, NOT revenue) ────────────────────
    posted_total = sum(t.amount for t in posted
                       if t.transaction_type == 'payment')
    refunded_total = sum(t.amount for t in posted
                         if t.transaction_type == 'refund')

    return {
        'lookback_days':         lookback_days,
        'cutoff':                cutoff,
        # Lists for table rendering
        'posted_payments':       posted,
        'voided_payments':       voided,
        'missing_reference':     missing_ref,
        'open_invoices':         open_invoices,
        # Totals — money RECEIVED not revenue
        'posted_count':          len(posted),
        'posted_total_received': round(posted_total, 2),
        'posted_total_refunded': round(refunded_total, 2),
        'posted_net_received':   round(posted_total - refunded_total, 2),
        'voided_count':          len(voided),
        'missing_ref_count':     len(missing_ref),
        'open_invoice_count':    len(open_invoices),
        'open_invoice_balance':  round(
            sum(r['balance'] for r in open_invoices), 2
        ),
        # Cross-link badge to the bank-CSV page
        'bank_unmatched_count':  bank_unmatched_count,
    }


def transaction_label(method: str) -> str:
    return PAYMENT_METHOD_LABELS.get(method, method or 'Other')
