"""Single source of truth for the booking + payment status lifecycle.

This module defines:
  • The canonical vocabularies (BOOKING_STATUSES, PAYMENT_STATUSES)
  • The set of valid (booking_status, payment_status) pairs
  • Helpers to validate, normalize legacy values, and derive display labels
    + badge CSS classes for templates

Key design rule: payment_status describes VERIFICATION TRUST only.
"Partial payment" is a DERIVED display state (balance_due > 0 combined with a
verified payment), never a stored payment_status value. This module never
writes 'partial' to the DB and never depends on a 'partial' input — it maps
legacy 'partial' rows to 'verified' for display purposes.

Public API (everything else is private):
  BOOKING_STATUSES                 — tuple of canonical booking statuses
  PAYMENT_STATUSES                 — tuple of canonical payment statuses
  is_valid_booking_status(s)       — bool
  is_valid_payment_status(s)       — bool
  is_valid_status_pair(b, p)       — bool
  normalize_legacy_payment_status(s)  → canonical or None on unknown
  get_status_label(b, p, invoice=None)        → human-readable label
  get_status_badge_class(b, p, invoice=None)  → tailwind CSS classes
"""

from __future__ import annotations

from typing import Optional


# ── Canonical vocabularies ──────────────────────────────────────────────────

BOOKING_STATUSES: tuple = (
    'new_request',
    'pending_payment',
    'payment_uploaded',
    'payment_verified',
    'confirmed',
    'checked_in',
    'checked_out',
    'cancelled',
    'rejected',
)

PAYMENT_STATUSES: tuple = (
    'not_received',
    'pending_review',
    'verified',
    'rejected',
    'mismatch',
)


# ── Legacy payment_status mapping ───────────────────────────────────────────
# Existing DB rows use the old vocabulary ('unpaid', 'partial', 'paid').
# These map to the new vocabulary as follows:
#   unpaid  → not_received   (no payment trust established)
#   partial → verified       (some payment WAS received & trusted; balance is
#                             a separate derived concern from balance_due > 0)
#   paid    → verified       (payment received & trusted; balance == 0)
_LEGACY_PAYMENT_MAP: dict = {
    'unpaid': 'not_received',
    'partial': 'verified',
    'paid': 'verified',
}

# Legacy booking statuses → canonical mapping (used by display helpers so old
# rows still render with the right label/badge until they're migrated):
_LEGACY_BOOKING_MAP: dict = {
    'unconfirmed': 'pending_payment',          # old: no slip, awaiting payment
    'pending_verification': 'payment_uploaded',  # old: slip uploaded, awaiting admin
}


# ── Valid (booking_status, payment_status) pairs ────────────────────────────
# Any pair NOT in this set is invalid and must not be written to the DB.
# Routes that set both fields should call is_valid_status_pair() first.
VALID_STATUS_PAIRS: frozenset = frozenset({
    # Pre-payment: guest hasn't paid
    ('new_request',      'not_received'),
    ('pending_payment',  'not_received'),

    # Slip uploaded — admin hasn't reviewed yet
    ('payment_uploaded', 'pending_review'),
    # Admin reviewed & flagged amount mismatch (guest can top up; reversible)
    ('payment_uploaded', 'mismatch'),

    # Admin verified payment, booking not yet finalized as 'confirmed'
    ('payment_verified', 'verified'),

    # Booking confirmed (active reservation, payment trusted)
    ('confirmed',  'verified'),

    # In-house states — payment must be verified
    ('checked_in',  'verified'),
    ('checked_out', 'verified'),

    # Cancelled — can come from any payment state (refund handled separately)
    ('cancelled',  'not_received'),
    ('cancelled',  'pending_review'),
    ('cancelled',  'verified'),
    ('cancelled',  'rejected'),
    ('cancelled',  'mismatch'),

    # Rejected — admin rejected the booking
    ('rejected',  'not_received'),    # rejected before any payment was made
    ('rejected',  'rejected'),        # rejected because payment was rejected
    ('rejected',  'mismatch'),        # rejected because amount mismatch never resolved
})


