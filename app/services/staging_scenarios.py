"""Staging Scenario Seeder V1.

Generates a believable boutique-island-property dataset on staging:
guests, bookings across every status (in-house, arrivals/departures
today, future confirmed, pending payment, cancelled, checked-out),
folios with charges + payments + a discount + a void, housekeeping
states, and a handful of POS orders.

Anchored on date.today() so the scenario stays realistic on whatever
day the seeder is run. Idempotent when called via `clean=True`
(default): every demo booking / folio / order is wiped first, then
recreated from the canonical script. Rooms, room types, POS
categories, users, and PropertySettings are NEVER touched — they're
treated as the fixed environment.

Designed to be invoked exclusively from the `flask staging
seed-scenarios` CLI command (which enforces STAGING=1). Calling
this from production code is structurally prevented by that gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import random
from typing import Optional


# ── Guest pool ──────────────────────────────────────────────────────────────
# Plausible international + regional mix for a Maldivian island property.
# All names are fictional. Phones are obviously-fake +960 (MV) /
# +91 (IN) / +44 (UK) / +1 (US) numbers.
_GUESTS = (
    # (first, last, phone, nationality, email)
    ('Sarah',   'Mitchell',  '+44 7700 900100', 'United Kingdom', 'sarah.m@example.com'),
    ('James',   'O\'Connor', '+353 86 100 0001','Ireland',        'james.oc@example.com'),
    ('Yuki',    'Tanaka',    '+81 90 1000 0002','Japan',          'yuki.t@example.com'),
    ('Aishath', 'Rasheed',   '+960 777 0001',   'Maldives',       None),
    ('Ahmed',   'Naseer',    '+960 777 0002',   'Maldives',       None),
    ('Priya',   'Sharma',    '+91 98765 00001', 'India',          'priya.s@example.com'),
    ('Rohan',   'Mehta',     '+91 98765 00002', 'India',          'rohan.m@example.com'),
    ('Olivia',  'Bennett',   '+1 415 555 0101', 'United States',  'olivia.b@example.com'),
    ('Daniel',  'Schwarz',   '+49 151 100 0001','Germany',        'daniel.s@example.com'),
    ('Sofia',   'Rossi',     '+39 320 100 0001','Italy',          'sofia.r@example.com'),
    ('Marc',    'Dubois',    '+33 6 1234 5678', 'France',         'marc.d@example.com'),
    ('Linnea',  'Karlsson',  '+46 70 100 0001', 'Sweden',         'linnea.k@example.com'),
    ('Hassan',  'Al-Sabah',  '+965 9000 0001',  'Kuwait',         'hassan.s@example.com'),
    ('Layla',   'Hadid',     '+971 50 100 0001','UAE',            'layla.h@example.com'),
    ('Wei',     'Chen',      '+86 138 0000 0001','China',         'wei.c@example.com'),
    ('Min-jun', 'Park',      '+82 10 1000 0001','South Korea',    'minjun.p@example.com'),
    ('Emily',   'Watson',    '+1 212 555 0102', 'United States',  'emily.w@example.com'),
    ('Lucas',   'Pereira',   '+55 11 99000 0001','Brazil',        'lucas.p@example.com'),
    ('Anya',    'Volkov',    '+7 985 100 0001', 'Russia',         'anya.v@example.com'),
    ('Tom',     'Hartley',   '+44 7700 900200', 'United Kingdom', 'tom.h@example.com'),
    ('Rebecca', 'Cole',      '+1 305 555 0103', 'United States',  'rebecca.c@example.com'),
    ('Ibrahim', 'Waheed',    '+960 777 0003',   'Maldives',       None),
    ('Fathmath','Latheefa',  '+960 777 0004',   'Maldives',       None),
    ('Connor',  'McKenzie',  '+61 4 1000 0001', 'Australia',      'connor.m@example.com'),
    ('Hannah',  'Ng',        '+65 9100 0001',   'Singapore',      'hannah.n@example.com'),
    ('Pieter',  'van Dijk',  '+31 6 1000 0001', 'Netherlands',    'pieter.vd@example.com'),
    ('Marta',   'Nowak',     '+48 600 100 001', 'Poland',         'marta.n@example.com'),
    ('Joaquin', 'Reyes',     '+34 600 100 001', 'Spain',          'joaquin.r@example.com'),
    ('Aria',    'Patel',     '+44 7700 900300', 'United Kingdom', 'aria.p@example.com'),
    ('Noah',    'Lindgren',  '+47 900 10 001',  'Norway',         'noah.l@example.com'),
)


# Booking-source mix (Booking.source values from the model whitelist).
_SOURCES = ('walk_in', 'whatsapp', 'phone', 'agoda', 'booking_com', 'direct_email')


@dataclass(frozen=True)
class _Scenario:
    """Specification for one booking that the seeder will create."""
    label:        str         # Human label for logs ("in-house", "arrival-today", ...)
    room_number:  str
    nights:       int
    check_in_offset: int      # days from today; 0 = today, -3 = three days ago
    booking_status:  str      # bookings.status value
    folio_extras:    tuple    # extra FolioItem types to add ('restaurant', 'laundry', 'transfer', ...)
    payment_state:   str      # 'paid', 'partial', 'unpaid'
    add_discount:    bool = False
    add_void:        bool = False
    actual_check_in: bool = False  # was actual_check_in stamped? (for in-house/checked-out)


# ── Booking scenario script ─────────────────────────────────────────────────
#
# Anchored on today (date.today()) at runtime. Tuples list rooms +
# scenario shape; the seeder picks guests round-robin from the pool.
# Rooms 306 + 310 are intentionally LEFT OUT so they can be set to
# out-of-order. Other rooms with no entry stay vacant + clean.
_SCENARIOS: tuple = (
    # ── In-house (currently staying, status=checked_in) ──
    _Scenario('in-house', '101', 4, -2, 'checked_in', ('restaurant',),                  'partial', actual_check_in=True),
    _Scenario('in-house', '103', 2, -1, 'checked_in', (),                               'paid',    actual_check_in=True),
    _Scenario('in-house', '105', 7, -5, 'checked_in', ('restaurant', 'laundry'),        'partial', add_discount=True, actual_check_in=True),
    _Scenario('in-house', '201', 7, -3, 'checked_in', ('restaurant', 'transfer'),       'paid',    actual_check_in=True),
    _Scenario('in-house', '203', 3, -2, 'checked_in', ('restaurant',),                  'partial', actual_check_in=True),
    _Scenario('in-house', '207', 7, -4, 'checked_in', ('laundry',),                     'paid',    actual_check_in=True),
    _Scenario('in-house', '211', 4, -1, 'checked_in', (),                               'partial', actual_check_in=True),
    _Scenario('in-house', '301', 8, -3, 'checked_in', ('restaurant', 'restaurant'),     'partial', actual_check_in=True),
    _Scenario('in-house', '303', 4, -2, 'checked_in', ('laundry', 'transfer'),          'paid',    actual_check_in=True),
    _Scenario('in-house', '307', 7, -1, 'checked_in', ('restaurant',),                  'partial', actual_check_in=True),
    _Scenario('in-house', '401', 9, -4, 'checked_in', ('restaurant', 'transfer', 'laundry'), 'paid', add_void=True, actual_check_in=True),
    _Scenario('in-house', '311', 2,  0, 'checked_in', (),                               'paid',    actual_check_in=True),

    # ── Arrivals today (status=confirmed, check_in=today, no actual_check_in yet) ──
    _Scenario('arrival-today', '109', 2, 0, 'confirmed', (), 'paid'),
    _Scenario('arrival-today', '209', 4, 0, 'confirmed', (), 'paid'),
    _Scenario('arrival-today', '305', 5, 0, 'confirmed', (), 'partial'),
    _Scenario('arrival-today', '402', 7, 0, 'confirmed', (), 'paid'),

    # ── Departures today (status=checked_in, check_out=today) ──
    _Scenario('departure-today', '110', 3, -3, 'checked_in', ('restaurant',),         'paid',    actual_check_in=True),
    _Scenario('departure-today', '205', 4, -4, 'checked_in', ('laundry',),            'paid',    actual_check_in=True),
    _Scenario('departure-today', '308', 5, -5, 'checked_in', ('restaurant',),         'partial', actual_check_in=True),

    # ── Future confirmed (status=confirmed, check_in > today) ──
    _Scenario('future-confirmed', '102', 3,  3, 'confirmed', (), 'paid'),
    _Scenario('future-confirmed', '204', 4,  2, 'confirmed', (), 'paid'),
    _Scenario('future-confirmed', '212', 4,  4, 'confirmed', (), 'paid'),
    _Scenario('future-confirmed', '302', 4,  5, 'confirmed', (), 'partial'),
    _Scenario('future-confirmed', '309', 4,  8, 'confirmed', (), 'paid'),

    # ── Pending payment ──
    _Scenario('pending-payment', '104', 3,  5, 'pending_payment', (), 'unpaid'),
    _Scenario('pending-payment', '206', 3, 10, 'pending_payment', (), 'unpaid'),
    _Scenario('pending-payment', '304', 3, 15, 'pending_verification', (), 'unpaid'),

    # ── Cancelled ──
    _Scenario('cancelled', '106', 3,  1, 'cancelled', (), 'unpaid'),
    _Scenario('cancelled', '208', 2, -2, 'cancelled', (), 'unpaid'),

    # ── Checked-out recent ──
    _Scenario('checked-out', '107', 3, -7, 'checked_out', ('restaurant',),         'paid', actual_check_in=True),
    _Scenario('checked-out', '108', 5, -6, 'checked_out', ('restaurant', 'laundry'),'paid', actual_check_in=True),
    _Scenario('checked-out', '202', 5, -8, 'checked_out', ('transfer',),           'paid', actual_check_in=True),
    _Scenario('checked-out', '210', 3, -5, 'checked_out', (),                      'paid', actual_check_in=True),
)

# Rooms intentionally left without bookings so they can be marked
# out-of-order for realism.
_OUT_OF_ORDER = (
    ('306', 'AC repair scheduled'),
    ('310', 'Repainting in progress'),
)


# ── Pricing for folio item extras (in MVR) ──────────────────────────────────
_EXTRA_PRICES = {
    'restaurant': [125.0, 220.0, 95.0, 340.0, 180.0],   # one of these per restaurant FolioItem
    'laundry':    [85.0, 145.0, 60.0],
    'transfer':   [350.0, 600.0],                        # airport pickup / dropoff
    'misc':       [50.0],
}
_EXTRA_DESCRIPTIONS = {
    'restaurant': ['Restaurant — dinner',     'Restaurant — lunch',
                   'Restaurant — breakfast',  'Restaurant — beverages',
                   'Room service'],
    'laundry':    ['Laundry — small load', 'Laundry — same-day', 'Pressing'],
    'transfer':   ['Airport transfer — arrival', 'Airport transfer — departure'],
    'misc':       ['Miscellaneous'],
}


# ── Demo POS items (only created if no PosItems exist yet, so the   ─────────
# import-pos-items workflow isn't shadowed once the real menu lands).
_DEMO_POS_ITEMS = (
    # (category_name, item_name, price_mvr, item_type)
    ('Hot Beverages',       '[Demo] Espresso',                35.0, 'restaurant'),
    ('Hot Beverages',       '[Demo] Cappuccino',              55.0, 'restaurant'),
    ('Hot Beverages',       '[Demo] Maldivian Black Tea',     30.0, 'restaurant'),
    ('Cold Coffee & Shakes','[Demo] Iced Latte',              65.0, 'restaurant'),
    ('Juices & Smoothies',  '[Demo] Fresh Mango Juice',       70.0, 'restaurant'),
    ('Sodas & Water',       '[Demo] Bottled Water 500ml',     20.0, 'restaurant'),
    ('Soups & Starters',    '[Demo] Garden Soup',             95.0, 'restaurant'),
    ('Chicken Mains',       '[Demo] Grilled Chicken Plate',  220.0, 'restaurant'),
    ('Fish & Seafood',      '[Demo] Reef Fish Curry',        265.0, 'restaurant'),
    ('Desserts',            '[Demo] Coconut Pudding',         85.0, 'restaurant'),
)


def _ref(prefix: str, n: int) -> str:
    return f'{prefix}-{n:04d}'


# ── Wipe / seed orchestration ──────────────────────────────────────────────

def _wipe_demo_data():
    """Delete bookings, folios, payments, orders, guests, h/k logs.

    Preserves: rooms, room types, POS categories + items (the menu),
    users, property settings, activity log, expense / bank rows.
    Order matters because of FK dependencies.
    """
    from ..models import (
        db, GuestOrderItem, GuestOrder, CashierTransaction, FolioItem,
        Invoice, Booking, Guest, HousekeepingLog,
    )
    deleted = {}
    for model in (GuestOrderItem, GuestOrder, CashierTransaction,
                  FolioItem, Invoice, Booking, HousekeepingLog, Guest):
        n = model.query.delete()
        deleted[model.__tablename__] = n
    db.session.commit()
    return deleted


def _seed_guests():
    """Create the guest pool, return list ordered as in _GUESTS."""
    from ..models import db, Guest

    guests = []
    for first, last, phone, country, email in _GUESTS:
        g = Guest(
            first_name=first, last_name=last,
            phone=phone, email=email, nationality=country,
        )
        db.session.add(g)
        guests.append(g)
    db.session.flush()
    return guests


def _ensure_demo_pos_items():
    """If no PosItems exist yet, seed a small set of [Demo]-prefixed
    items so the POS-orders scenarios have something to charge.
    Once the real menu lands via `import-pos-items`, the operator can
    deactivate or delete these demo rows easily by name prefix."""
    from ..models import db, PosCategory, PosItem
    if PosItem.query.count() > 0:
        return  # menu already seeded — don't double-up

    cat_by_name = {c.name: c for c in PosCategory.query.all()}
    for cat_name, name, price, item_type in _DEMO_POS_ITEMS:
        cat = cat_by_name.get(cat_name)
        if cat is None:
            continue
        db.session.add(PosItem(
            category_id=cat.id, name=name, price=price,
            default_item_type=item_type, is_active=True, sort_order=999,
        ))
    db.session.flush()


def _seed_bookings_and_folios(guests, today):
    """Materialize every scenario into Booking + Invoice + FolioItem
    + CashierTransaction rows. Returns the list of created booking
    rows for downstream callers (housekeeping painter, POS orders)."""
    from ..models import (
        db, Booking, Invoice, FolioItem, CashierTransaction, Room, User,
    )

    rooms_by_number = {r.number: r for r in Room.query.all()}
    admin = User.query.filter_by(role='admin').first()

    bookings = []
    rng = random.Random(20260430)

    for idx, sc in enumerate(_SCENARIOS):
        room = rooms_by_number.get(sc.room_number)
        if room is None:
            continue
        guest = guests[idx % len(guests)]

        check_in  = today + timedelta(days=sc.check_in_offset)
        check_out = check_in + timedelta(days=sc.nights)
        nightly   = float(room.price_per_night)
        subtotal  = nightly * sc.nights

        # actual_check_in stamped at lunchtime on the original day for
        # in-house and checked-out scenarios.
        act_in = None
        if sc.actual_check_in:
            act_in = datetime.combine(check_in, datetime.min.time()) + timedelta(hours=14)
        act_out = None
        if sc.booking_status == 'checked_out':
            act_out = datetime.combine(check_out, datetime.min.time()) + timedelta(hours=11)

        b = Booking(
            booking_ref=_ref('STG', idx + 1),
            room_id=room.id, guest_id=guest.id,
            check_in_date=check_in, check_out_date=check_out,
            actual_check_in=act_in, actual_check_out=act_out,
            num_guests=min(room.capacity, max(1, sc.nights // 3 or 1)),
            status=sc.booking_status,
            total_amount=subtotal,
            source=rng.choice(_SOURCES),
            billing_target='guest',
            created_by=admin.id if admin else None,
            special_requests=None,
        )
        db.session.add(b)
        db.session.flush()

        # Cancelled bookings get NO folio / invoice — the room becomes
        # available again. Pending-payment bookings get an invoice
        # (unpaid) but no folio activity yet.
        if sc.booking_status == 'cancelled':
            bookings.append(b)
            continue

        inv = Invoice(
            invoice_number=_ref('INV', idx + 1),
            booking_id=b.id,
            issue_date=check_in,
            subtotal=subtotal,
            total_amount=subtotal,
            payment_status='unpaid',
            amount_paid=0.0,
            invoice_to=guest.full_name,
        )
        db.session.add(inv)
        db.session.flush()

        # Room-charge folio items — one per night.
        for n in range(sc.nights):
            night = check_in + timedelta(days=n)
            db.session.add(FolioItem(
                booking_id=b.id, guest_id=guest.id, invoice_id=inv.id,
                item_type='room_charge', source_module='manual',
                description=f'Room {room.number} — night of {night.isoformat()}',
                quantity=1.0, unit_price=nightly,
                amount=nightly, total_amount=nightly,
                status='open',
                posted_by_user_id=admin.id if admin else None,
                created_at=datetime.combine(night, datetime.min.time()) + timedelta(hours=23),
            ))

        # Extra folio items (restaurant / laundry / transfer).
        for kind in sc.folio_extras:
            price = rng.choice(_EXTRA_PRICES[kind])
            desc  = rng.choice(_EXTRA_DESCRIPTIONS[kind])
            db.session.add(FolioItem(
                booking_id=b.id, guest_id=guest.id, invoice_id=inv.id,
                item_type=kind, source_module='manual',
                description=desc,
                quantity=1.0, unit_price=price,
                amount=price, total_amount=price,
                status='open',
                posted_by_user_id=admin.id if admin else None,
                created_at=act_in or datetime.combine(check_in, datetime.min.time()),
            ))
            subtotal += price

        # Optional discount — a negative-amount adjustment row.
        if sc.add_discount:
            disc = -150.0
            db.session.add(FolioItem(
                booking_id=b.id, guest_id=guest.id, invoice_id=inv.id,
                item_type='adjustment', source_module='manual',
                description='Loyalty discount — repeat guest',
                quantity=1.0, unit_price=disc,
                amount=disc, total_amount=disc,
                status='open',
                posted_by_user_id=admin.id if admin else None,
            ))
            subtotal += disc

        # Optional voided folio item.
        if sc.add_void:
            db.session.add(FolioItem(
                booking_id=b.id, guest_id=guest.id, invoice_id=inv.id,
                item_type='restaurant', source_module='manual',
                description='Cocktail — posted to wrong room',
                quantity=1.0, unit_price=140.0,
                amount=140.0, total_amount=140.0,
                status='voided',
                voided_at=datetime.utcnow() - timedelta(hours=6),
                voided_by_user_id=admin.id if admin else None,
                void_reason='Posted to wrong room — corrected',
                posted_by_user_id=admin.id if admin else None,
            ))
            # Voided items don't add to subtotal.

        inv.subtotal = subtotal
        inv.total_amount = subtotal

        # Payments — CashierTransactions tied to the invoice.
        if sc.payment_state == 'paid':
            paid = subtotal
        elif sc.payment_state == 'partial':
            paid = round(subtotal * 0.5, 2)
        else:
            paid = 0.0

        if paid > 0:
            db.session.add(CashierTransaction(
                booking_id=b.id, guest_id=guest.id, invoice_id=inv.id,
                amount=paid, currency='MVR',
                payment_method=rng.choice(['cash', 'card', 'bank_transfer']),
                received_by_user_id=admin.id if admin else None,
                transaction_type='payment',
                status='posted',
                reference_number=_ref('PMT', idx + 1),
                created_at=act_in or datetime.combine(check_in, datetime.min.time()),
            ))
            inv.amount_paid = paid

        # Settle invoice.payment_status.
        if paid <= 0:
            inv.payment_status = 'unpaid'
        elif paid + 0.001 < subtotal:
            inv.payment_status = 'partial'
        else:
            inv.payment_status = 'paid'

        bookings.append(b)

    db.session.commit()
    return bookings


def _paint_housekeeping(today):
    """Set Room.status + Room.housekeeping_status based on each room's
    current booking situation. Also stamps two rooms as out-of-order
    for realism.

    The values used:
      Room.status            : available | occupied | maintenance | cleaning
      Room.housekeeping_status: clean | dirty | in_progress | inspected | out_of_order
    """
    from ..models import db, Room, Booking, HousekeepingLog, User
    admin = User.query.filter_by(role='admin').first()

    rooms = Room.query.all()
    rng = random.Random(20260430 + 1)

    # Build a quick map: room_number -> active scenario (if any).
    active = {}  # number -> Booking
    today_bookings = Booking.query.all()
    for b in today_bookings:
        if b.status == 'cancelled':
            continue
        if b.check_in_date <= today < b.check_out_date and b.status == 'checked_in':
            active[b.room.number] = ('occupied_now', b)
        elif b.status == 'checked_in' and b.check_out_date == today:
            # Departure-today rooms — guest still in room until checkout
            active.setdefault(b.room.number, ('departing_today', b))
        elif b.status == 'checked_out' and (today - b.check_out_date).days <= 3:
            active.setdefault(b.room.number, ('recently_vacated', b))

    out_of_order_numbers = {n for n, _ in _OUT_OF_ORDER}

    for r in rooms:
        if r.number in out_of_order_numbers:
            r.status = 'maintenance'
            r.housekeeping_status = 'out_of_order'
            continue

        situation = active.get(r.number, ('vacant', None))[0]
        if situation == 'occupied_now' or situation == 'departing_today':
            r.status = 'occupied'
            # Most occupied rooms are clean (turned over this morning);
            # ~20% are dirty (need turndown), ~10% in_progress.
            roll = rng.random()
            if roll < 0.10:
                r.housekeeping_status = 'in_progress'
            elif roll < 0.30:
                r.housekeeping_status = 'dirty'
            else:
                r.housekeeping_status = 'clean'
        elif situation == 'recently_vacated':
            r.status = 'available'
            # Recently-vacated rooms are mostly dirty (await cleaning),
            # some already cleaned + inspected.
            roll = rng.random()
            if roll < 0.55:
                r.housekeeping_status = 'dirty'
            elif roll < 0.85:
                r.housekeeping_status = 'clean'
            else:
                r.housekeeping_status = 'inspected'
        else:
            # Vacant + no recent guest. Mostly clean, a few inspected
            # (pre-arrival ready), an occasional dirty (passed-through
            # housekeeping but not yet inspected).
            r.status = 'available'
            roll = rng.random()
            if roll < 0.15:
                r.housekeeping_status = 'inspected'
            elif roll < 0.95:
                r.housekeeping_status = 'clean'
            else:
                r.housekeeping_status = 'dirty'

    # Housekeeping log entries for the OOO rooms.
    for number, reason in _OUT_OF_ORDER:
        room = next((rr for rr in rooms if rr.number == number), None)
        if room is None:
            continue
        db.session.add(HousekeepingLog(
            room_id=room.id,
            staff_id=admin.id if admin else None,
            action='maintenance_request',
            notes=reason,
        ))

    db.session.commit()
    return out_of_order_numbers


def _seed_pos_orders(today):
    """Create ~9 GuestOrders covering new / confirmed / delivered /
    posted-to-folio / cancelled status values."""
    from ..models import (
        db, GuestOrder, GuestOrderItem, PosItem, Booking, FolioItem, User,
    )
    import secrets

    admin = User.query.filter_by(role='admin').first()
    pos_items = PosItem.query.filter(PosItem.is_active.is_(True)).all()
    if not pos_items:
        return 0  # no items yet → no orders

    # Pick a few in-house bookings to attach orders to.
    in_house = [b for b in Booking.query.filter_by(status='checked_in').all()
                if b.check_in_date <= today < b.check_out_date]
    if not in_house:
        return 0

    rng = random.Random(20260430 + 2)
    rng.shuffle(pos_items)

    plan = [
        # (status, with_booking, post_to_folio, cancelled)
        ('new',       True,  False, False),
        ('new',       True,  False, False),
        ('confirmed', True,  False, False),
        ('confirmed', True,  False, False),
        ('delivered', True,  True,  False),  # delivered + posted to folio
        ('delivered', True,  True,  False),
        ('delivered', False, False, False),  # delivered, walk-in, not posted
        ('delivered', True,  False, False),  # delivered, attached, not posted
        ('cancelled', True,  False, True),
    ]

    created = 0
    for spec_idx, (status, with_booking, post_to_folio, cancelled) in enumerate(plan):
        booking = in_house[spec_idx % len(in_house)] if with_booking else None
        # 1-3 line items per order
        line_count = rng.randint(1, 3)
        order_items = []
        total = 0.0
        for li in range(line_count):
            it = pos_items[(spec_idx * 3 + li) % len(pos_items)]
            qty = rng.choice([1, 1, 1, 2])
            line_total = it.price * qty
            order_items.append((it, qty, line_total))
            total += line_total

        order = GuestOrder(
            public_token=secrets.token_urlsafe(16)[:22],
            booking_id=booking.id if booking else None,
            room_number_input=booking.room.number if booking else None,
            guest_name_input=(booking.guest.full_name if booking
                              else 'Walk-in guest'),
            contact_phone=(booking.guest.phone if booking else None),
            status=status,
            total_amount=total,
            source='guest_menu',
        )
        if status in ('confirmed', 'delivered', 'cancelled'):
            order.confirmed_at = datetime.utcnow() - timedelta(hours=4)
            order.confirmed_by_user_id = admin.id if admin else None
        if status == 'delivered':
            order.delivered_at = datetime.utcnow() - timedelta(hours=2)
            order.delivered_by_user_id = admin.id if admin else None
        if cancelled:
            order.cancelled_at = datetime.utcnow() - timedelta(hours=1)
            order.cancelled_by_user_id = admin.id if admin else None
            order.cancel_reason = 'Guest changed mind'
        db.session.add(order)
        db.session.flush()

        for it, qty, line_total in order_items:
            db.session.add(GuestOrderItem(
                order_id=order.id, pos_item_id=it.id,
                item_name_snapshot=it.name,
                item_type_snapshot=it.default_item_type or 'restaurant',
                unit_price=it.price, quantity=qty, line_total=line_total,
            ))

        # Post to folio for the "delivered + posted" plan.
        if post_to_folio and booking is not None:
            folio_ids = []
            for it, qty, line_total in order_items:
                fi = FolioItem(
                    booking_id=booking.id, guest_id=booking.guest_id,
                    invoice_id=None,
                    item_type='restaurant', source_module='menu',
                    description=f'POS order #{order.id} — {it.name}',
                    quantity=qty, unit_price=it.price,
                    amount=line_total, total_amount=line_total,
                    status='open',
                    posted_by_user_id=admin.id if admin else None,
                )
                db.session.add(fi)
                db.session.flush()
                folio_ids.append(str(fi.id))
            order.posted_to_folio_at = datetime.utcnow()
            order.posted_by_user_id = admin.id if admin else None
            order.folio_item_ids = ','.join(folio_ids)

        created += 1
    db.session.commit()
    return created


# ── Public API ──────────────────────────────────────────────────────────────

def run(*, clean: bool = True) -> dict:
    """Run the full scenario seeder. Returns a dict of counts.

    Caller is responsible for the STAGING=1 guard. This function
    itself has no env check — it's library code.
    """
    today = date.today()

    deleted = {}
    if clean:
        deleted = _wipe_demo_data()

    guests = _seed_guests()
    _ensure_demo_pos_items()
    bookings = _seed_bookings_and_folios(guests, today)
    out_of_order = _paint_housekeeping(today)
    pos_order_count = _seed_pos_orders(today)

    # Build a small summary that the CLI can echo.
    from ..models import (
        Booking, Room, Invoice, GuestOrder, FolioItem,
    )

    summary = {
        'today':                   today.isoformat(),
        'wiped':                   deleted,
        'guests_created':          len(guests),
        'bookings_total':          len(bookings),
        'bookings_by_status':      {},
        'arrivals_today':          0,
        'departures_today':        0,
        'in_house_now':            0,
        'rooms_total':             Room.query.count(),
        'rooms_dirty':             Room.query.filter_by(housekeeping_status='dirty').count(),
        'rooms_inspected':         Room.query.filter_by(housekeeping_status='inspected').count(),
        'rooms_out_of_order':      Room.query.filter_by(housekeeping_status='out_of_order').count(),
        'invoices_paid':           Invoice.query.filter_by(payment_status='paid').count(),
        'invoices_partial':        Invoice.query.filter_by(payment_status='partial').count(),
        'invoices_unpaid':         Invoice.query.filter_by(payment_status='unpaid').count(),
        'folio_items_total':       FolioItem.query.count(),
        'folio_items_voided':      FolioItem.query.filter_by(status='voided').count(),
        'pos_orders_created':      pos_order_count,
        'pos_orders_total':        GuestOrder.query.count(),
    }

    # bookings_by_status, arrivals_today, departures_today, in_house_now
    for b in Booking.query.all():
        summary['bookings_by_status'][b.status] = (
            summary['bookings_by_status'].get(b.status, 0) + 1
        )
        if b.check_in_date == today and b.status == 'confirmed':
            summary['arrivals_today'] += 1
        if b.status == 'checked_in' and b.check_out_date == today:
            summary['departures_today'] += 1
        if b.status == 'checked_in' and b.check_in_date <= today < b.check_out_date:
            summary['in_house_now'] += 1

    return summary
