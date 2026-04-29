"""Reports & Analytics V1 — trustworthy metrics, no double counting.

Every metric below documents its CANONICAL SOURCE — the single table /
column the value reads from. Where the same business number could be
computed two different ways, this module picks one and explains why.

Canonical sources for V1:

  - ROOM REVENUE  → Booking.total_amount summed across bookings whose
                    check-in falls in the range AND whose status is
                    NOT cancelled/rejected.
                    WHY: per the project's anti-double-counting rule
                    ("Booking.total_amount is canonical room revenue;
                    folio is additive"). FolioItem.item_type='room_charge'
                    auto-posting is Phase 4, deferred. Until then the
                    folio holds AT MOST ancillary lines for any given
                    booking, so summing folio room_charge rows would
                    miss most of the room revenue.

  - ANCILLARY REV → FolioItem.total_amount summed for non-voided rows
                    whose item_type is in NON_ROOM_REVENUE_TYPES and
                    whose created_at falls in the range.
                    WHY: room_charge is excluded to prevent double-
                    counting with Booking.total_amount. Discounts and
                    payments are stored as negative — see
                    services/folio.py — and are accounted for
                    separately, not summed into ancillary revenue.

  - DISCOUNTS     → |FolioItem.total_amount| summed for item_type='discount',
                    non-voided, created_at in range. Reported as a
                    positive number (the value subtracted from
                    customer charges).

  - PAYMENTS      → CashierTransaction.amount summed for
                    transaction_type='payment' AND status='posted'
                    AND created_at in range.
                    WHY: payments are NOT revenue — they are settlement
                    of charges. Cashiering is the canonical source of
                    payment events; the negative FolioItem rows
                    written by post-payment exist for balance math but
                    are intentionally NOT used for the payment total
                    (avoiding double-count if both flows ever drift).

  - REFUNDS       → CashierTransaction.amount summed for
                    transaction_type='refund' AND status='posted'.

  - OUTSTANDING   → sum of folio_balance(booking) across bookings
                    whose status is in (confirmed, payment_verified,
                    checked_in, payment_uploaded, pending_payment) and
                    whose folio_balance() > 0. Voided folio items are
                    excluded by folio_balance() itself.

  - OCCUPIED      → count of distinct rooms held on the as-of date by a
                    booking whose status is checked_in AND whose
                    [check_in_date, check_out_date) covers that date.

  - ROOM NIGHTS   → for a date range, sum over each date in the range
    SOLD            of the number of bookings whose status is in
                    (checked_in, checked_out) and whose stay covers
                    that date. Only "realized" stays count.
                    WHY: not "confirmed" — confirmed bookings can still
                    cancel; reports for past nights should reflect what
                    actually happened, not what was planned.

  - AVAIL NIGHTS  → number of active rooms × number of dates in range.

Things that are EXPLICITLY DEFERRED (V1 does not surface them):

  - ADR / RevPAR  — derivable from room nights + room revenue but
                    require a stable definition of "rooms available"
                    that handles maintenance + OOO consistently.
                    Surfacing them here without that = misleading.
  - P&L           — needs the Accounting + Expenses module to be
                    finished. It exists in skeleton form, not enough
                    to compute net.
  - Source mix    — Booking has no `source` column yet. Reporting
                    "where did the business come from" is impossible
                    without lying. Defer.
  - Tax           — Invoice.tax_amount is currently 0 across the fleet.
                    Until tax rates are set, "tax collected" = 0,
                    which is technically correct but misleading.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional


# ── Type taxonomy (mirrors services/folio.py) ───────────────────────

# Items that are sellable revenue OUTSIDE of room_charge. These plus
# Booking.total_amount = total customer charges, no double-count.
NON_ROOM_REVENUE_TYPES = (
    'restaurant', 'laundry', 'transfer', 'excursion',
    'goods', 'service', 'fee', 'damage',
)

# Booking statuses that count as "live" for outstanding balance math.
_LIVE_BOOKING_STATUSES = (
    'confirmed', 'payment_verified', 'checked_in',
    'payment_uploaded', 'pending_payment',
)

# Booking statuses that count as a "realized" stay for room-nights-sold.
_REALIZED_STAY_STATUSES = ('checked_in', 'checked_out')

# Booking statuses that disqualify a row from any revenue total.
_NON_REVENUE_BOOKING_STATUSES = (
    'cancelled', 'rejected', 'cancelled_by_guest',
)


# ── Date-range helpers ──────────────────────────────────────────────

@dataclass
class DateRange:
    """Half-open [start_date, end_date_exclusive) used for SQL filters,
    plus an inclusive end_date for human display."""
    start: date
    end_inclusive: date
    label: str

    @property
    def end_exclusive(self) -> date:
        return self.end_inclusive + timedelta(days=1)

    @property
    def start_dt(self) -> datetime:
        return datetime.combine(self.start, time.min)

    @property
    def end_dt_exclusive(self) -> datetime:
        return datetime.combine(self.end_exclusive, time.min)

    @property
    def days(self) -> int:
        return (self.end_inclusive - self.start).days + 1

    def each_day(self) -> Iterable[date]:
        d = self.start
        while d <= self.end_inclusive:
            yield d
            d += timedelta(days=1)


def resolve_range(
        preset: Optional[str] = None,
        start_str: Optional[str] = None,
        end_str: Optional[str] = None,
        *, today=None) -> DateRange:
    """Resolve a query-string date filter into a DateRange.

    preset ∈ {'today', 'yesterday', 'week', 'month', 'custom'}.
    For 'custom', start_str / end_str must both be ISO dates.
    Falls back to 'today' if input is invalid.
    """
    if today is None:
        today = date.today()

    p = (preset or '').lower().strip() or 'today'

    if p == 'today':
        return DateRange(today, today, 'Today')
    if p == 'yesterday':
        y = today - timedelta(days=1)
        return DateRange(y, y, 'Yesterday')
    if p == 'week':
        # Monday → today (this week to date)
        start = today - timedelta(days=today.weekday())
        return DateRange(start, today, 'This week')
    if p == 'month':
        start = date(today.year, today.month, 1)
        return DateRange(start, today, 'This month')
    if p == 'custom':
        try:
            s = date.fromisoformat((start_str or '').strip())
            e = date.fromisoformat((end_str or '').strip())
            if e < s:
                s, e = e, s
            return DateRange(s, e,
                             f'{s.isoformat()} → {e.isoformat()}')
        except (ValueError, AttributeError):
            return DateRange(today, today, 'Today')

    return DateRange(today, today, 'Today')


# ── Operations counts ───────────────────────────────────────────────

def operations_summary(as_of=None) -> dict:
    """Counts of arrivals / departures / in-house / room states for the
    given as_of date (defaults to today). Cheap, fast — driven entirely
    off Booking + Room rows.

    Each value is a single integer. No money, no rates.
    """
    from ..models import Booking, Room

    if as_of is None:
        as_of = date.today()

    arrivals = (Booking.query
                .filter(Booking.check_in_date == as_of)
                .filter(Booking.status.in_(
                    ('confirmed', 'payment_verified', 'payment_uploaded',
                     'pending_payment', 'checked_in')))
                .count())
    departures = (Booking.query
                  .filter(Booking.check_out_date == as_of)
                  .filter(Booking.status.in_(
                      ('checked_in', 'checked_out')))
                  .count())
    in_house = (Booking.query
                .filter(Booking.status == 'checked_in')
                .filter(Booking.check_in_date <= as_of)
                .filter(Booking.check_out_date > as_of)
                .count())

    rooms = Room.query.filter_by(is_active=True).all()
    occupied_room_ids = {b.room_id for b in
                         Booking.query
                         .filter(Booking.status == 'checked_in')
                         .filter(Booking.check_in_date <= as_of)
                         .filter(Booking.check_out_date > as_of)
                         .all()}
    occupied = sum(1 for r in rooms if r.id in occupied_room_ids)
    vacant   = sum(1 for r in rooms if r.id not in occupied_room_ids)
    dirty    = sum(1 for r in rooms if (r.housekeeping_status or '') == 'dirty')
    out_of_order = sum(1 for r in rooms
                       if (r.housekeeping_status or '') == 'out_of_order')

    return {
        'as_of':            as_of,
        'arrivals_today':   arrivals,
        'departures_today': departures,
        'in_house':         in_house,
        'rooms_total':      len(rooms),
        'occupied_rooms':   occupied,
        'vacant_rooms':     vacant,
        'dirty_rooms':      dirty,
        'out_of_order_rooms': out_of_order,
    }


def pending_payment_summary() -> dict:
    """Count + balance of bookings/invoices with money still owed."""
    from ..models import Booking, Invoice
    from .folio import folio_balance

    pending_count = (Invoice.query
                     .filter(Invoice.payment_status.in_(
                         ('not_received', 'pending_review')))
                     .count())

    live = (Booking.query
            .filter(Booking.status.in_(_LIVE_BOOKING_STATUSES))
            .all())
    outstanding_total = 0.0
    outstanding_count = 0
    for b in live:
        bal = folio_balance(b)
        if bal > 0.005:
            outstanding_total += bal
            outstanding_count += 1
    return {
        'pending_count':       pending_count,
        'outstanding_count':   outstanding_count,
        'outstanding_total':   round(outstanding_total, 2),
    }


# ── Revenue ─────────────────────────────────────────────────────────

def room_revenue(rng: DateRange) -> float:
    """Sum of Booking.total_amount across non-cancelled bookings whose
    check-in falls in the range. CANONICAL ROOM REVENUE for V1."""
    from ..models import Booking

    rows = (Booking.query
            .filter(Booking.check_in_date >= rng.start)
            .filter(Booking.check_in_date <= rng.end_inclusive)
            .filter(~Booking.status.in_(_NON_REVENUE_BOOKING_STATUSES))
            .all())
    return round(sum(b.total_amount or 0.0 for b in rows), 2)


def ancillary_revenue_breakdown(rng: DateRange) -> dict:
    """Per-category dict for non-room revenue. Voided items excluded.

    Returns:
        OrderedDict {item_type: float} for each NON_ROOM_REVENUE_TYPES
        type, plus the special key 'total'.
    """
    from ..models import FolioItem

    rows = (FolioItem.query
            .filter(FolioItem.created_at >= rng.start_dt)
            .filter(FolioItem.created_at <  rng.end_dt_exclusive)
            .filter(FolioItem.status != 'voided')
            .filter(FolioItem.item_type.in_(NON_ROOM_REVENUE_TYPES))
            .all())

    out = OrderedDict((t, 0.0) for t in NON_ROOM_REVENUE_TYPES)
    for r in rows:
        out[r.item_type] = out.get(r.item_type, 0.0) + (r.total_amount or 0.0)
    out['total'] = round(sum(out.values()), 2)
    for k in list(out.keys()):
        out[k] = round(out[k], 2)
    return out


def discounts_total(rng: DateRange) -> float:
    """|FolioItem.total_amount| summed for item_type='discount'.
    Returned positive — it's the amount subtracted from customer charges."""
    from ..models import FolioItem

    rows = (FolioItem.query
            .filter(FolioItem.created_at >= rng.start_dt)
            .filter(FolioItem.created_at <  rng.end_dt_exclusive)
            .filter(FolioItem.status != 'voided')
            .filter(FolioItem.item_type == 'discount')
            .all())
    return round(sum(abs(r.total_amount or 0.0) for r in rows), 2)


