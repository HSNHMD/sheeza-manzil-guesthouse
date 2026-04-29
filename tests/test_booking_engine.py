"""Tests for Booking Engine V1.

Covers the 12 requirements from the build spec, section I:

  1. invalid date range rejected
  2. guest count validation
  3. no availability when rooms blocked / booked
  4. valid search returns correct room types
  5. stop-sell / restrictions respected
  6. booking creation success path
  7. overbooking prevented
  8. price calculation correct for simple cases
  9. ActivityLog entries created
 10. no WhatsApp / Gemini calls (asserted via patches)
 11. migration files only if needed (none added → assert)
 12. no production coupling (no env reads, no real outbound)

Plus service-level unit tests + a confirmation-render test.
"""

from __future__ import annotations

import json
import os
import re
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

for _v in ('DATABASE_URL',
           'AI_DRAFT_PROVIDER', 'GEMINI_API_KEY', 'ANTHROPIC_API_KEY',
           'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                       # noqa: E402
from app import create_app                                      # noqa: E402
from app.models import (                                        # noqa: E402
    db, User, Room, Guest, Booking, Invoice, ActivityLog,
    RoomType, RatePlan, RateOverride, RateRestriction, RoomBlock,
)
from app.services import booking_engine as be                   # noqa: E402
from app.services import whatsapp as wa                         # noqa: E402
from app.services import ai_drafts                              # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parent.parent
_TODAY     = date.today()
_TOMORROW  = _TODAY + timedelta(days=1)
_PLUS_2    = _TODAY + timedelta(days=2)
_PLUS_3    = _TODAY + timedelta(days=3)


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _make_app():
    return create_app(_TestConfig)


_seed_counter = {'n': 0}


def _seed_users():
    _seed_counter['n'] += 1
    n = _seed_counter['n']
    admin = User(username=f'be_admin_{n}', email=f'a{n}@x', role='admin')
    admin.set_password('aaaaaaaaaa1')
    db.session.add(admin); db.session.commit()
    return admin.id


def _seed_room_type(code='DEL', name='Deluxe', max_occ=2):
    rt = RoomType(code=code, name=name,
                  max_occupancy=max_occ, base_capacity=max_occ,
                  is_active=True)
    db.session.add(rt); db.session.commit()
    return rt


def _seed_rooms_of_type(rt, count=2, base_num=10, price=600.0):
    rooms = []
    for i in range(count):
        r = Room(
            number=str(base_num + i), name='T', room_type=rt.name,
            room_type_id=rt.id, floor=1, capacity=2,
            price_per_night=price, status='available',
            housekeeping_status='clean',
        )
        db.session.add(r); rooms.append(r)
    db.session.commit()
    return rooms


def _seed_plan(rt, code='BAR', base_rate=600.0, refundable=True):
    p = RatePlan(code=code, name=code, room_type_id=rt.id,
                 base_rate=base_rate, currency='USD',
                 is_refundable=refundable, is_active=True)
    db.session.add(p); db.session.commit()
    return p


def _seed_booking(room, check_in, check_out, status='confirmed', guest=None):
    if guest is None:
        guest = Guest(first_name='G', last_name='X',
                      phone='+9607000000', email='g@x')
        db.session.add(guest); db.session.commit()
    b = Booking(
        booking_ref=f'BK-{room.id}-{check_in.isoformat()}',
        room_id=room.id, guest_id=guest.id,
        check_in_date=check_in, check_out_date=check_out,
        num_guests=1, total_amount=0.0, status=status,
    )
    db.session.add(b); db.session.commit()
    return b


# ─────────────────────────────────────────────────────────────────────
# 1 + 2) Validation (Reqs 1, 2)
# ─────────────────────────────────────────────────────────────────────

class ValidationTests(unittest.TestCase):

    def test_check_out_before_check_in_rejected(self):
        msg = be.validate_search_input(_TOMORROW, _TODAY, 1)
        self.assertIn('check-out', msg or '')

    def test_same_day_rejected(self):
        msg = be.validate_search_input(_TODAY, _TODAY, 1)
        self.assertIsNotNone(msg)

    def test_past_check_in_rejected(self):
        past = _TODAY - timedelta(days=1)
        msg = be.validate_search_input(past, _TODAY, 1)
        self.assertIn('past', msg or '')

    def test_too_long_stay_rejected(self):
        msg = be.validate_search_input(_TODAY, _TODAY + timedelta(days=120), 1)
        self.assertIn('60', msg or '')

    def test_zero_guests_rejected(self):
        msg = be.validate_search_input(_TODAY, _TOMORROW, 0)
        self.assertIn('guest', msg or '')

    def test_too_many_guests_rejected(self):
        msg = be.validate_search_input(_TODAY, _TOMORROW, 50)
        self.assertIn('guest', msg or '')

    def test_non_numeric_guests_rejected(self):
        msg = be.validate_search_input(_TODAY, _TOMORROW, 'abc')
        self.assertIn('number', (msg or '').lower())

    def test_valid_input_accepted(self):
        self.assertIsNone(be.validate_search_input(_TODAY, _TOMORROW, 2))


# ─────────────────────────────────────────────────────────────────────
# Common base — patches WhatsApp + AI providers (Req 10)
# ─────────────────────────────────────────────────────────────────────

class _RouteBase(unittest.TestCase):

    def setUp(self):
        self._patches = []
        self._wa_send = mock.patch.object(
            wa, '_send', side_effect=AssertionError(
                'WhatsApp must not be called by Booking Engine V1'))
        self._patches.append(self._wa_send.start())
        self._wa_template = mock.patch.object(
            wa, '_send_template', side_effect=AssertionError(
                'WhatsApp templates must not be called by Booking Engine V1'))
        self._patches.append(self._wa_template.start())
        self._ai_patch = mock.patch.object(
            ai_drafts, 'generate_draft',
            side_effect=AssertionError(
                'AI drafts must not be called by Booking Engine V1'))
        self._patches.append(self._ai_patch.start())

        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.admin_id = _seed_users()
        self.client = self.app.test_client()

    def tearDown(self):
        for p in (self._wa_send, self._wa_template, self._ai_patch):
            p.stop()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()


# ─────────────────────────────────────────────────────────────────────
# 3 + 4 + 5) Search behavior (Reqs 3, 4, 5)
# ─────────────────────────────────────────────────────────────────────

class SearchTests(_RouteBase):

    def test_returns_correct_room_types(self):
        rt_d = _seed_room_type('DEL', 'Deluxe', 2)
        rt_t = _seed_room_type('TWI', 'Twin', 2)
        _seed_rooms_of_type(rt_d, count=2, base_num=10, price=600.0)
        _seed_rooms_of_type(rt_t, count=1, base_num=20, price=400.0)
        _seed_plan(rt_d, code='BAR_D', base_rate=600.0)
        _seed_plan(rt_t, code='BAR_T', base_rate=400.0)

        result = be.search_availability(_TODAY, _PLUS_2, 2)
        self.assertIsNone(result['error'])
        codes = {o.room_type_code for o in result['options']}
        self.assertEqual(codes, {'DEL', 'TWI'})
        self.assertEqual(result['nights'], 2)

    def test_filters_by_capacity(self):
        # 2-guest type should NOT surface for a 4-guest request
        rt = _seed_room_type('DEL', 'Deluxe', 2)
        _seed_rooms_of_type(rt, count=2)
        _seed_plan(rt)
        result = be.search_availability(_TODAY, _PLUS_2, 4)
        self.assertEqual([o.room_type_code for o in result['options']], [])

    def test_no_availability_when_all_booked(self):
        rt = _seed_room_type('DEL', 'Deluxe')
        rooms = _seed_rooms_of_type(rt, count=2)
        _seed_plan(rt)
        for r in rooms:
            _seed_booking(r, _TODAY, _PLUS_3, status='checked_in')
        result = be.search_availability(_TODAY, _PLUS_2, 1)
        self.assertEqual(len(result['options']), 1)
        opt = result['options'][0]
        self.assertEqual(opt.available, 0)
        self.assertFalse(opt.bookable)
        self.assertTrue(any('no available rooms' in r for r in opt.reasons))

    def test_no_availability_when_all_blocked(self):
        rt = _seed_room_type('DEL', 'Deluxe')
        rooms = _seed_rooms_of_type(rt, count=2)
        _seed_plan(rt)
        for r in rooms:
            db.session.add(RoomBlock(
                room_id=r.id, start_date=_TODAY, end_date=_PLUS_3,
                reason='deep_clean'))
        db.session.commit()
        result = be.search_availability(_TODAY, _PLUS_2, 1)
        self.assertEqual(result['options'][0].available, 0)
        self.assertFalse(result['options'][0].bookable)

    def test_out_of_order_excluded(self):
        rt = _seed_room_type('DEL', 'Deluxe')
        rooms = _seed_rooms_of_type(rt, count=2)
        _seed_plan(rt)
        rooms[0].housekeeping_status = 'out_of_order'
        db.session.commit()
        result = be.search_availability(_TODAY, _PLUS_2, 1)
        self.assertEqual(result['options'][0].available, 1)

    def test_stop_sell_blocks_bookable(self):
        rt = _seed_room_type('DEL', 'Deluxe')
        _seed_rooms_of_type(rt, count=2)
        _seed_plan(rt)
        db.session.add(RateRestriction(
            room_type_id=rt.id, start_date=_TODAY,
            end_date=_TODAY + timedelta(days=30),
            stop_sell=True, is_active=True,
        ))
        db.session.commit()
        result = be.search_availability(_TODAY, _PLUS_2, 1)
        opt = result['options'][0]
        self.assertEqual(opt.available, 2)   # physical inventory unchanged
        self.assertFalse(opt.bookable)        # but stop-sell blocks
        self.assertTrue(any('stop_sell' in r for r in opt.reasons))


# ─────────────────────────────────────────────────────────────────────
# 8) Pricing (Req 8)
# ─────────────────────────────────────────────────────────────────────

class PricingTests(_RouteBase):

    def test_simple_total_uses_plan_base_rate(self):
        rt = _seed_room_type('DEL', 'Deluxe')
        _seed_rooms_of_type(rt, count=1, price=600.0)
        plan = _seed_plan(rt, base_rate=600.0)
        # 3 nights * 600 = 1800
        q = be.quote_stay(rt.id, _TODAY, _TODAY + timedelta(days=3), 1,
                           rate_plan_id=plan.id)
        self.assertTrue(q['ok'])
        self.assertEqual(q['total'], 1800.0)
        self.assertEqual(len(q['nights']), 3)

    def test_seasonal_override_changes_total(self):
        rt = _seed_room_type('DEL', 'Deluxe')
        _seed_rooms_of_type(rt, count=1, price=600.0)
        plan = _seed_plan(rt, base_rate=600.0)
        # Override TOMORROW only → 1000
        db.session.add(RateOverride(
            room_type_id=rt.id, start_date=_TOMORROW,
            end_date=_TOMORROW, nightly_rate=1000.0, is_active=True,
        ))
        db.session.commit()
        # 3 nights spanning [today, today+1, today+2]:
        #   today → 600 (plan base)
        #   tomorrow → 1000 (override)
        #   today+2 → 600 (plan base)
        q = be.quote_stay(rt.id, _TODAY, _TODAY + timedelta(days=3), 1,
                           rate_plan_id=plan.id)
        self.assertEqual(q['total'], 600 + 1000 + 600)

    def test_quote_falls_back_to_room_price_without_plan(self):
        rt = _seed_room_type('DEL', 'Deluxe')
        _seed_rooms_of_type(rt, count=1, price=450.0)
        # No plan, no override — should use room.price_per_night
        q = be.quote_stay(rt.id, _TODAY, _TOMORROW, 1)
        self.assertEqual(q['total'], 450.0)


# ─────────────────────────────────────────────────────────────────────
# 6 + 7) Booking creation + overbooking guard (Reqs 6, 7)
# ─────────────────────────────────────────────────────────────────────

class CreateBookingTests(_RouteBase):

    def test_happy_path_creates_guest_booking_invoice(self):
        rt = _seed_room_type('DEL', 'Deluxe')
        _seed_rooms_of_type(rt, count=2)
        plan = _seed_plan(rt)
        result = be.create_direct_booking(
            room_type_id=rt.id, rate_plan_id=plan.id,
            check_in=_TODAY, check_out=_PLUS_2,
            guests=2,
            first_name='Alice', last_name='Test',
            phone='+9607000111', email='alice@example.com',
        )
        db.session.commit()

        self.assertTrue(result['ok'])
        self.assertIsNotNone(result['booking'])
        self.assertEqual(result['booking'].status, 'pending_payment')
        # 2 nights × 600 = 1200
        self.assertEqual(result['total'], 1200.0)
        self.assertEqual(Guest.query.count(), 1)
        self.assertEqual(Booking.query.count(), 1)
        self.assertIsNotNone(result['booking'].invoice)
        self.assertEqual(result['booking'].invoice.payment_status,
                         'not_received')

    def test_invalid_phone_rejected(self):
        rt = _seed_room_type('DEL', 'Deluxe')
        _seed_rooms_of_type(rt, count=1)
        _seed_plan(rt)
        result = be.create_direct_booking(
            room_type_id=rt.id,
            check_in=_TODAY, check_out=_TOMORROW, guests=1,
            first_name='X', last_name='Y', phone='1',
        )
        self.assertFalse(result['ok'])
        self.assertIn('phone', (result['error'] or '').lower())
        self.assertEqual(Booking.query.count(), 0)

    def test_missing_name_rejected(self):
        rt = _seed_room_type('DEL', 'Deluxe')
        _seed_rooms_of_type(rt, count=1)
        result = be.create_direct_booking(
            room_type_id=rt.id,
            check_in=_TODAY, check_out=_TOMORROW, guests=1,
            first_name='', last_name='Y', phone='+9607000000',
        )
        self.assertFalse(result['ok'])
        self.assertEqual(Booking.query.count(), 0)

    def test_overbooking_prevented(self):
        # 2 rooms in inventory; book both for the requested span
        rt = _seed_room_type('DEL', 'Deluxe')
        rooms = _seed_rooms_of_type(rt, count=2)
        _seed_plan(rt)
        for r in rooms:
            _seed_booking(r, _TODAY, _PLUS_3, status='checked_in')
        # Third booking attempt for the same span MUST be refused
        result = be.create_direct_booking(
            room_type_id=rt.id,
            check_in=_TODAY, check_out=_PLUS_2, guests=1,
            first_name='Over', last_name='Booker',
            phone='+9607000099',
        )
        self.assertFalse(result['ok'])
        self.assertIn('availab', (result['error'] or '').lower())
        # Only the original 2 holding bookings exist
        self.assertEqual(Booking.query.count(), 2)

    def test_stop_sell_refuses_create(self):
        rt = _seed_room_type('DEL', 'Deluxe')
        _seed_rooms_of_type(rt, count=2)
        _seed_plan(rt)
        db.session.add(RateRestriction(
            room_type_id=rt.id, start_date=_TODAY,
            end_date=_PLUS_3, stop_sell=True, is_active=True,
        ))
        db.session.commit()
        result = be.create_direct_booking(
            room_type_id=rt.id,
            check_in=_TODAY, check_out=_PLUS_2, guests=1,
            first_name='S', last_name='S', phone='+9607000000',
        )
        self.assertFalse(result['ok'])
        self.assertIn('stop_sell', (result['error'] or ''))


# ─────────────────────────────────────────────────────────────────────
# Route tests — full HTTP round-trip
# ─────────────────────────────────────────────────────────────────────

class RouteIntegrationTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self.rt = _seed_room_type('DEL', 'Deluxe')
        self.rooms = _seed_rooms_of_type(self.rt, count=2)
        self.plan = _seed_plan(self.rt)

    def test_search_form_renders(self):
        r = self.client.get('/book/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Book your stay', r.data)

    def test_results_renders_with_options(self):
        r = self.client.get('/book/results', query_string={
            'check_in': _TODAY.isoformat(),
            'check_out': _PLUS_2.isoformat(),
            'guests': 1,
        })
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Deluxe', r.data)

    def test_results_invalid_dates_redirects(self):
        r = self.client.get('/book/results', query_string={
            'check_in': _TOMORROW.isoformat(),
            'check_out': _TODAY.isoformat(),
            'guests': 1,
        }, follow_redirects=False)
        self.assertIn(r.status_code, (301, 302))

    def test_select_renders_quote(self):
        r = self.client.get('/book/select', query_string={
            'room_type_id': self.rt.id,
            'rate_plan_id': self.plan.id,
            'check_in': _TODAY.isoformat(),
            'check_out': _PLUS_2.isoformat(),
            'guests': 1,
        })
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Deluxe', r.data)
        # 2 nights × 600 = 1200 surfaces in the markup
        self.assertIn(b'1200', r.data)

    def test_confirm_creates_booking_and_redirects(self):
        r = self.client.post('/book/confirm', data={
            'room_type_id': str(self.rt.id),
            'rate_plan_id': str(self.plan.id),
            'check_in':     _TODAY.isoformat(),
            'check_out':    _PLUS_2.isoformat(),
            'guests':       '1',
            'first_name':   'Alice', 'last_name': 'Tester',
            'phone':        '+9607000111',
        }, follow_redirects=False)
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(Booking.query.count(), 1)
        b = Booking.query.first()
        self.assertEqual(b.status, 'pending_payment')
        # Redirect target points at the confirmation page
        self.assertIn(b.booking_ref, r.headers.get('Location', ''))

    def test_confirmation_page_renders(self):
        # Create a booking first
        result = be.create_direct_booking(
            room_type_id=self.rt.id, rate_plan_id=self.plan.id,
            check_in=_TODAY, check_out=_PLUS_2, guests=1,
            first_name='C', last_name='Test', phone='+9607000222',
        )
        db.session.commit()
        ref = result['booking'].booking_ref
        r = self.client.get(f'/book/confirmation/{ref}')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Reservation request received', r.data)
        self.assertIn(ref.encode(), r.data)


# ─────────────────────────────────────────────────────────────────────
# 9) ActivityLog (Req 9)
# ─────────────────────────────────────────────────────────────────────

class ActivityLogTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self.rt = _seed_room_type('DEL', 'Deluxe')
        self.rooms = _seed_rooms_of_type(self.rt, count=1)
        self.plan = _seed_plan(self.rt)

    def test_search_writes_audit_row(self):
        self.client.get('/book/results', query_string={
            'check_in': _TODAY.isoformat(),
            'check_out': _PLUS_2.isoformat(),
            'guests': 1,
        })
        rows = ActivityLog.query.filter_by(
            action='booking_engine.search_performed').all()
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0].metadata_json or '{}')
        for k in ('check_in', 'check_out', 'guest_count',
                  'option_count', 'bookable_count'):
            self.assertIn(k, meta)
        self.assertEqual(meta['guest_count'], 1)

    def test_create_writes_audit_row(self):
        self.client.post('/book/confirm', data={
            'room_type_id': str(self.rt.id),
            'rate_plan_id': str(self.plan.id),
            'check_in':     _TODAY.isoformat(),
            'check_out':    _PLUS_2.isoformat(),
            'guests':       '1',
            'first_name':   'A', 'last_name': 'B',
            'phone':        '+9607000111',
        })
        rows = ActivityLog.query.filter_by(
            action='booking_engine.booking_created').all()
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0].metadata_json or '{}')
        self.assertIn('booking_id', meta)
        self.assertIn('booking_ref', meta)
        self.assertIn('total', meta)
        self.assertEqual(meta['source'], 'booking_engine')

    def test_failed_create_writes_failed_audit(self):
        # No rooms left → failure path
        _seed_booking(self.rooms[0], _TODAY, _PLUS_3,
                      status='checked_in')
        self.client.post('/book/confirm', data={
            'room_type_id': str(self.rt.id),
            'rate_plan_id': str(self.plan.id),
            'check_in':     _TODAY.isoformat(),
            'check_out':    _PLUS_2.isoformat(),
            'guests':       '1',
            'first_name':   'A', 'last_name': 'B',
            'phone':        '+9607000111',
        })
        rows = ActivityLog.query.filter_by(
            action='booking_engine.booking_failed').all()
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0].metadata_json or '{}')
        self.assertIn('reason', meta)