# ── Transition predecessor sets ─────────────────────────────────────────────
# Centralizes which booking statuses are valid SOURCE states for each admin
# transition. Routes and templates should call the helpers below rather than
# hardcoding the tuples — that way a future status addition only needs to
# update this module.

# Statuses from which admin "Confirm Booking" is allowed.
# Includes legacy values ('unconfirmed', 'pending_verification') so existing
# DB rows submitted before the new vocabulary still confirm correctly.
# Excludes 'confirmed', 'checked_in', 'checked_out', 'cancelled', 'rejected'
# — those are post-confirmation or terminal states.
CONFIRMABLE_FROM: tuple = (
    # Legacy values still present in production rows:
    'unconfirmed',
    'pending_verification',
    # New-vocabulary pre-confirmation states:
    'new_request',
    'pending_payment',
    'payment_uploaded',
    'payment_verified',
)


def can_confirm(booking_status: Optional[str]) -> bool:
    """True iff admin can confirm a booking from this status.

    Pre-confirmation states (new and legacy) are allowed.
    Post-confirmation and terminal states (confirmed, checked_in,
    checked_out, cancelled, rejected) are refused.
    Unknown / None statuses are refused (safe default).
    """
    return booking_status in CONFIRMABLE_FROM


# ── Validators ──────────────────────────────────────────────────────────────

def is_valid_booking_status(status: Optional[str]) -> bool:
    """True iff status is in the canonical BOOKING_STATUSES tuple."""
    return status in BOOKING_STATUSES


def is_valid_payment_status(status: Optional[str]) -> bool:
    """True iff status is in the canonical PAYMENT_STATUSES tuple."""
    return status in PAYMENT_STATUSES


def is_valid_status_pair(booking_status: Optional[str],
                         payment_status: Optional[str]) -> bool:
    """True iff (booking_status, payment_status) is a permitted combination.

    Used as a write-time guard in routes that set both fields.
    """
    return (booking_status, payment_status) in VALID_STATUS_PAIRS


# ── Legacy normalization ────────────────────────────────────────────────────

def normalize_legacy_payment_status(status: Optional[str]) -> Optional[str]:
    """Map an old payment_status value to the new vocabulary.

    Returns:
      - the same value if already canonical (in PAYMENT_STATUSES)
      - the mapped new value if it's a recognized legacy ('unpaid'/'partial'/'paid')
      - None for unknown values (caller should treat as warning, not crash)
      - None for None input
    """
    if status is None:
        return None
    if status in PAYMENT_STATUSES:
        return status
    return _LEGACY_PAYMENT_MAP.get(status)


def _normalize_legacy_booking_status(status: Optional[str]) -> Optional[str]:
    """Best-effort mapping of legacy booking statuses to new vocabulary."""
    if status is None:
        return None
    if status in BOOKING_STATUSES:
        return status
    return _LEGACY_BOOKING_MAP.get(status, status)  # unknown → return as-is


# ── Display helpers ─────────────────────────────────────────────────────────

def _has_balance_due(invoice) -> bool:
    """True if the invoice exists and balance_due > 0."""
    if invoice is None:
        return False
    try:
        return float(getattr(invoice, 'balance_due', 0) or 0) > 0
    except (TypeError, ValueError):
        return False