def adjustments_net(rng: DateRange) -> float:
    """Net of signed adjustment / other rows. Can be ±."""
    from ..models import FolioItem
    rows = (FolioItem.query
            .filter(FolioItem.created_at >= rng.start_dt)
            .filter(FolioItem.created_at <  rng.end_dt_exclusive)
            .filter(FolioItem.status != 'voided')
            .filter(FolioItem.item_type.in_(('adjustment', 'other')))
            .all())
    return round(sum(r.total_amount or 0.0 for r in rows), 2)


def payments_total(rng: DateRange) -> float:
    """Sum of posted payment-type CashierTransactions in range.
    Payments are NOT revenue."""
    from ..models import CashierTransaction
    rows = (CashierTransaction.query
            .filter(CashierTransaction.created_at >= rng.start_dt)
            .filter(CashierTransaction.created_at <  rng.end_dt_exclusive)
            .filter(CashierTransaction.transaction_type == 'payment')
            .filter(CashierTransaction.status == 'posted')
            .all())
    return round(sum(t.amount or 0.0 for t in rows), 2)


def refunds_total(rng: DateRange) -> float:
    from ..models import CashierTransaction
    rows = (CashierTransaction.query
            .filter(CashierTransaction.created_at >= rng.start_dt)
            .filter(CashierTransaction.created_at <  rng.end_dt_exclusive)
            .filter(CashierTransaction.transaction_type == 'refund')
            .filter(CashierTransaction.status == 'posted')
            .all())
    return round(sum(t.amount or 0.0 for t in rows), 2)


