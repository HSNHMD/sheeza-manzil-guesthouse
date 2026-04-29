"""One-shot staging-only seed for Guest Folio demo data.

Posts a varied set of synthetic FolioItem rows against the existing
demo bookings (DEMO-0101 … DEMO-0606) so the Guest Folio panel on the
booking detail page has clickable content for QA.

Hard rules:
  - IDEMPOTENT: refuses to run if ANY folio_items rows already exist.
    Defends against double-seeding which would corrupt balances.
  - STAGING ONLY: relies on DEMO-* booking refs that only exist on
    the staging seed. Production bookings have BK* refs.
  - SYNTHETIC: every amount and description is fake. No real guest
    payment data is referenced.
  - AUDITED: every insert writes a folio.item.created activity row,
    same code path as the real /bookings/<id>/folio/items POST route.

Run:
  cd /var/www/guesthouse-staging
  set -a && source .env && set +a
  venv/bin/python scripts/seed_demo_folio.py

Or locally against a sqlite DB after `flask db upgrade`.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

# Ensure the repo root is importable when this script is invoked as
# `venv/bin/python scripts/seed_demo_folio.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.models import db, User, Booking, FolioItem, ActivityLog
from app.services.folio import signed_total
from app.services.audit import log_activity


# ── Plan: per-booking item recipes ──────────────────────────────────
# Each recipe is a list of (item_type, description, qty, unit_price,
# tax, sc, void_reason_or_None) tuples. Sign convention applied by
# signed_total() — payments / discounts auto-negate.
RECIPES = {
    'DEMO-0101': [
        ('room_charge',  'Room night 2026-04-19', 1, 600.0, 0, 0, None),
        ('room_charge',  'Room night 2026-04-20', 1, 600.0, 0, 0, None),
        ('room_charge',  'Room night 2026-04-21', 1, 600.0, 0, 0, None),
        ('restaurant',   'Welcome dinner — 2 mains', 1, 280.0, 14, 28, None),
        ('payment',      'Bank transfer · slip BK-DEMO-0101', 1, 1922.0, 0, 0, None),
    ],
    'DEMO-0202': [
        ('room_charge',  'Room night 2026-04-24', 1, 600.0, 0, 0, None),
        ('room_charge',  'Room night 2026-04-25', 1, 600.0, 0, 0, None),
        ('room_charge',  'Room night 2026-04-26', 1, 600.0, 0, 0, None),
        ('room_charge',  'Room night 2026-04-27', 1, 600.0, 0, 0, None),
        ('laundry',      '4 shirts · 2 pants',         1, 120.0, 6, 0, None),
        ('transfer',     'Speedboat HIA → Hanimaadhoo', 1, 800.0, 40, 0, None),
        ('restaurant',   'Lunch — 3 plates',            1, 320.0, 16, 32, None),
        ('payment',      'Cash on arrival · receipt 0212', 1, 2400.0, 0, 0, None),
    ],
    'DEMO-0303': [
        ('room_charge',  'Room night 2026-04-28', 1, 600.0, 0, 0, None),
        ('room_charge',  'Room night 2026-04-29', 1, 600.0, 0, 0, None),
        ('room_charge',  'Room night 2026-04-30', 1, 600.0, 0, 0, None),
        ('goods',        'Minibar · 4 bottled water + chocolate', 1, 95.0, 5, 0, None),
        ('laundry',      'Dry cleaning suit',           1, 180.0, 9, 0, None),
        ('fee',          'Late check-in · after 23:00', 1, 100.0, 0, 0, 'duplicate of comp policy'),  # voided
        ('payment',      'Bank transfer · slip BK-DEMO-0303', 1, 1800.0, 0, 0, None),
    ],
    'DEMO-0404': [
        ('room_charge',  'Room night 2026-05-01', 1, 600.0, 0, 0, None),
        ('room_charge',  'Room night 2026-05-02', 1, 600.0, 0, 0, None),
        ('discount',     'Promo · loyalty 10%',         1, 120.0, 0, 0, None),
    ],
    'DEMO-0505': [
        ('room_charge',  'Room night 2026-05-04', 1, 600.0, 0, 0, None),
        ('room_charge',  'Room night 2026-05-05', 1, 600.0, 0, 0, None),
        ('room_charge',  'Room night 2026-05-06', 1, 600.0, 0, 0, None),
        ('room_charge',  'Room night 2026-05-07', 1, 600.0, 0, 0, None),
        ('adjustment',   'Pre-paid deposit credit',     1, -500.0, 0, 0, None),
    ],
    'DEMO-0606': [
        # pending_payment: just an early-bird charge already posted
        ('fee',          'Reservation hold fee', 1, 50.0, 0, 0, None),
    ],
}


def _resolve_admin_user() -> int | None:
    """Return the id of any admin user. Returns None if there are no
    admins (in which case folio rows are seeded with NULL posted_by)."""
    u = User.query.filter_by(role='admin').order_by(User.id.asc()).first()
    return u.id if u else None


def _post_item(*, booking, recipe, posted_by_user_id):
    """Replicate the route handler's insert path so the audit + signed
    total math go through the SAME code as a real admin click."""
    item_type, description, qty, unit_price, tax, sc, void_reason = recipe

    # signed_total handles the sign convention (payments/discounts
    # auto-negate) — same helper the real route uses.
    amount = qty * unit_price
    total = signed_total(item_type, amount, tax, sc)

    item = FolioItem(
        booking_id=booking.id,
        guest_id=booking.guest_id,
        item_type=item_type,
        description=description,
        quantity=qty,
        unit_price=unit_price,
        amount=amount,
        tax_amount=tax,
        service_charge_amount=sc,
        total_amount=round(total, 2),
        status='open',
        source_module='manual',
        posted_by_user_id=posted_by_user_id,
    )
    db.session.add(item)
    db.session.flush()  # populate item.id

    log_activity(
        'folio.item.created',
        actor_type='system',  # seed script — not a live user
        booking=booking, invoice=getattr(booking, 'invoice', None),
        description=f'Folio item posted (seed): {item_type} · {description[:40]}',
        metadata={
            'booking_id':     booking.id,
            'booking_ref':    booking.booking_ref,
            'folio_item_id':  item.id,
            'item_type':      item_type,
            'source_module':  'manual',
            'amount':         item.total_amount,
            'status':         'open',
            'voided':         False,
        },
    )

    # If the recipe has a void_reason, void it immediately so the demo
    # has a voided row to demonstrate the void state in the UI.
    if void_reason:
        item.status = 'voided'
        item.voided_at = datetime.utcnow()
        item.voided_by_user_id = posted_by_user_id
        item.void_reason = void_reason
        log_activity(
            'folio.item.voided',
            actor_type='system',
            booking=booking, invoice=getattr(booking, 'invoice', None),
            description=f'Folio item voided (seed): {item_type}',
            metadata={
                'booking_id':     booking.id,
                'booking_ref':    booking.booking_ref,
                'folio_item_id':  item.id,
                'item_type':      item_type,
                'source_module':  'manual',
                'amount':         item.total_amount,
                'status':         'voided',
                'voided':         True,
            },
        )

    return item


def main() -> int:
    app = create_app()
    with app.app_context():
        if FolioItem.query.count() > 0:
            print('REFUSE: folio_items already populated — '
                  'seed script will not double-seed.')
            return 0

        admin_id = _resolve_admin_user()
        bookings_by_ref = {
            b.booking_ref: b for b in Booking.query.all()
            if b.booking_ref in RECIPES
        }
        if not bookings_by_ref:
            print('REFUSE: no DEMO-* bookings found. This script is '
                  'staging-only and depends on the staging demo seed.')
            return 1

        total_inserted = 0
        total_voided = 0
        for ref, recipes in RECIPES.items():
            booking = bookings_by_ref.get(ref)
            if booking is None:
                print(f'  · skipping {ref} (booking not present)')
                continue
            for recipe in recipes:
                item = _post_item(booking=booking, recipe=recipe,
                                  posted_by_user_id=admin_id)
                total_inserted += 1
                if item.status == 'voided':
                    total_voided += 1

        db.session.commit()
        print(f'OK: inserted {total_inserted} folio items '
              f'({total_voided} pre-voided for demo) '
              f'across {len(bookings_by_ref)} bookings.')
        # Quick balance summary so the operator sees what to expect
        for ref, b in sorted(bookings_by_ref.items()):
            from app.services.folio import folio_balance, calculate_folio_totals
            t = calculate_folio_totals(b)
            print(f'  {ref}: charges={t["total_charges"]:.0f} '
                  f'credits={t["total_credits"]:.0f} '
                  f'adj={t["total_adjustments"]:.0f} '
                  f'balance={t["balance"]:.0f}')
        return 0


if __name__ == '__main__':
    sys.exit(main())