def get_status_label(booking_status: Optional[str],
                     payment_status: Optional[str],
                     invoice=None) -> str:
    """Return a human-readable label that fuses booking + payment + invoice
    state. Never raises on unknown inputs — falls back to a Title-Cased
    version of the booking_status string.

    Examples (from the design spec):
      ('new_request', 'not_received')                      → 'New Request'
      ('pending_payment', 'not_received')                  → 'Pending Payment'
      ('payment_uploaded', 'pending_review')               → 'Payment Uploaded / Needs Review'
      ('payment_uploaded', 'mismatch')                     → 'Payment Uploaded / Amount Mismatch'
      ('payment_verified', 'verified', balance_due > 0)    → 'Partial Payment Verified'
      ('payment_verified', 'verified', balance_due == 0)   → 'Payment Verified'
      ('confirmed', 'verified', balance_due > 0)           → 'Confirmed / Balance Due'
      ('confirmed', 'verified', balance_due == 0)          → 'Confirmed'
      ('checked_in', 'verified', balance_due > 0)          → 'Checked In / Balance Due'
      ('checked_in', 'verified')                           → 'Checked In'
      ('checked_out', 'verified', balance_due > 0)         → 'Checked Out / Balance Due'
      ('checked_out', 'verified')                          → 'Checked Out'
      ('cancelled',  *)                                    → 'Cancelled'
      ('rejected',   *)                                    → 'Rejected'
    """
    b = _normalize_legacy_booking_status(booking_status)
    p_norm = normalize_legacy_payment_status(payment_status)
    p = p_norm or payment_status  # keep original for display fallback if unknown
    has_due = _has_balance_due(invoice)

    # Terminal states: booking_status alone determines the label
    if b == 'cancelled':
        return 'Cancelled'
    if b == 'rejected':
        return 'Rejected'

    if b == 'new_request':
        return 'New Request'
    if b == 'pending_payment':
        return 'Pending Payment'

    if b == 'payment_uploaded':
        if p == 'mismatch':
            return 'Payment Uploaded / Amount Mismatch'
        return 'Payment Uploaded / Needs Review'

    if b == 'payment_verified':
        return 'Partial Payment Verified' if has_due else 'Payment Verified'

    if b == 'confirmed':
        return 'Confirmed / Balance Due' if has_due else 'Confirmed'

    if b == 'checked_in':
        return 'Checked In / Balance Due' if has_due else 'Checked In'

    if b == 'checked_out':
        return 'Checked Out / Balance Due' if has_due else 'Checked Out'

    # Unknown or non-canonical fallback — never crash a template
    if b is None:
        return 'Unknown'
    return str(b).replace('_', ' ').title()


def get_status_badge_class(booking_status: Optional[str],
                           payment_status: Optional[str],
                           invoice=None) -> str:
    """Return Tailwind CSS classes for a status badge that fuse booking +
    payment + balance state. Mirrors the colors used by the existing UI but
    extends to the new states.

    Color palette:
      red    — needs attention (new request, awaiting payment, awaiting upload)
      amber  — slip uploaded, admin needs to review
      orange — admin flagged a mismatch (action needed by guest)
      yellow — payment verified but balance still due (partial)
      green  — payment verified and complete; or confirmed
      indigo — checked in (active stay)
      gray   — terminal states (cancelled, rejected, checked out)
    """
    b = _normalize_legacy_booking_status(booking_status)
    p_norm = normalize_legacy_payment_status(payment_status)
    p = p_norm or payment_status
    has_due = _has_balance_due(invoice)

    if b in ('cancelled', 'rejected'):
        return 'bg-gray-100 text-gray-700'

    if b in ('new_request', 'pending_payment'):
        return 'bg-red-100 text-red-700'

    if b == 'payment_uploaded':
        if p == 'mismatch':
            return 'bg-orange-100 text-orange-700'
        return 'bg-amber-100 text-amber-700'

    if b in ('payment_verified', 'confirmed'):
        return 'bg-yellow-100 text-yellow-700' if has_due else 'bg-green-100 text-green-700'

    if b == 'checked_in':
        return 'bg-indigo-100 text-indigo-700'

    if b == 'checked_out':
        return 'bg-gray-100 text-gray-600'

    # Unknown — neutral gray, never crash a template
    return 'bg-gray-100 text-gray-600'


# ── Jinja registration helper ───────────────────────────────────────────────

def register_jinja_helpers(app) -> None:
    """Expose helpers as Jinja globals so templates can call them directly."""
    app.jinja_env.globals['status_label'] = get_status_label
    app.jinja_env.globals['status_badge'] = get_status_badge_class
    app.jinja_env.globals['can_confirm'] = can_confirm
    app.jinja_env.globals['BOOKING_STATUSES'] = BOOKING_STATUSES
    app.jinja_env.globals['PAYMENT_STATUSES'] = PAYMENT_STATUSES
    app.jinja_env.globals['CONFIRMABLE_FROM'] = CONFIRMABLE_FROM