def revenue_summary(rng: DateRange) -> dict:
    """Combined revenue snapshot. All numbers are positive money values
    EXCEPT `adjustments_net` which can be ±.

    Note: `total_charges` = room_revenue + ancillary - discounts +
    adjustments_net. Payments and refunds are listed separately and
    are NOT included in total_charges.
    """
    rr = room_revenue(rng)
    anc = ancillary_revenue_breakdown(rng)
    disc = discounts_total(rng)
    adj = adjustments_net(rng)
    pay = payments_total(rng)
    ref = refunds_total(rng)

    total_charges = round(rr + anc['total'] - disc + adj, 2)
    return {
        'range':             rng,
        'room_revenue':      rr,
        'ancillary':         anc,
        'discounts_total':   disc,
        'adjustments_net':   adj,
        'total_charges':     total_charges,
        'payments_received': pay,
        'refunds_paid':      ref,
        'net_cashflow':      round(pay - ref, 2),
    }


def revenue_by_day(rng: DateRange) -> list:
    """Per-day revenue + payments series, suitable for a small bar chart.

    Each row contains: {date, room_revenue, ancillary, payments}.
    """
    series = []
    for d in rng.each_day():
        sub = DateRange(d, d, d.isoformat())
        series.append({
            'date':         d,
            'room_revenue': room_revenue(sub),
            'ancillary':    ancillary_revenue_breakdown(sub)['total'],
            'payments':     payments_total(sub),
        })
    return series


