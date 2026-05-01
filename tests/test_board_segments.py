"""Tests for segment-aware Reservation Board rendering (V1).

When a Booking has stay_segments rows, the board must render ONE BAR
PER SEGMENT on each segment's room row instead of a single bar on
booking.room_id. The booking, folio, guest, and payments stay
attached to the single Booking row — segments are an additive
"where the guest sleeps tonight" overlay.

Pinned by these tests:
  - make_segment_bar() builds a BookingBar correctly from a segment
  - the route renders segment bars on the SEGMENT room rows
  - the drawer_data still keys by booking_id + carries a 'segments'
    summary list
  - folio rows remain attached to the booking after a split
  - re-running existing test_board_mutations still passes (folio
    invariant, ActivityLog invariant)
"""

from __future__ import annotations

import os
import re
import unittest
from datetime import date, timedelta

for _v in ('DATABASE_URL', 'AI_DRAFT_PROVIDER', 'AI_DRAFT_MODEL',
           'GEMINI_API_KEY', 'ANTHROPIC_API_KEY',
           'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                         # noqa: E402
from app import create_app                                        # noqa: E402
from app.models import (                                          # noqa: E402
    db, User, Guest, Room, Booking, Invoice, FolioItem, StaySegment,
)


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _seed_world(app):
    """Seed: 1 admin, 3 rooms, 1 guest, 1 booking with 5-night stay."""
    with app.app_context():
        db.create_all()
        admin = User(username='admin', email='a@x', role='admin')
        admin.set_password('aaaaaaaaaa1')
        db.session.add(admin)
        rooms = [
            Room(number='101', name='Standard Room', room_type='Standard',
                 floor=1, capacity=2, price_per_night=800.0, is_active=True),
            Room(number='102', name='Standard Room', room_type='Standard',
                 floor=1, capacity=2, price_per_night=800.0, is_active=True),
            Room(number='103', name='Deluxe Room',   room_type='Deluxe',
                 floor=1, capacity=2, price_per_night=1200.0, is_active=True),
        ]
        for r in rooms:
            db.session.add(r)
        g = Guest(first_name='Test', last_name='Guest', phone='+960 0')
        db.session.add(g)
        db.session.commit()
        # 5-night stay starting today on room 101
        today = date.today()
        b = Booking(
            booking_ref='BK-SEG', room_id=rooms[0].id, guest_id=g.id,
            check_in_date=today, check_out_date=today + timedelta(days=5),
            num_guests=2, status='confirmed',
            total_amount=4000.0, source='walk_in', billing_target='guest',
            created_by=admin.id,
        )
        db.session.add(b)
        db.session.flush()
        inv = Invoice(invoice_number='INV-SEG', booking_id=b.id,
                      issue_date=today, subtotal=4000.0, total_amount=4000.0,
                      amount_paid=0.0, payment_status='unpaid',
                      invoice_to='Test Guest')
        db.session.add(inv)
        for n in range(5):
            db.session.add(FolioItem(
                booking_id=b.id, guest_id=g.id, invoice_id=inv.id,
                item_type='room_charge', source_module='manual',
                description=f'Night {n+1}',
                quantity=1.0, unit_price=800.0,
                amount=800.0, total_amount=800.0,
                status='open',
            ))
        db.session.commit()
        return admin.id, [r.id for r in rooms], b.id


# ── make_segment_bar ──────────────────────────────────────────────

class MakeSegmentBarTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.admin_id, self.rooms, self.bid = _seed_world(self.app)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_segment_bar_uses_segment_dates_and_room(self):
        from app.services.board import make_segment_bar
        b = Booking.query.get(self.bid)
        # Build a manual segment for nights 0–2 on room 101 (the
        # split_stay service does the FK insert; we just need the
        # ORM object for the bar builder).
        seg = StaySegment(
            booking_id=b.id, room_id=self.rooms[0],
            start_date=b.check_in_date,
            end_date=b.check_in_date + timedelta(days=2),
        )
        window_start = b.check_in_date
        window_end = b.check_out_date
        bar = make_segment_bar(seg, b, window_start, window_end,
                               segment_index=0, segment_total=2)
        self.assertIsNotNone(bar)
        self.assertEqual(bar.booking_id, b.id)
        self.assertEqual(bar.check_in,  seg.start_date)
        self.assertEqual(bar.check_out, seg.end_date)
        self.assertEqual(bar.nights, 2)
        # Grid placement uses segment dates, not booking dates
        self.assertEqual(bar.grid_col_start, 2)  # day 0 → col 2
        self.assertEqual(bar.grid_col_span,  2)  # 2 nights
        # Multi-segment label gets the "1/2" decoration
        self.assertIn('1/2', bar.short_label)

    def test_segment_bar_skips_when_outside_window(self):
        from app.services.board import make_segment_bar
        b = Booking.query.get(self.bid)
        seg = StaySegment(
            booking_id=b.id, room_id=self.rooms[0],
            start_date=b.check_in_date,
            end_date=b.check_in_date + timedelta(days=2),
        )
        # Window starts AFTER segment end → should return None
        bar = make_segment_bar(
            seg, b,
            b.check_in_date + timedelta(days=10),
            b.check_in_date + timedelta(days=20),
        )
        self.assertIsNone(bar)

    def test_single_segment_label_drops_fraction(self):
        from app.services.board import make_segment_bar
        b = Booking.query.get(self.bid)
        seg = StaySegment(
            booking_id=b.id, room_id=self.rooms[0],
            start_date=b.check_in_date,
            end_date=b.check_out_date,
        )
        bar = make_segment_bar(seg, b, b.check_in_date, b.check_out_date,
                               segment_index=0, segment_total=1)
        self.assertNotIn('/', bar.short_label,
                         'single-segment bookings should not show "1/1"')


# ── Route renders per-segment bars ────────────────────────────────

class RouteSegmentRenderingTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.admin_id, self.rooms, self.bid = _seed_world(self.app)

        # Split the stay: nights 0-1 on room 101, nights 1-5 on
        # room 102. The split_stay service writes both segments in
        # a transaction.
        from app.services.board_mutations import split_stay
        b = Booking.query.get(self.bid)
        result = split_stay(
            booking_id=b.id,
            split_date=b.check_in_date + timedelta(days=2),
            target_room_id=self.rooms[1],
            actor_user_id=self.admin_id,
            note='guest moved to view room',
        )
        self.assertTrue(result.ok, result.message)

        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.admin_id)
            sess['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_board_renders_two_bars_for_segmented_booking(self):
        # Render the board for the stay window
        b = Booking.query.get(self.bid)
        start = b.check_in_date.isoformat()
        r = self.client.get(f'/board?view=14d&start={start}')
        self.assertEqual(r.status_code, 200)
        html = r.data
        # Both segment bars should render — count <a class="bar"> with
        # data-booking-id matching the split booking
        pat = (rb'<a\s+class="bar[^"]*"[^>]*'
               rb'data-booking-id="' + str(b.id).encode() + rb'"')
        bars_for_booking = len(re.findall(pat, html))
        self.assertEqual(
            bars_for_booking, 2,
            f'expected 2 segment bars for booking {b.id}, found {bars_for_booking}',
        )

    def test_segment_bars_carry_distinct_data_room_id(self):
        # Each segment bar's data-room-id must reflect the SEGMENT
        # room, not the booking.room_id (which stays the original).
        b = Booking.query.get(self.bid)
        start = b.check_in_date.isoformat()
        r = self.client.get(f'/board?view=14d&start={start}')
        self.assertEqual(r.status_code, 200)
        html = r.data.decode('utf-8')
        # Find every bar tag for our booking_id and pull its data-room-id
        rooms_on_bars = re.findall(
            rf'<a\s+class="bar[^"]*"[^>]*data-booking-id="{b.id}"[^>]*'
            rf'data-room-id="(\d+)"',
            html,
        )
        self.assertEqual(len(rooms_on_bars), 2)
        self.assertEqual(set(rooms_on_bars),
                         {str(self.rooms[0]), str(self.rooms[1])})

    def test_drawer_data_carries_segments_summary(self):
        # The inline drawer JSON should contain a 'segments' array
        # with one entry per segment.
        b = Booking.query.get(self.bid)
        start = b.check_in_date.isoformat()
        r = self.client.get(f'/board?view=14d&start={start}')
        self.assertEqual(r.status_code, 200)
        html = r.data.decode('utf-8')
        # Just look for the literal "segments" key — the drawer
        # serializer renders the list inline as a JSON object inside
        # the page's <script>.
        self.assertIn('"segments":', html)
        # Each segment summary names its room number
        self.assertIn('"roomNumber":', html)


# ── Folio invariant after split ───────────────────────────────────

class FolioInvariantTests(unittest.TestCase):
    """Splitting a stay must NOT fragment the folio. Every FolioItem
    stays attached to the original booking_id; payments + guest +
    history remain intact."""

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.admin_id, self.rooms, self.bid = _seed_world(self.app)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_folio_items_still_on_booking_after_split(self):
        from app.services.board_mutations import split_stay
        b = Booking.query.get(self.bid)
        before = FolioItem.query.filter_by(booking_id=b.id).count()
        self.assertEqual(before, 5)
        result = split_stay(
            booking_id=b.id,
            split_date=b.check_in_date + timedelta(days=2),
            target_room_id=self.rooms[1],
            actor_user_id=self.admin_id,
        )
        self.assertTrue(result.ok)
        after = FolioItem.query.filter_by(booking_id=b.id).count()
        self.assertEqual(after, before,
            'all folio rows must remain on the original booking')
        # Booking.room_id is unchanged — board still renders by
        # booking.room_id when no segment-aware code path triggers
        self.assertEqual(Booking.query.get(b.id).room_id, self.rooms[0])
        # Guest unchanged
        self.assertIsNotNone(Booking.query.get(b.id).guest)


if __name__ == '__main__':
    unittest.main()
