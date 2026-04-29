"""Booking Engine V1 — public availability search + direct booking creation.

Pure functions consumed by app/routes/booking_engine.py. Uses the
Rates & Inventory V1 layer for pricing and availability and the
existing booking lifecycle for the actual create.

Hard contract:

  - V1 is the FIRST inventory-aware booking flow. It is mounted under
    `/book/*` and coexists with the legacy `/availability` + `/submit`
    flow. Existing booking creation paths are NOT modified — both
    flows write into the same Booking table with the same lifecycle
    statuses (`pending_payment` / `payment_uploaded`).

  - Availability is room-TYPE level. The service picks one specific
    physical Room of that type at create time (the lowest-numbered
    physical room of the type that has no conflict for the requested
    span). No overbooking is possible: every disqualifier
    (booking conflict, room block, OOO, maintenance) is enforced both
    in the search count AND in the create-time guard.

  - Pricing uses inventory.price_stay() so seasonal overrides apply.
    If a RatePlan is given, plan-scoped overrides win (most-specific
    first); otherwise type-scoped overrides apply. Falls back to
    the chosen Room's price_per_night if no rate plan / override
    matches that night.

  - No payment gateway. The flow ends at the same lifecycle states
    the existing /submit flow uses:
        booking.status   = 'pending_payment'
        invoice.payment_status = 'not_received'
    The confirmation page shows manual bank-transfer instructions
    (existing pattern). When/if a gateway is added later, only the
    confirmation step changes.

  - No WhatsApp / email / Gemini side effects in V1. The audit trail
    via app.services.audit.log_activity is the source of truth.

  - ActivityLog actions:
        booking_engine.search_performed
        booking_engine.booking_created
        booking_engine.booking_failed
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional


# ── Validation ──────────────────────────────────────────────────────

def validate_search_input(check_in, check_out, guests=1) -> Optional[str]:
    """Return error string if invalid, else None."""
    if not isinstance(check_in, date) or not isinstance(check_out, date):
        return 'check-in and check-out must be valid dates.'
    if check_out <= check_in:
        return 'check-out must be after check-in.'
    nights = (check_out - check_in).days
    if nights < 1:
        return 'stay must be at least 1 night.'
    if nights > 60:
        return 'stay cannot exceed 60 nights.'
    today = date.today()
    if check_in < today:
        return 'check-in cannot be in the past.'
    try:
        g = int(guests)
    except (TypeError, ValueError):
        return 'guest count must be a number.'
    if g < 1:
        return 'guest count must be at least 1.'
    if g > 20:
        return 'guest count cannot exceed 20.'
    return None


def parse_iso_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(s.strip())
    except (ValueError, AttributeError):
        return None


# ── Search ──────────────────────────────────────────────────────────

@dataclass
class AvailabilityOption:
    """One sellable option returned by search_availability."""
    room_type_id: int
    room_type_code: str
    room_type_name: str
    max_occupancy: int
    available: int            # number of physical rooms of this type
    nights: int
    rate_plan_id: Optional[int] = None
    rate_plan_name: Optional[str] = None
    nightly_breakdown: list = field(default_factory=list)
    subtotal: float = 0.0
    total: float = 0.0
    currency: str = 'USD'
    refundable: bool = True
    bookable: bool = True
    reasons: list = field(default_factory=list)


def search_availability(check_in, check_out, guests=1) -> dict:
    """Return list of AvailabilityOption for the request, plus errors.

    Picks the FIRST active rate plan per room type for V1 quoting.
    Future versions will let the operator publish multiple plans per
    type (BAR / non-refundable / etc.).
    """
    from ..models import RoomType, RatePlan
    from . import inventory as inv

    err = validate_search_input(check_in, check_out, guests)
    if err:
        return {'options': [], 'error': err, 'nights': 0}

    nights = (check_out - check_in).days
    options = []

    types = (RoomType.query
             .filter_by(is_active=True)
             .order_by(RoomType.name)
             .all())

    for rt in types:
        if rt.max_occupancy < int(guests):
            # Filter by capacity — don't surface a Twin to a 4-guest request
            continue

        # Pick the first active rate plan for this type (V1 — single
        # publishable plan per type). If no plan, we still surface the
        # type at the room.price_per_night fallback.
        plan = (RatePlan.query
                .filter_by(room_type_id=rt.id, is_active=True)
                .order_by(RatePlan.created_at.desc())
                .first())
        plan_id = plan.id if plan else None

        # Use the lowest-numbered physical room of the type as the
        # fallback rate (every room of a type has the same legacy
        # price_per_night in V1).
        fallback_rate = None
        first_room = (rt.rooms
                      .filter_by(is_active=True)
                      .order_by(rt.rooms.expression.right.table.c.number)
                      .first()
                      if False else None)  # the dynamic chain above is fragile
        if not first_room:
            from ..models import Room
            first_room = (Room.query
                          .filter_by(is_active=True, room_type_id=rt.id)
                          .order_by(Room.number)
                          .first())
            if first_room is None:
                first_room = (Room.query
                              .filter_by(is_active=True, room_type=rt.name)
                              .order_by(Room.number)
                              .first())
        if first_room is not None:
            fallback_rate = float(first_room.price_per_night or 0)

        # Nightly + total via inventory
        priced = inv.price_stay(
            rt.id, check_in, check_out,
            rate_plan_id=plan_id,
            fallback=fallback_rate,
        )
        # Bookable check: combines restrictions + physical availability
        bk = inv.check_bookable(rt.id, check_in, check_out,
                                rate_plan_id=plan_id)

        opt = AvailabilityOption(
            room_type_id=rt.id,
            room_type_code=rt.code,
            room_type_name=rt.name,
            max_occupancy=rt.max_occupancy,
            available=bk['available'],
            nights=nights,
            rate_plan_id=plan_id,
            rate_plan_name=(plan.name if plan else None),
            nightly_breakdown=priced['nights'],
            subtotal=priced['total'],
            total=priced['total'],   # tax/service deferred — see report
            currency=(plan.currency if plan else 'USD'),
            refundable=(plan.is_refundable if plan else True),
            bookable=bk['ok'],
            reasons=list(bk['reasons']),
        )
        options.append(opt)

    return {'options': options, 'error': None, 'nights': nights}


# ── Quote ───────────────────────────────────────────────────────────

def quote_stay(room_type_id, check_in, check_out, guests=1,
               *, rate_plan_id=None) -> dict:
    """Re-quote a single room type just before the user fills the form."""
    from ..models import RoomType, RatePlan
    from . import inventory as inv

    rt = RoomType.query.get(room_type_id)
    if rt is None or not rt.is_active:
        return {'ok': False, 'error': 'room type not found.'}

    err = validate_search_input(check_in, check_out, guests)
    if err:
        return {'ok': False, 'error': err}

    if rt.max_occupancy < int(guests):
        return {'ok': False,
                'error': f'this room type sleeps a maximum of {rt.max_occupancy}.'}

    plan = None
    if rate_plan_id:
        plan = RatePlan.query.get(rate_plan_id)
    if plan is None:
        plan = (RatePlan.query
                .filter_by(room_type_id=rt.id, is_active=True)
                .order_by(RatePlan.created_at.desc())
                .first())

    plan_id = plan.id if plan else None
    fallback_rate = None
    from ..models import Room
    first_room = (Room.query
                  .filter_by(is_active=True, room_type_id=rt.id)
                  .order_by(Room.number)
                  .first())
    if first_room is None:
        first_room = (Room.query
                      .filter_by(is_active=True, room_type=rt.name)
                      .order_by(Room.number)
                      .first())
    if first_room is not None:
        fallback_rate = float(first_room.price_per_night or 0)

    priced = inv.price_stay(rt.id, check_in, check_out,
                             rate_plan_id=plan_id, fallback=fallback_rate)
    bk = inv.check_bookable(rt.id, check_in, check_out,
                             rate_plan_id=plan_id)
    return {
        'ok':           bk['ok'] and len(priced['nights']) > 0,
        'error':        None if bk['ok'] else '; '.join(bk['reasons']),
        'room_type':    rt,
        'rate_plan':    plan,
        'nights':       priced['nights'],
        'subtotal':     priced['total'],
        'total':        priced['total'],
        'currency':     (plan.currency if plan else 'USD'),
        'available':    bk['available'],
    }


# ── Create ──────────────────────────────────────────────────────────

def _pick_physical_room(room_type_id, check_in, check_out):
    """Return one physical Room of the requested type that is bookable
    for [check_in, check_out), or None."""
    from ..models import Room, RoomType, Booking, RoomBlock

    rt = RoomType.query.get(room_type_id)
    if rt is None:
        return None

    holding = (
        'unconfirmed', 'pending_verification', 'new_request',
        'pending_payment', 'payment_uploaded', 'payment_verified',
        'confirmed', 'checked_in',
    )

    candidates = (Room.query
                  .filter_by(is_active=True, room_type_id=rt.id)
                  .order_by(Room.number)
                  .all())
    if not candidates:
        candidates = (Room.query
                      .filter_by(is_active=True, room_type=rt.name)
                      .order_by(Room.number)
                      .all())

    for r in candidates:
        if (r.status or '').lower() == 'maintenance':
            continue
        if (r.housekeeping_status or '').lower() == 'out_of_order':
            continue
        conflict = (Booking.query
                    .filter(Booking.room_id == r.id,
                            Booking.status.in_(holding),
                            Booking.check_in_date < check_out,
                            Booking.check_out_date > check_in)
                    .first())
        if conflict is not None:
            continue
        block = (RoomBlock.query
                 .filter(RoomBlock.room_id == r.id,
                         RoomBlock.start_date < check_out,
                         RoomBlock.end_date > check_in)
                 .first())
        if block is not None:
            continue
        return r

    return None


def create_direct_booking(*,
        room_type_id,
        check_in, check_out,
        guests,
        first_name, last_name, phone, email=None,
        nationality=None, special_requests=None,
        rate_plan_id=None) -> dict:
    """Atomic: validate → re-quote → pick room → create guest + booking
    + invoice. Caller is responsible for db.session.commit().

    Returns:
        {ok: bool, booking: Booking|None, error: str|None,
         total: float, room_number: str|None}
    """
    from ..models import db, Booking, Guest, Invoice
    from ..routes.bookings import generate_booking_ref
    from ..routes.invoices import generate_invoice
    from . import inventory as inv

    # Re-validate input
    err = validate_search_input(check_in, check_out, guests)
    if err:
        return {'ok': False, 'error': err, 'booking': None,
                'total': 0.0, 'room_number': None}

    if not first_name or not last_name:
        return {'ok': False, 'error': 'first and last name are required.',
                'booking': None, 'total': 0.0, 'room_number': None}
    if not phone or len(phone.strip()) < 5:
        return {'ok': False, 'error': 'phone number is required.',
                'booking': None, 'total': 0.0, 'room_number': None}

    # Re-run availability check to defend against race
    bk = inv.check_bookable(room_type_id, check_in, check_out,
                             rate_plan_id=rate_plan_id)
    if not bk['ok']:
        return {'ok': False,
                'error': '; '.join(bk['reasons']) or 'no availability.',
                'booking': None, 'total': 0.0, 'room_number': None}

    # Pick a specific physical room
    room = _pick_physical_room(room_type_id, check_in, check_out)
    if room is None:
        return {'ok': False,
                'error': 'no physical rooms available for this type.',
                'booking': None, 'total': 0.0, 'room_number': None}

    # Re-price
    priced = inv.price_stay(room_type_id, check_in, check_out,
                             rate_plan_id=rate_plan_id,
                             fallback=float(room.price_per_night or 0))
    total = float(priced['total'])
    nights = (check_out - check_in).days

    # Create / reuse guest. V1: always create a fresh guest row.
    # (Future) duplicate-detect by phone + last_name.
    guest = Guest(
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        phone=phone.strip(),
        email=(email.strip() if email else None),
        nationality=(nationality.strip() if nationality else None),
    )
    db.session.add(guest)
    db.session.flush()

    booking = Booking(
        booking_ref=generate_booking_ref(),
        room_id=room.id,
        guest_id=guest.id,
        check_in_date=check_in,
        check_out_date=check_out,
        num_guests=int(guests),
        special_requests=(special_requests.strip() if special_requests else None),
        total_amount=total,
        # Lifecycle: same as the legacy /submit flow when no payment slip
        # is uploaded. Operator confirms once payment lands.
        status='pending_payment',
    )
    db.session.add(booking)
    db.session.flush()

    invoice = generate_invoice(booking)
    # Set the lifecycle-correct V1 status. Use the returned object so we
    # don't depend on backref refresh timing (booking.invoice can be
    # None until the session refreshes the relationship).
    if invoice is not None:
        invoice.payment_status = 'not_received'

    return {
        'ok': True,
        'error': None,
        'booking': booking,
        'total': total,
        'room_number': room.number,
        'nights': nights,
    }