# ─────────────────────────────────────────────────────────────────────
# 10 + 12) No external side effects + no production coupling
# ─────────────────────────────────────────────────────────────────────

class SafetyTests(_RouteBase):

    def test_full_flow_no_external_calls(self):
        rt = _seed_room_type('DEL', 'Deluxe')
        _seed_rooms_of_type(rt, count=1)
        plan = _seed_plan(rt)
        # Search
        self.client.get('/book/results', query_string={
            'check_in': _TODAY.isoformat(),
            'check_out': _PLUS_2.isoformat(),
            'guests': 1,
        })
        # Confirm
        self.client.post('/book/confirm', data={
            'room_type_id': str(rt.id), 'rate_plan_id': str(plan.id),
            'check_in':     _TODAY.isoformat(),
            'check_out':    _PLUS_2.isoformat(),
            'guests':       '1',
            'first_name':   'A', 'last_name': 'B',
            'phone':        '+9607000111',
        })
        self.assertEqual(wa._send.call_count, 0)
        self.assertEqual(wa._send_template.call_count, 0)
        self.assertEqual(ai_drafts.generate_draft.call_count, 0)


# ─────────────────────────────────────────────────────────────────────
# 11) Migration files only if needed
# ─────────────────────────────────────────────────────────────────────

class MigrationShapeTests(unittest.TestCase):

    def test_no_new_migration_for_booking_engine(self):
        """Booking Engine V1 reuses existing tables — Booking, Guest,
        Invoice, RoomType, RatePlan, RateOverride, RateRestriction,
        RoomBlock, ActivityLog — and adds NO new schema. Confirm no
        booking_engine-named migration was created."""
        versions = _REPO_ROOT / 'migrations' / 'versions'
        be_migs = [p.name for p in versions.glob('*booking_engine*.py')]
        self.assertEqual(be_migs, [],
                         f'booking engine should add no migrations, found {be_migs}')


if __name__ == '__main__':
    unittest.main()
