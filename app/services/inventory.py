"""Rates & Inventory V1 — pricing, restrictions, availability.

Pure-function helpers consumed by the inventory routes (and, later,
the booking-engine layer). No I/O side effects beyond DB reads.

Design contract:

  - V1 is ADDITIVE. Existing booking flows continue to read
    Room.price_per_night and Room.room_type unchanged. The booking
    engine is explicitly out of V1 scope.

  - Pricing precedence (most-specific first):
        1. RateOverride covering the date AND the (room_type, plan)
        2. RateOverride covering the date AND the room_type (any plan)
        3. RatePlan.base_rate
        4. Property fallback (passed by caller, e.g. Room.price_per_night)

  - Restrictions compose: when multiple active RateRestriction rows
    cover a date, the MOST RESTRICTIVE wins:
        * stop_sell      → True if ANY says True
        * closed_to_*    → True if ANY says True
        * min_stay       → MAX of all min_stay values
        * max_stay       → MIN of all non-null max_stay values

  - Availability counts are room-type-level. They subtract:
        * out-of-order rooms (housekeeping_status='out_of_order')
        * maintenance rooms (status='maintenance')
        * rooms with overlapping bookings in active statuses
        * rooms behind RoomBlock entries that overlap the requested span
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional


# ── Validation helpers ──────────────────────────────────────────────

def validate_date_range(start_date, end_date) -> Optional[str]:
    """Return error string if invalid, else None."""
    if start_date is None or end_date is None:
        return 'start_date and end_date are required.'
    if not isinstance(start_date, date) or not isinstance(end_date, date):
        return 'dates must be date objects.'
    if end_date < start_date:
        return 'end_date must be on or after start_date.'
    return None


def validate_nightly_rate(rate) -> Optional[str]:
    if rate is None:
        return 'nightly_rate is required.'
    try:
        v = float(rate)
    except (TypeError, ValueError):
        return 'nightly_rate must be a number.'
    if v < 0:
        return 'nightly_rate cannot be negative.'
    return None


def validate_min_max_stay(min_stay, max_stay) -> Optional[str]:
    if min_stay is not None:
        try:
            mn = int(min_stay)
        except (TypeError, ValueError):
            return 'min_stay must be an integer.'
        if mn < 1:
            return 'min_stay must be >= 1.'
    else:
        mn = None
    if max_stay is not None:
        try:
            mx = int(max_stay)
        except (TypeError, ValueError):
            return 'max_stay must be an integer.'
        if mx < 1:
            return 'max_stay must be >= 1.'
    else:
        mx = None
    if mn is not None and mx is not None and mx < mn:
        return 'max_stay must be >= min_stay.'
    return None


# ── Pricing ─────────────────────────────────────────────────────────

def nightly_rate_for(room_type_id, on_date,
                      *, rate_plan_id=None, fallback=None):
    """Return the effective nightly rate for `room_type_id` on `on_date`.

    Args:
      room_type_id: int. Required.
      on_date: date. The night to price.
      rate_plan_id: optional int. If given, prefer overrides scoped to
        that plan; otherwise pick the room-type-scoped override.
      fallback: optional float. Returned if no override AND no plan
        base_rate matches.

    Returns:
      float (or None if `fallback` is None and nothing matched).
    """
    from ..models import RateOverride, RatePlan

    if room_type_id is None or on_date is None:
        return fallback

    q = (RateOverride.query
         .filter_by(room_type_id=room_type_id, is_active=True)
         .filter(RateOverride.start_date <= on_date)
         .filter(RateOverride.end_date >= on_date)
         .order_by(RateOverride.created_at.desc()))

    if rate_plan_id is not None:
        # Prefer plan-scoped override; if none, fall through to type-only
        plan_match = (q.filter(RateOverride.rate_plan_id == rate_plan_id)
                      .first())
        if plan_match is not None:
            return plan_match.nightly_rate

    # Type-scoped override (rate_plan_id NULL)
    type_match = q.filter(RateOverride.rate_plan_id.is_(None)).first()
    if type_match is not None:
        return type_match.nightly_rate

    # Fall back to plan base_rate
    if rate_plan_id is not None:
        plan = RatePlan.query.get(rate_plan_id)
        if plan and plan.is_active:
            return plan.base_rate

    return fallback


def price_stay(room_type_id, check_in, check_out,
               *, rate_plan_id=None, fallback=None):
    """Return total price for a [check_in, check_out) stay, plus per-night
    breakdown for transparency."""
    if check_out <= check_in:
        return {'total': 0.0, 'nights': []}
    nights = []
    total = 0.0
    d = check_in
    while d < check_out:
        rate = nightly_rate_for(room_type_id, d,
                                rate_plan_id=rate_plan_id,
                                fallback=fallback)
        rate = float(rate) if rate is not None else 0.0
        nights.append({'date': d, 'rate': rate})
        total += rate
        d += timedelta(days=1)
    return {'total': round(total, 2), 'nights': nights}


# ── Restrictions ────────────────────────────────────────────────────

@dataclass
class EffectiveRestriction:
    """Composed restriction shape for a given (room_type, date)."""
    stop_sell: bool = False
    closed_to_arrival: bool = False
    closed_to_departure: bool = False
    min_stay: Optional[int] = None
    max_stay: Optional[int] = None


def restrictions_on(room_type_id, on_date) -> EffectiveRestriction:
    """Compose the effective restriction for one date."""
    from ..models import RateRestriction

    rows = (RateRestriction.query
            .filter_by(room_type_id=room_type_id, is_active=True)
            .filter(RateRestriction.start_date <= on_date)
            .filter(RateRestriction.end_date >= on_date)
            .all())

    eff = EffectiveRestriction()
    for r in rows:
        if r.stop_sell:           eff.stop_sell = True
        if r.closed_to_arrival:   eff.closed_to_arrival = True
        if r.closed_to_departure: eff.closed_to_departure = True
        if r.min_stay is not None:
            eff.min_stay = (r.min_stay if eff.min_stay is None
                            else max(eff.min_stay, r.min_stay))
        if r.max_stay is not None:
            eff.max_stay = (r.max_stay if eff.max_stay is None
                            else min(eff.max_stay, r.max_stay))
    return eff


def check_restrictions(room_type_id, check_in, check_out) -> dict:
    """Apply restrictions across a stay span. Returns:
        {ok: bool, reasons: [str], nightly: {date: EffectiveRestriction}}.
    """
    err = validate_date_range(check_in, check_out)
    if err:
        return {'ok': False, 'reasons': [err], 'nightly': {}}
    if check_out <= check_in:
        return {'ok': False, 'reasons': ['stay must be at least 1 night.'],
                'nightly': {}}

    nights = (check_out - check_in).days
    nightly = {}
    reasons = []

    d = check_in
    while d < check_out:
        eff = restrictions_on(room_type_id, d)
        nightly[d] = eff
        if eff.stop_sell:
            reasons.append(
                f'stop_sell active on {d.isoformat()}.')
        if d == check_in and eff.closed_to_arrival:
            reasons.append(
                f'closed-to-arrival on {d.isoformat()}.')
        if eff.min_stay is not None and nights < eff.min_stay:
            reasons.append(
                f'min_stay {eff.min_stay} not met on '
                f'{d.isoformat()} (got {nights}).')
        if eff.max_stay is not None and nights > eff.max_stay:
            reasons.append(
                f'max_stay {eff.max_stay} exceeded on '
                f'{d.isoformat()} (got {nights}).')
        d += timedelta(days=1)

    # Closed-to-departure applies on the night the guest leaves: the
    # check_out_date itself.
    eff_dep = restrictions_on(room_type_id, check_out)
    if eff_dep.closed_to_departure:
        reasons.append(
            f'closed-to-departure on {check_out.isoformat()}.')
        nightly[check_out] = eff_dep

    return {'ok': len(reasons) == 0, 'reasons': reasons, 'nightly': nightly}


# ── Availability ────────────────────────────────────────────────────

# Booking statuses that hold inventory (i.e. block other bookings)
_HOLDING_STATUSES = (
    'unconfirmed', 'pending_verification', 'new_request',
    'pending_payment', 'payment_uploaded', 'payment_verified',
    'confirmed', 'checked_in',
)


def _physical_rooms_of_type(room_type_id):
    """Active rooms of a given type, including legacy string-matched rooms."""
    from ..models import Room, RoomType
    rt = RoomType.query.get(room_type_id)
    if rt is None:
        return []
    # Match by FK first; fall back to legacy string match for unmigrated rows
    fk_matches = Room.query.filter(
        Room.is_active.is_(True),
        Room.room_type_id == room_type_id,
    ).all()
    if fk_matches:
        return fk_matches
    return Room.query.filter(
        Room.is_active.is_(True),
        Room.room_type == rt.name,
    ).all()


def count_available(room_type_id, check_in, check_out) -> int:
    """Number of rooms of this type that have no conflict in [check_in, check_out)."""
    from ..models import Booking, RoomBlock

    rooms = _physical_rooms_of_type(room_type_id)
    if not rooms:
        return 0

    available = 0
    for r in rooms:
        # Operational disqualifiers
        if (r.status or '').lower() == 'maintenance':
            continue
        if (r.housekeeping_status or '').lower() == 'out_of_order':
            continue

        # Booking conflict
        conflict = Booking.query.filter(
            Booking.room_id == r.id,
            Booking.status.in_(_HOLDING_STATUSES),
            Booking.check_in_date < check_out,
            Booking.check_out_date > check_in,
        ).first()
        if conflict is not None:
            continue

        # Block conflict
        block = RoomBlock.query.filter(
            RoomBlock.room_id == r.id,
            RoomBlock.start_date < check_out,
            RoomBlock.end_date > check_in,
        ).first()
        if block is not None:
            continue

        available += 1

    return available


def check_bookable(room_type_id, check_in, check_out,
                    *, rate_plan_id=None) -> dict:
    """Compose restrictions + availability into one decision.

    Returns:
      {
        ok:            bool,
        available:     int,
        reasons:       [str],
        nightly:       dict,
      }
    """
    err = validate_date_range(check_in, check_out)
    if err:
        return {'ok': False, 'available': 0, 'reasons': [err],
                'nightly': {}}
    if check_out <= check_in:
        return {'ok': False, 'available': 0,
                'reasons': ['check_out must be after check_in.'],
                'nightly': {}}

    restr = check_restrictions(room_type_id, check_in, check_out)
    available = count_available(room_type_id, check_in, check_out)

    reasons = list(restr['reasons'])
    if available <= 0:
        reasons.append(
            f'no available rooms for the type in the requested span.')

    return {
        'ok':         (len(reasons) == 0),
        'available':  available,
        'reasons':    reasons,
        'nightly':    restr['nightly'],
    }


# ── Inventory summary (for the admin board) ─────────────────────────

def fleet_summary(check_in=None, check_out=None) -> list:
    """One row per active RoomType, with totals + availability for the
    given span (defaults to today/today+1)."""
    from ..models import RoomType

    if check_in is None:
        check_in = date.today()
    if check_out is None:
        check_out = check_in + timedelta(days=1)

    rows = []
    for rt in (RoomType.query
               .filter_by(is_active=True)
               .order_by(RoomType.name)
               .all()):
        physical = _physical_rooms_of_type(rt.id)
        rows.append({
            'room_type':       rt,
            'physical_count':  len(physical),
            'available':       count_available(rt.id, check_in, check_out),
            'effective':       restrictions_on(rt.id, check_in),
            'rate':             nightly_rate_for(rt.id, check_in,
                                                 fallback=None),
        })
    return rows


# ════════════════════════════════════════════════════════════════════
# Booking Engine V2 — inventory truth (single source), holds-aware.
#
# THE contract (spec §2): sellable(type, night) =
#     physical rooms of type
#   − out-of-order rooms
#   − rooms assigned to confirmed/in-house/holding bookings
#   − rooms consumed by pending holds
#   − rooms held by selection holds
#
# No stored counters anywhere — every number below is computed live.
# Every consumer (public engine, front desk, dashboard, tape chart, OTA)
# must call these functions; identical numbers by construction.
# ════════════════════════════════════════════════════════════════════

def free_rooms_for_stay(room_type_id, check_in, check_out):
    """List of physical rooms of the type that are free for the ENTIRE stay
    [check_in, check_out): not maintenance/OOO, no overlapping holding booking,
    no overlapping RoomBlock. This is the CONTIGUITY set — each room here can
    satisfy the whole stay by itself. (count_available == len of this.)"""
    from ..models import Booking, RoomBlock

    out = []
    for r in _physical_rooms_of_type(room_type_id):
        if (r.status or '').lower() == 'maintenance':
            continue
        if (r.housekeeping_status or '').lower() == 'out_of_order':
            continue
        conflict = Booking.query.filter(
            Booking.room_id == r.id,
            Booking.status.in_(_HOLDING_STATUSES),
            Booking.check_in_date < check_out,
            Booking.check_out_date > check_in,
        ).first()
        if conflict is not None:
            continue
        block = RoomBlock.query.filter(
            RoomBlock.room_id == r.id,
            RoomBlock.removed_at.is_(None),
            RoomBlock.start_date < check_out,
            RoomBlock.end_date > check_in,
        ).first()
        if block is not None:
            continue
        out.append(r)
    return out


def _held_qty_on_night(room_type_id, night, hold_type, *, now=None):
    """Sum of qty of LIVE holds of `hold_type` covering `night`.

    'Live' = state='active' AND not past expiry — an expired-but-unswept hold
    no longer holds inventory (the sweep just formalizes the state), so
    availability is correct in the window between expiry and the sweep.
    """
    from datetime import datetime as _dt
    from ..models import Hold, db
    now = now or _dt.utcnow()
    night_end = night + timedelta(days=1)
    total = db.session.query(db.func.coalesce(db.func.sum(Hold.qty), 0)).filter(
        Hold.room_type_id == room_type_id,
        Hold.hold_type == hold_type,
        Hold.state == 'active',
        Hold.expires_at > now,
        Hold.check_in_date < night_end,
        Hold.check_out_date > night,
    ).scalar()
    return int(total or 0)


def selection_held(room_type_id, night, *, now=None):
    return _held_qty_on_night(room_type_id, night, 'selection', now=now)


def pending_held(room_type_id, night, *, now=None):
    return _held_qty_on_night(room_type_id, night, 'pending', now=now)


def assigned_count(room_type_id, night):
    """Physical rooms of the type occupied by a holding booking on `night`."""
    split = _free_or_ooo_split(room_type_id, night)
    return split['physical'] - split['free'] - split['ooo']


def _free_or_ooo_split(room_type_id, night):
    """{physical, ooo, free} for a single night (free excludes bookings+blocks+ooo)."""
    from ..models import Booking, RoomBlock
    rooms = _physical_rooms_of_type(room_type_id)
    night_end = night + timedelta(days=1)
    ooo = 0
    free = 0
    for r in rooms:
        is_ooo = ((r.status or '').lower() == 'maintenance'
                  or (r.housekeeping_status or '').lower() == 'out_of_order')
        blocked = RoomBlock.query.filter(
            RoomBlock.room_id == r.id, RoomBlock.removed_at.is_(None),
            RoomBlock.start_date < night_end, RoomBlock.end_date > night,
        ).first() is not None
        if is_ooo or blocked:
            ooo += 1
            continue
        booked = Booking.query.filter(
            Booking.room_id == r.id, Booking.status.in_(_HOLDING_STATUSES),
            Booking.check_in_date < night_end, Booking.check_out_date > night,
        ).first() is not None
        if not booked:
            free += 1
    return {'physical': len(rooms), 'ooo': ooo, 'free': free}


def sellable(room_type_id, night, *, now=None):
    """THE single sellable number for (type, night). Never negative-floored —
    callers/invariant rely on the raw signed value (a negative means oversell,
    which the invariant flags as HIGH)."""
    split = _free_or_ooo_split(room_type_id, night)
    return (split['free']
            - selection_held(room_type_id, night, now=now)
            - pending_held(room_type_id, night, now=now))


def available_for_stay(room_type_id, check_in, check_out, qty=1, *, now=None):
    """Contiguity-aware availability (spec §2). Returns dict:
        {ok, contiguous_free, min_sellable, qty, reasons}

    ok iff: (a) >= qty WHOLE physical rooms free for the entire stay
    (contiguity), AND (b) sellable >= qty on every night (capacity net of
    OOO/assigned/pending/selection holds). Both must hold — (a) guarantees
    real rooms exist; (b) guarantees soft holds haven't oversold the type.
    """
    err = validate_date_range(check_in, check_out)
    if err:
        return {'ok': False, 'contiguous_free': 0, 'min_sellable': 0,
                'qty': qty, 'reasons': [err]}
    if check_out <= check_in:
        return {'ok': False, 'contiguous_free': 0, 'min_sellable': 0,
                'qty': qty, 'reasons': ['stay must be at least 1 night.']}
    if qty < 1:
        return {'ok': False, 'contiguous_free': 0, 'min_sellable': 0,
                'qty': qty, 'reasons': ['qty must be >= 1.']}

    contiguous_free = len(free_rooms_for_stay(room_type_id, check_in, check_out))
    min_sellable = None
    d = check_in
    while d < check_out:
        s = sellable(room_type_id, d, now=now)
        min_sellable = s if min_sellable is None else min(min_sellable, s)
        d += timedelta(days=1)
    min_sellable = min_sellable if min_sellable is not None else 0

    reasons = []
    if contiguous_free < qty:
        reasons.append(
            f'only {contiguous_free} whole room(s) of the type are free for '
            f'the entire stay (need {qty}); fragmented availability is not '
            f'sold publicly (front-desk override only).')
    if min_sellable < qty:
        reasons.append(
            f'sellable capacity dips to {min_sellable} on at least one night '
            f'(need {qty}) after holds/pending/OOO.')
    return {
        'ok': (contiguous_free >= qty and min_sellable >= qty),
        'contiguous_free': contiguous_free,
        'min_sellable': min_sellable,
        'qty': qty,
        'reasons': reasons,
    }


def occupancy_tiles(night=None, *, now=None):
    """Per-type dashboard tiles (spec §7): sellable / occupied / selection-held /
    pending / OOO for one night — ALL from the single inventory query, so the
    numbers match the portal, board, and any other consumer by construction."""
    from ..models import RoomType
    from datetime import datetime as _dt
    night = night or _dt.utcnow().date()
    tiles = []
    for rt in (RoomType.query.filter_by(is_active=True)
               .order_by(RoomType.name).all()):
        split = _free_or_ooo_split(rt.id, night)
        tiles.append({
            'room_type': rt,
            'physical': split['physical'],
            'ooo': split['ooo'],
            'occupied': split['physical'] - split['free'] - split['ooo'],
            'selection_held': selection_held(rt.id, night, now=now),
            'pending': pending_held(rt.id, night, now=now),
            'sellable': sellable(rt.id, night, now=now),
        })
    return tiles


def invariant_violations(horizon_days=180, *, now=None):
    """Runnable invariant (spec §3): for every (type, night) in the horizon,
    held + pending + assigned <= sellable_capacity, i.e. sellable() >= 0.
    Returns a list of violation dicts (empty == healthy)."""
    from ..models import RoomType
    from datetime import datetime as _dt
    start = (now or _dt.utcnow()).date()
    violations = []
    for rt in RoomType.query.filter_by(is_active=True).all():
        for i in range(horizon_days):
            night = start + timedelta(days=i)
            s = sellable(rt.id, night, now=now)
            if s < 0:
                split = _free_or_ooo_split(rt.id, night)
                violations.append({
                    'room_type_id': rt.id,
                    'room_type': rt.name,
                    'night': night.isoformat(),
                    'sellable': s,
                    'physical': split['physical'],
                    'ooo': split['ooo'],
                    'selection_held': selection_held(rt.id, night, now=now),
                    'pending_held': pending_held(rt.id, night, now=now),
                })
    return violations