# ── Outstanding ─────────────────────────────────────────────────────

def outstanding_balances(limit=200) -> list:
    """Live bookings with positive folio_balance. Sorted balance-DESC.
    Returns dicts with the booking + balance + per-status flag."""
    from ..models import Booking
    from .folio import folio_balance

    rows = (Booking.query
            .filter(Booking.status.in_(_LIVE_BOOKING_STATUSES))
            .all())
    outstanding = []
    for b in rows:
        bal = folio_balance(b)
        if bal > 0.005:
            outstanding.append({'booking': b, 'balance': round(bal, 2)})
    outstanding.sort(key=lambda r: r['balance'], reverse=True)
    return outstanding[:limit]


# ── Occupancy ───────────────────────────────────────────────────────

def occupancy_for_day(d: date) -> dict:
    """Occupancy snapshot for one calendar date.

    Definitions:
      - rooms_active: Room rows with is_active=True
      - rooms_sellable: rooms_active minus those with status='maintenance'
                        or housekeeping_status='out_of_order' on `d`
                        (we apply CURRENT housekeeping; historical
                        snapshots aren't yet recorded).
      - rooms_occupied: rooms held by a booking whose status is in
                        (checked_in, checked_out) and whose stay covers
                        `d`.
      - occupancy_pct:  rooms_occupied / rooms_active × 100, NOT divided
                        by sellable. We pick the simpler denominator —
                        otherwise an OOO room would inflate occupancy.
    """
    from ..models import Booking, Room

    rooms = Room.query.filter_by(is_active=True).all()
    rooms_total = len(rooms)
    if rooms_total == 0:
        return {'date': d, 'rooms_total': 0, 'rooms_occupied': 0,
                'occupancy_pct': 0.0,
                'room_nights_sold': 0, 'available_room_nights': 0}

    occupied_room_ids = {
        b.room_id for b in
        Booking.query
        .filter(Booking.status.in_(_REALIZED_STAY_STATUSES))
        .filter(Booking.check_in_date <= d)
        .filter(Booking.check_out_date > d)
        .all()
    }
    occupied = sum(1 for r in rooms if r.id in occupied_room_ids)
    pct = round(100.0 * occupied / rooms_total, 1)

    return {
        'date':                  d,
        'rooms_total':           rooms_total,
        'rooms_occupied':        occupied,
        'occupancy_pct':         pct,
        'room_nights_sold':      occupied,           # 1 day
        'available_room_nights': rooms_total,        # 1 day
    }


def occupancy_summary(rng: DateRange) -> dict:
    """Aggregate occupancy across the range."""
    series = [occupancy_for_day(d) for d in rng.each_day()]
    sold = sum(r['room_nights_sold'] for r in series)
    avail = sum(r['available_room_nights'] for r in series)
    return {
        'range':                 rng,
        'series':                series,
        'room_nights_sold':      sold,
        'available_room_nights': avail,
        'occupancy_pct':         round(100.0 * sold / avail, 1)
                                  if avail else 0.0,
    }
