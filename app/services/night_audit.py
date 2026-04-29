"""Night Audit V1 — pre-checks, business-date state, run lifecycle.

Pure-function helpers that the route handlers use to (1) read or
flip the property's current business date, (2) classify the day's
operational state into blocking vs warning issues, and (3) commit
the close as a single atomic step.

Design contract (binding, see docs/accounts_business_date_night_audit_plan.md):

  - Business date is operator-controlled. It does NOT auto-follow
    server-clock midnight. It advances ONLY when Night Audit
    completes successfully.
  - Night Audit is human-confirmed. AI may summarize later, but
    the click that flips the date stays human.
  - Pre-checks return categorized issues:
      'blocking'  → cannot run audit until resolved
      'warning'   → operator may proceed after acknowledging
  - V1 deliberately does NOT touch booking.status, invoice.payment_status,
    or any guest data. The audit ONLY rolls the business date and
    snapshots the day's pre-check state. Phase 4 will add room-charge
    auto-posting + revenue snapshots.
  - Server-clock skew is treated as a blocker: if the server clock
    is more than 6 hours BEHIND the closing business date, refuse
    the run (likely clock misconfiguration).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional


# ── Issue dataclass + severities ─────────────────────────────────────

@dataclass
class AuditIssue:
    code:        str          # short stable identifier (e.g. 'arrivals_pending')
    severity:    str          # 'blocking' | 'warning'
    title:       str          # human-readable label
    detail:      str          # one-line description
    count:       int = 0      # number of items affected
    refs:        list = field(default_factory=list)  # booking_refs / ids
    fix_hint:    str = ''     # suggested operator action


SEVERITY_BLOCKING = 'blocking'
SEVERITY_WARNING  = 'warning'

# Booking statuses considered "active" for night-audit purposes
_ACTIVE_BOOKING_STATUSES = (
    'new_request', 'pending_payment', 'payment_uploaded',
    'payment_verified', 'confirmed', 'checked_in',
)
_DEPARTING_STATUSES = ('checked_in', 'confirmed', 'payment_verified')


# ── Business-date state singleton helpers ───────────────────────────

def get_business_date_state():
    """Return the (singleton) BusinessDateState row, or None if missing.

    V1: the migration seeds exactly one row. If something has gone
    wrong (manual delete, fresh test DB without seed) the night-audit
    pre-check will report this as a BLOCKER.
    """
    from ..models import BusinessDateState
    return BusinessDateState.query.order_by(BusinessDateState.id.asc()).first()


def current_business_date() -> date:
    """Convenience: return the current business date, or today's date
    as a fall-back if state is missing (with a logger warning).

    Routes that need to STAMP business_date on new rows should use
    this helper. Routes that need to RUN AUDIT should use
    get_business_date_state() so they can detect the missing-state
    blocker explicitly.
    """
    import logging
    state = get_business_date_state()
    if state is None:
        logging.getLogger(__name__).warning(
            'BusinessDateState row is missing — falling back to date.today(). '
            'Night Audit will refuse to run until the row is restored.'
        )
        return date.today()
    return state.current_business_date


# ── Pre-check helpers (each returns 0 or 1 AuditIssue) ──────────────

def _check_state_present() -> Optional[AuditIssue]:
    if get_business_date_state() is None:
        return AuditIssue(
            code='no_business_date_state',
            severity=SEVERITY_BLOCKING,
            title='Business date state is missing',
            detail=('No row found in business_date_state. Night Audit '
                    'cannot run without a current business date.'),
            count=1,
            fix_hint='Ask an engineer to re-run the night-audit migration.',
        )
    return None


def _check_audit_in_progress() -> Optional[AuditIssue]:
    state = get_business_date_state()
    if state is not None and state.audit_in_progress:
        started = state.audit_started_at.strftime('%Y-%m-%d %H:%M') \
                  if state.audit_started_at else '(unknown)'
        user = (state.audit_started_by.username
                if state.audit_started_by else '(unknown)')
        return AuditIssue(
            code='audit_in_progress',
            severity=SEVERITY_BLOCKING,
            title='Another Night Audit is in progress',
            detail=f'Started by {user} at {started}.',
            count=1,
            fix_hint=('Wait for the in-progress audit to complete, '
                      'or ask an admin to clear the flag.'),
        )
    return None


def _check_clock_skew(business_date: date) -> Optional[AuditIssue]:
    """Refuse the close if server clock seems to be in the wrong week."""
    server_date = datetime.utcnow().date()
    if server_date < business_date - timedelta(days=1):
        # Server thinks it's MORE than 1 day before the business date —
        # operator probably has the date wrong, or NTP is broken.
        return AuditIssue(
            code='clock_skew',
            severity=SEVERITY_BLOCKING,
            title='Server clock and business date out of sync',
            detail=(f'Server thinks today is {server_date}, '
                    f'but business date is {business_date}. '
                    'Likely server-clock misconfiguration.'),
            count=1,
            fix_hint='Check NTP / system clock before running audit.',
        )
    return None


def _check_pending_arrivals(business_date: date) -> Optional[AuditIssue]:
    from ..models import Booking
    rows = (
        Booking.query
        .filter(Booking.check_in_date == business_date)
        .filter(Booking.status.in_(
            ('new_request', 'pending_payment', 'payment_uploaded',
             'payment_verified', 'confirmed'),
        ))
        .all()
    )
    if not rows:
        return None
    return AuditIssue(
        code='arrivals_not_checked_in',
        severity=SEVERITY_WARNING,
        title=f'{len(rows)} arrival{"s" if len(rows) != 1 else ""} not yet checked in',
        detail=('Guests expected today have not been checked in yet. '
                'Mark them as no-show or check them in before closing.'),
        count=len(rows),
        refs=[r.booking_ref for r in rows],
        fix_hint='Check in or mark as no-show via Front Office → Arrivals.',
    )


def _check_departures_in_house(business_date: date) -> Optional[AuditIssue]:
    from ..models import Booking
    rows = (
        Booking.query
        .filter(Booking.check_out_date == business_date)
        .filter(Booking.status == 'checked_in')
        .all()
    )
    if not rows:
        return None
    return AuditIssue(
        code='departures_still_in_house',
        severity=SEVERITY_WARNING,
        title=f'{len(rows)} departure{"s" if len(rows) != 1 else ""} not yet checked out',
        detail=('Guests due to leave today are still marked checked-in. '
                'Check them out before closing the day.'),
        count=len(rows),
        refs=[r.booking_ref for r in rows],
        fix_hint='Check out via Front Office → Departures or booking detail.',
    )


def _check_overdue_in_house(business_date: date) -> Optional[AuditIssue]:
    from ..models import Booking
    rows = (
        Booking.query
        .filter(Booking.check_out_date < business_date)
        .filter(Booking.status == 'checked_in')
        .all()
    )
    if not rows:
        return None
    return AuditIssue(
        code='overdue_in_house',
        severity=SEVERITY_WARNING,
        title=f'{len(rows)} overdue in-house booking{"s" if len(rows) != 1 else ""}',
        detail=('Bookings whose check-out date is in the past are still '
                'marked checked-in. Resolve before closing.'),
        count=len(rows),
        refs=[r.booking_ref for r in rows],
        fix_hint=('Either check out the guest or post a late-checkout fee + '
                  'extend the stay before audit.'),
    )


def _check_outstanding_folios(business_date: date) -> Optional[AuditIssue]:
    """Bookings that depart today with positive folio balance."""
    from ..models import Booking
    from .folio import folio_balance

    rows = (
        Booking.query
        .filter(Booking.check_out_date == business_date)
        .filter(Booking.status.in_(_DEPARTING_STATUSES))
        .all()
    )
    flagged = []
    for b in rows:
        if folio_balance(b) > 0.01:
            flagged.append(b)
    if not flagged:
        return None
    return AuditIssue(
        code='outstanding_folio_at_departure',
        severity=SEVERITY_WARNING,
        title=(f'{len(flagged)} departing booking'
               f'{"s have" if len(flagged) != 1 else " has"} '
               f'outstanding folio balance'),
        detail=('Departing guests with positive balance — settle or '
                'invoice before closing the day.'),
        count=len(flagged),
        refs=[b.booking_ref for b in flagged],
        fix_hint='Post payments via the Receipts panel before audit.',
    )


def _check_pending_payments() -> Optional[AuditIssue]:
    """Payments still in pending_review state. WARNING — not BLOCKING — for V1
    so the operator can choose to proceed and resolve in the next day."""
    from ..models import Invoice
    rows = (
        Invoice.query
        .filter(Invoice.payment_status == 'pending_review')
        .all()
    )
    if not rows:
        return None
    return AuditIssue(
        code='invoices_pending_review',
        severity=SEVERITY_WARNING,
        title=(f'{len(rows)} invoice'
               f'{"s" if len(rows) != 1 else ""} '
               f'awaiting payment review'),
        detail=('Payment slips uploaded but not yet verified. Verify or '
                'reject before closing for cleanest audit trail.'),
        count=len(rows),
        refs=[i.invoice_number for i in rows],
        fix_hint='Verify or reject in the booking detail page.',
    )


def _check_payment_mismatch() -> Optional[AuditIssue]:
    """Payments flagged as mismatched. BLOCKING for V1 — data-integrity issue."""
    from ..models import Invoice
    rows = (
        Invoice.query
        .filter(Invoice.payment_status == 'mismatch')
        .all()
    )
    if not rows:
        return None
    return AuditIssue(
        code='invoices_payment_mismatch',
        severity=SEVERITY_BLOCKING,
        title=(f'{len(rows)} invoice'
               f'{"s have" if len(rows) != 1 else " has"} '
               f'payment mismatch'),
        detail=('Payment amounts do not match invoice totals. Resolve '
                'before closing — closing with mismatched amounts '
                'corrupts daily revenue numbers.'),
        count=len(rows),
        refs=[i.invoice_number for i in rows],
        fix_hint=('Adjust the folio (discount / adjustment) or void the '
                  'mismatched payment in the cashiering panel.'),
    )


# ── Aggregator ──────────────────────────────────────────────────────

def run_pre_checks(business_date: date) -> list:
    """Return all AuditIssues found for the closing business date."""
    issues = []
    for fn in (_check_state_present,):
        result = fn()
        if result:
            issues.append(result)
    # If state itself is missing, don't bother with the rest.
    if any(i.code == 'no_business_date_state' for i in issues):
        return issues

    for fn in (_check_audit_in_progress, _check_payment_mismatch):
        result = fn()
        if result:
            issues.append(result)
    issues.append(_check_clock_skew(business_date))
    issues.append(_check_pending_arrivals(business_date))
    issues.append(_check_departures_in_house(business_date))
    issues.append(_check_overdue_in_house(business_date))
    issues.append(_check_outstanding_folios(business_date))
    issues.append(_check_pending_payments())

    return [i for i in issues if i is not None]


def split_by_severity(issues: list):
    blocking = [i for i in issues if i.severity == SEVERITY_BLOCKING]
    warning  = [i for i in issues if i.severity == SEVERITY_WARNING]
    return blocking, warning


# ── Run lifecycle ───────────────────────────────────────────────────

def can_run_audit(business_date: date) -> tuple:
    """Return (ok: bool, issues: list[AuditIssue]).

    ok is True iff there are zero blocking issues. Routes use this
    BEFORE calling commit_audit_close().
    """
    issues = run_pre_checks(business_date)
    blocking, _ = split_by_severity(issues)
    return (len(blocking) == 0, issues)


def serialize_issues(issues: list) -> list:
    """Convert AuditIssue dataclasses to JSON-safe dicts for storage
    in NightAuditRun.summary_json."""
    return [
        {
            'code':     i.code,
            'severity': i.severity,
            'title':    i.title,
            'detail':   i.detail,
            'count':    i.count,
            'refs':     i.refs[:50],   # cap to keep audit row tight
        }
        for i in issues
    ]
