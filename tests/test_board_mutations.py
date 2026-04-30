"""Tests for the Reservation Board mutation services.

Covers the three operations exposed by services.board_mutations:
  - apply_booking_room_move  (Phase B)
  - apply_stay_update        (Phase C)
  - split_stay               (Phase D foundation)

Also smoke-tests the JSON endpoints at /board/bookings/<id>/move,
/resize, /split that wrap each service.

These tests pin:
  - happy-path success
  - conflict rejection (overlap with another booking on the target)
  - block-overlap rejection
  - cancelled-booking rejection
  - no-op rejection (same room / same dates)
  - ActivityLog entry written
  - StaySegment rows created and folio remains attached to one booking
  - external secrets / network calls — none (this module touches DB only)
"""

from __future__ import annotations

import os
import unittest
from datetime import date, timedelta

for _v in ('DATABASE_URL', 'AI_DRAFT_PROVIDER', 'AI_DRAFT_MODEL',
           'GEMINI_API_KEY', 'ANTHROPIC_API_KEY',
           'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                            # noqa: E402
from app import create_app                                           # noqa: E402
from app.models import (                                             # noqa: E402
    db, User, Guest, Room, Booking, Invoice, FolioItem,
    ActivityLog, RoomBlock, StaySegment,
)


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


# Today anchor used everywhere so tests don't drift on different days.
TODAY = date(2026, 5, 1)


def _seed_basics(app):
    """Seed a minimal admin + 3 rooms + 1 guest. Returns (admin, rooms, guest)."""
    with app.app_context():
        db.create_all()
        admin = User(username='admin', email='a@x', role='admin')
        admin.set_password('aaaaaaaaaa1')
        db.session.add(admin)
        db.session.flush()

        rooms = []
        for i, (num, type_, cap, price) in enumerate([
            ('101', 'Standard', 2, 800),
            ('102', 'Standard', 2, 800),
            ('103', 'Deluxe',   2, 1200),
        ]):
            r = Room(number=num, name=f'{type_} Room', room_type=type_,
                    floor=1, capacity=cap, price_per_night=price,
                    is_active=True)
            db.session.add(r)
            rooms.append(r)

        guest = Guest(first_name='Test', last_name='Guest', phone='+960 000')
        db.session.add(guest)
        db.session.commit()
        return admin.id, [r.id for r in rooms], guest.id


def _make_booking(*, room_id, guest_id, days_offset=0, nights=3,
                  status='confirmed', actor_user_id=None,
                  ref='BK-1') -> int:
    """Insert one booking + invoice + per-night folio rows. Returns booking.id."""
    check_in  = TODAY + timedelta(days=days_offset)
    check_out = check_in + timedelta(days=nights)
    b = Booking(
        booking_ref=ref, room_id=room_id, guest_id=guest_id,
        check_in_date=check_in, check_out_date=check_out,
        num_guests=2, status=status,
        total_amount=800.0 * nights,
        source='walk_in', billing_target='guest',
        created_by=actor_user_id,
    )
    db.session.add(b)
    db.session.flush()

    inv = Invoice(invoice_number=f'INV-{ref}', booking_id=b.id,
                  issue_date=check_in,
                  subtotal=800.0 * nights, total_amount=800.0 * nights,
                  amount_paid=0.0, payment_status='unpaid',
                  invoice_to='Test Guest')
    db.session.add(inv)
    db.session.flush()

    for n in range(nights):
        db.session.add(FolioItem(
            booking_id=b.id, guest_id=guest_id, invoice_id=inv.id,
            item_type='room_charge', source_module='manual',
            description=f'Night {n+1}',
            quantity=1.0, unit_price=800.0,
            amount=800.0, total_amount=800.0,
            status='open',
        ))
    db.session.commit()
    return b.id


# ── apply_booking_room_move ───────────────────────────────────────

class ApplyBookingRoomMoveTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.admin_id, self.room_ids, self.guest_id = _seed_basics(self.app)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_happy_path_moves_room_and_logs_activity(self):
        from app.services.board_mutations import apply_booking_room_move
        bid = _make_booking(room_id=self.room_ids[0], guest_id=self.guest_id)

        result = apply_booking_room_move(
            booking_id=bid, target_room_id=self.room_ids[1],
            actor_user_id=self.admin_id, note='AC noisy in 101',
        )
        self.assertTrue(result.ok, result.message)

        b = Booking.query.get(bid)
        self.assertEqual(b.room_id, self.room_ids[1])

        log = (ActivityLog.query
               .filter_by(action='booking.room_moved', booking_id=bid)
               .first())
        self.assertIsNotNone(log,
                             'ActivityLog row for booking.room_moved missing')
        self.assertIn('101', log.description)
        self.assertIn('102', log.description)

    def test_overlap_with_existing_booking_rejected(self):
        from app.services.board_mutations import apply_booking_room_move
        # Source booking on room 101
        src = _make_booking(room_id=self.room_ids[0],
                            guest_id=self.guest_id, ref='BK-1')
        # Conflict booking on room 102 spanning the same dates
        _make_booking(room_id=self.room_ids[1],
                      guest_id=self.guest_id, ref='BK-2',
                      days_offset=1, nights=4)

        result = apply_booking_room_move(
            booking_id=src, target_room_id=self.room_ids[1],
            actor_user_id=self.admin_id,
        )
        self.assertFalse(result.ok)
        self.assertIn('overlap', result.message.lower())
        # Source booking unchanged
        self.assertEqual(Booking.query.get(src).room_id, self.room_ids[0])

    def test_block_overlap_rejected(self):
        from app.services.board_mutations import apply_booking_room_move
        src = _make_booking(room_id=self.room_ids[0],
                            guest_id=self.guest_id)
        # Active block on target room
        blk = RoomBlock(
            room_id=self.room_ids[1],
            start_date=TODAY,
            end_date=TODAY + timedelta(days=10),
            reason='maintenance', notes='leaking pipe',
            created_by_user_id=self.admin_id,
        )
        db.session.add(blk)
        db.session.commit()

        result = apply_booking_room_move(
            booking_id=src, target_room_id=self.room_ids[1],
            actor_user_id=self.admin_id,
        )
        self.assertFalse(result.ok)
        self.assertIn('block', result.message.lower())

    def test_cancelled_booking_rejected(self):
        from app.services.board_mutations import apply_booking_room_move
        bid = _make_booking(room_id=self.room_ids[0],
                            guest_id=self.guest_id, status='cancelled')

        result = apply_booking_room_move(
            booking_id=bid, target_room_id=self.room_ids[1],
            actor_user_id=self.admin_id,
        )
        self.assertFalse(result.ok)
        self.assertIn('cancelled', result.message.lower())

    def test_same_room_rejected(self):
        from app.services.board_mutations import apply_booking_room_move
        bid = _make_booking(room_id=self.room_ids[0],
                            guest_id=self.guest_id)
        result = apply_booking_room_move(
            booking_id=bid, target_room_id=self.room_ids[0],
            actor_user_id=self.admin_id,
        )
        self.assertFalse(result.ok)


# ── apply_stay_update ─────────────────────────────────────────────

class ApplyStayUpdateTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.admin_id, self.room_ids, self.guest_id = _seed_basics(self.app)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_extend_stay_succeeds(self):
        from app.services.board_mutations import apply_stay_update
        bid = _make_booking(room_id=self.room_ids[0],
                            guest_id=self.guest_id, nights=3)

        new_out = TODAY + timedelta(days=5)  # was +3
        result = apply_stay_update(
            booking_id=bid, new_check_out=new_out,
            actor_user_id=self.admin_id,
        )
        self.assertTrue(result.ok, result.message)
        self.assertEqual(Booking.query.get(bid).check_out_date, new_out)

        log = (ActivityLog.query
               .filter_by(action='booking.stay_updated', booking_id=bid)
               .first())
        self.assertIsNotNone(log)

    def test_shorten_stay_succeeds(self):
        from app.services.board_mutations import apply_stay_update
        bid = _make_booking(room_id=self.room_ids[0],
                            guest_id=self.guest_id, nights=5)

        new_out = TODAY + timedelta(days=2)  # was +5
        result = apply_stay_update(
            booking_id=bid, new_check_out=new_out,
            actor_user_id=self.admin_id,
        )
        self.assertTrue(result.ok)
        self.assertEqual(Booking.query.get(bid).check_out_date, new_out)

    def test_no_change_rejected(self):
        from app.services.board_mutations import apply_stay_update
        bid = _make_booking(room_id=self.room_ids[0],
                            guest_id=self.guest_id, nights=3)
        b = Booking.query.get(bid)
        result = apply_stay_update(
            booking_id=bid, new_check_out=b.check_out_date,
            actor_user_id=self.admin_id,
        )
        self.assertFalse(result.ok)
        self.assertIn('no change', result.message.lower())

    def test_extend_past_overlap_rejected(self):
        from app.services.board_mutations import apply_stay_update
        # Booking A on room 101: today → today+3
        a = _make_booking(room_id=self.room_ids[0],
                          guest_id=self.guest_id, nights=3, ref='BK-A')
        # Booking B on same room: today+4 → today+7  (next stay)
        _make_booking(room_id=self.room_ids[0],
                      guest_id=self.guest_id, nights=3,
                      days_offset=4, ref='BK-B')

        # Try to extend A to overlap B
        result = apply_stay_update(
            booking_id=a,
            new_check_out=TODAY + timedelta(days=6),
            actor_user_id=self.admin_id,
        )
        self.assertFalse(result.ok)
        self.assertIn('overlap', result.message.lower())


# ── split_stay (Phase D foundation) ───────────────────────────────

class SplitStayTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.admin_id, self.room_ids, self.guest_id = _seed_basics(self.app)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_split_creates_two_segments_and_keeps_folio_attached(self):
        from app.services.board_mutations import split_stay
        bid = _make_booking(room_id=self.room_ids[0],
                            guest_id=self.guest_id, nights=5)

        split_date = TODAY + timedelta(days=2)
        result = split_stay(
            booking_id=bid, split_date=split_date,
            target_room_id=self.room_ids[1],
            actor_user_id=self.admin_id, note='guest requested view',
        )
        self.assertTrue(result.ok, result.message)

        b = Booking.query.get(bid)
        segments = list(b.stay_segments)
        self.assertEqual(len(segments), 2,
                         'split_stay must create exactly 2 segments')
        self.assertEqual(segments[0].room_id, self.room_ids[0])
        self.assertEqual(segments[1].room_id, self.room_ids[1])
        self.assertEqual(segments[0].end_date, split_date)
        self.assertEqual(segments[1].start_date, split_date)

        # Folio rows still attached to the same booking — segments
        # don't fragment the bill.
        folio = FolioItem.query.filter_by(booking_id=bid).all()
        self.assertEqual(len(folio), 5,
                         'all folio items must remain on the original booking')
        for fi in folio:
            self.assertEqual(fi.booking_id, bid)

        # Booking itself unchanged in dates / room_id (foundation
        # only — board still renders by booking.room_id).
        self.assertEqual(b.room_id, self.room_ids[0])

        log = (ActivityLog.query
               .filter_by(action='booking.stay_split', booking_id=bid)
               .first())
        self.assertIsNotNone(log)

    def test_split_at_check_in_rejected(self):
        from app.services.board_mutations import split_stay
        bid = _make_booking(room_id=self.room_ids[0],
                            guest_id=self.guest_id, nights=4)
        b = Booking.query.get(bid)
        result = split_stay(booking_id=bid, split_date=b.check_in_date,
                            target_room_id=self.room_ids[1])
        self.assertFalse(result.ok)
        self.assertIn('between', result.message.lower())

    def test_split_target_room_overlap_rejected(self):
        from app.services.board_mutations import split_stay
        # Source booking on room 101 for 5 nights
        src = _make_booking(room_id=self.room_ids[0],
                            guest_id=self.guest_id, nights=5, ref='BK-A')
        # Conflicting booking on room 102 starting day 3 for 3 nights
        _make_booking(room_id=self.room_ids[1],
                      guest_id=self.guest_id, nights=3,
                      days_offset=3, ref='BK-B')

        # Try to split src at day 2 → move to room 102 day 2-5
        result = split_stay(
            booking_id=src,
            split_date=TODAY + timedelta(days=2),
            target_room_id=self.room_ids[1],
            actor_user_id=self.admin_id,
        )
        self.assertFalse(result.ok)
        self.assertIn('overlap', result.message.lower())

    def test_re_split_blocked_in_v1(self):
        from app.services.board_mutations import split_stay
        bid = _make_booking(room_id=self.room_ids[0],
                            guest_id=self.guest_id, nights=6)
        # First split — succeeds
        ok = split_stay(
            booking_id=bid, split_date=TODAY + timedelta(days=2),
            target_room_id=self.room_ids[1], actor_user_id=self.admin_id,
        )
        self.assertTrue(ok.ok)
        # Second split — refused (V1 doesn't support re-segmenting)
        again = split_stay(
            booking_id=bid, split_date=TODAY + timedelta(days=4),
            target_room_id=self.room_ids[2], actor_user_id=self.admin_id,
        )
        self.assertFalse(again.ok)
        self.assertIn('not supported', again.message.lower())


# ── Endpoint smoke tests (admin-only, JSON) ───────────────────────

class BoardMutationEndpointTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.admin_id, self.room_ids, self.guest_id = _seed_basics(self.app)
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.admin_id)
            sess['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_move_endpoint_happy_path(self):
        bid = _make_booking(room_id=self.room_ids[0],
                            guest_id=self.guest_id)
        r = self.client.post(
            f'/board/bookings/{bid}/move',
            json={'target_room_id': self.room_ids[1]},
        )
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(Booking.query.get(bid).room_id, self.room_ids[1])

    def test_move_endpoint_rejects_overlap_with_400(self):
        a = _make_booking(room_id=self.room_ids[0],
                          guest_id=self.guest_id, ref='BK-A')
        _make_booking(room_id=self.room_ids[1],
                      guest_id=self.guest_id, ref='BK-B',
                      days_offset=1, nights=4)
        r = self.client.post(
            f'/board/bookings/{a}/move',
            json={'target_room_id': self.room_ids[1]},
        )
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()['ok'])

    def test_resize_endpoint_happy_path(self):
        bid = _make_booking(room_id=self.room_ids[0],
                            guest_id=self.guest_id, nights=3)
        new_out = (TODAY + timedelta(days=5)).isoformat()
        r = self.client.post(
            f'/board/bookings/{bid}/resize',
            json={'check_out_date': new_out},
        )
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['ok'])

    def test_split_endpoint_happy_path(self):
        bid = _make_booking(room_id=self.room_ids[0],
                            guest_id=self.guest_id, nights=5)
        split_iso = (TODAY + timedelta(days=2)).isoformat()
        r = self.client.post(
            f'/board/bookings/{bid}/split',
            json={'split_date': split_iso,
                  'target_room_id': self.room_ids[1]},
        )
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(StaySegment.query.filter_by(booking_id=bid).count(),
                         2)

    def test_endpoints_require_admin(self):
        # Create a non-admin user and try the move endpoint
        staff = User(username='staff', email='s@x', role='staff')
        staff.set_password('aaaaaaaaaa1')
        db.session.add(staff)
        db.session.commit()
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(staff.id)
            sess['_fresh'] = True
        bid = _make_booking(room_id=self.room_ids[0],
                            guest_id=self.guest_id)
        r = client.post(f'/board/bookings/{bid}/move',
                        json={'target_room_id': self.room_ids[1]})
        # The staff_guard whitelist redirects non-admin staff hitting
        # /board/* off to /staff/dashboard; the @admin_required
        # decorator returns 403/302. Either rejection is acceptable
        # — we just need it NOT to be a 200.
        self.assertNotEqual(r.status_code, 200)


if __name__ == '__main__':
    unittest.main()
