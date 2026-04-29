"""Tests for Rates & Inventory V1.

Covers the 13 requirements from the build spec, section J:

  1. room type creation/validation
  2. rate plan validation
  3. rate override date validation
  4. seasonal override selection
  5. min/max stay validation
  6. stop_sell blocks availability
  7. booked rooms reduce availability
  8. blocked / out-of-order rooms reduce availability
  9. no overbooking in inventory check
 10. ActivityLog entries created
 11. no WhatsApp / Gemini calls
 12. migration file exists
 13. migration creates only rates / inventory tables (+ optional FK col)
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
    db, User, Room, Guest, Booking, ActivityLog,
    RoomType, RatePlan, RateOverride, RateRestriction, RoomBlock,
)
from app.services import inventory as inv                       # noqa: E402
from app.services import whatsapp as wa                         # noqa: E402
from app.services import ai_drafts                              # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = (
    _REPO_ROOT / 'migrations' / 'versions'
    / 'c5d2a3f8e103_add_rates_inventory_tables.py'
)


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
    admin = User(username=f'inv_admin_{n}', email=f'a{n}@x', role='admin')
    admin.set_password('aaaaaaaaaa1')
    staff = User(username=f'inv_staff_{n}', email=f's{n}@x', role='staff')
    staff.set_password('aaaaaaaaaa1')
    db.session.add_all([admin, staff])
    db.session.commit()
    return admin.id, staff.id


def _seed_room_type(code='DEL', name='Deluxe', max_occ=2):
    rt = RoomType(code=code, name=name,
                  max_occupancy=max_occ, base_capacity=max_occ,
                  is_active=True)
    db.session.add(rt); db.session.commit()
    return rt


def _seed_rooms_of_type(rt, count=2, hk='clean', op_status='available',
                        floor=1, base_num=10):
    rooms = []
    for i in range(count):
        r = Room(number=str(base_num + i), name='T', room_type=rt.name,
                 room_type_id=rt.id, floor=floor, capacity=2,
                 price_per_night=600.0, status=op_status,
                 housekeeping_status=hk)
        db.session.add(r); rooms.append(r)
    db.session.commit()
    return rooms


def _seed_booking(room, check_in, check_out, status='confirmed', guest=None):
    if guest is None:
        guest = Guest(first_name='G', last_name='Test',
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


_TODAY = date.today()
_TOMORROW = _TODAY + timedelta(days=1)


# ─────────────────────────────────────────────────────────────────────
# Validation helper unit tests
# ─────────────────────────────────────────────────────────────────────

class ValidationTests(unittest.TestCase):

    def test_validate_date_range(self):
        self.assertIsNone(inv.validate_date_range(_TODAY, _TODAY))
        self.assertIsNone(inv.validate_date_range(_TODAY, _TODAY + timedelta(days=3)))
        msg = inv.validate_date_range(_TODAY + timedelta(days=3), _TODAY)
        self.assertIn('end_date', msg or '')
        self.assertIn('start_date', inv.validate_date_range(None, _TODAY) or '')

    def test_validate_nightly_rate(self):
        self.assertIsNone(inv.validate_nightly_rate(0.0))
        self.assertIsNone(inv.validate_nightly_rate(100.5))
        self.assertIn('negative', inv.validate_nightly_rate(-1) or '')
        self.assertIn('required', inv.validate_nightly_rate(None) or '')
        self.assertIn('number', inv.validate_nightly_rate('abc') or '')

    def test_validate_min_max_stay(self):
        self.assertIsNone(inv.validate_min_max_stay(None, None))
        self.assertIsNone(inv.validate_min_max_stay(2, 7))
        self.assertIn('>=', inv.validate_min_max_stay(0, None) or '')
        self.assertIn('>=', inv.validate_min_max_stay(None, 0) or '')
        # max < min
        self.assertIn('min_stay', inv.validate_min_max_stay(5, 3) or '')


# ─────────────────────────────────────────────────────────────────────
# Common base: app + WhatsApp/AI hard-blocks (Req 11)
# ─────────────────────────────────────────────────────────────────────

class _RouteBase(unittest.TestCase):

    def setUp(self):
        self._patches = []
        self._wa_send = mock.patch.object(
            wa, '_send', side_effect=AssertionError(
                'WhatsApp must not be called by Inventory V1'))
        self._patches.append(self._wa_send.start())
        self._wa_template = mock.patch.object(
            wa, '_send_template', side_effect=AssertionError(
                'WhatsApp templates must not be called by Inventory V1'))
        self._patches.append(self._wa_template.start())
        self._ai_patch = mock.patch.object(
            ai_drafts, 'generate_draft',
            side_effect=AssertionError(
                'AI drafts must not be called by Inventory V1'))
        self._patches.append(self._ai_patch.start())

        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.admin_id, self.staff_id = _seed_users()
        self.client = self.app.test_client()

    def tearDown(self):
        for p in (self._wa_send, self._wa_template, self._ai_patch):
            p.stop()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True


# ─────────────────────────────────────────────────────────────────────
# 1) Room type creation/validation (Req 1)
# ─────────────────────────────────────────────────────────────────────

class RoomTypeCRUDTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_create_room_type_happy_path(self):
        r = self.client.post('/admin/inventory/room-types/new', data={
            'code': 'STD', 'name': 'Standard',
            'max_occupancy': 3, 'base_capacity': 2,
            'description': 'Two singles or one double.',
        })
        self.assertIn(r.status_code, (301, 302))
        rt = RoomType.query.filter_by(code='STD').first()
        self.assertIsNotNone(rt)
        self.assertEqual(rt.name, 'Standard')
        self.assertTrue(rt.is_active)

    def test_duplicate_code_rejected(self):
        _seed_room_type('DEL', 'Deluxe')
        r = self.client.post('/admin/inventory/room-types/new', data={
            'code': 'DEL', 'name': 'Another',
            'max_occupancy': 2, 'base_capacity': 2,
        })
        # Form re-renders with 400, no second row created
        self.assertEqual(r.status_code, 400)
        self.assertEqual(RoomType.query.filter_by(code='DEL').count(), 1)

    def test_blank_name_rejected(self):
        r = self.client.post('/admin/inventory/room-types/new', data={
            'code': 'X', 'name': '',
            'max_occupancy': 2, 'base_capacity': 2,
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(RoomType.query.count(), 0)

    def test_anonymous_blocked(self):
        # Log out by using a fresh client
        client = self.app.test_client()
        r = client.post('/admin/inventory/room-types/new', data={
            'code': 'X', 'name': 'X',
            'max_occupancy': 1, 'base_capacity': 1,
        })
        self.assertIn(r.status_code, (301, 302, 401))
        self.assertEqual(RoomType.query.count(), 0)

    def test_staff_blocked(self):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(self.staff_id)
            sess['_fresh'] = True
        r = client.post('/admin/inventory/room-types/new', data={
            'code': 'X', 'name': 'X',
            'max_occupancy': 1, 'base_capacity': 1,
        })
        self.assertIn(r.status_code, (302, 401, 403))
        self.assertEqual(RoomType.query.count(), 0)


# ─────────────────────────────────────────────────────────────────────
# 2) Rate plan validation (Req 2)
# ─────────────────────────────────────────────────────────────────────

class RatePlanCRUDTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)
        self.rt = _seed_room_type()

    def test_create_rate_plan_happy_path(self):
        r = self.client.post('/admin/inventory/rate-plans/new', data={
            'code': 'BAR', 'name': 'Best Available',
            'room_type_id': str(self.rt.id),
            'base_rate': '199.00', 'currency': 'USD',
            'is_refundable': '1',
        })
        self.assertIn(r.status_code, (301, 302))
        plan = RatePlan.query.filter_by(code='BAR').first()
        self.assertIsNotNone(plan)
        self.assertEqual(plan.base_rate, 199.0)

    def test_negative_rate_rejected(self):
        r = self.client.post('/admin/inventory/rate-plans/new', data={
            'code': 'X', 'name': 'X',
            'room_type_id': str(self.rt.id),
            'base_rate': '-50',
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(RatePlan.query.count(), 0)

    def test_missing_room_type_rejected(self):
        r = self.client.post('/admin/inventory/rate-plans/new', data={
            'code': 'X', 'name': 'X',
            'room_type_id': '',
            'base_rate': '100',
        })
        self.assertEqual(r.status_code, 400)


# ─────────────────────────────────────────────────────────────────────
# 3) Rate override date validation (Req 3)
# ─────────────────────────────────────────────────────────────────────

class OverrideValidationTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)
        self.rt = _seed_room_type()

    def test_end_before_start_rejected(self):
        r = self.client.post('/admin/inventory/overrides/new', data={
            'room_type_id': str(self.rt.id),
            'start_date': '2026-06-10',
            'end_date': '2026-06-05',
            'nightly_rate': '500',
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(RateOverride.query.count(), 0)

    def test_invalid_date_rejected(self):
        r = self.client.post('/admin/inventory/overrides/new', data={
            'room_type_id': str(self.rt.id),
            'start_date': 'not-a-date',
            'end_date':   '2026-06-05',
            'nightly_rate': '500',
        })
        self.assertEqual(r.status_code, 400)

    def test_negative_rate_rejected(self):
        r = self.client.post('/admin/inventory/overrides/new', data={
            'room_type_id': str(self.rt.id),
            'start_date':  '2026-06-01',
            'end_date':    '2026-06-07',
            'nightly_rate': '-1',
        })
        self.assertEqual(r.status_code, 400)


# ─────────────────────────────────────────────────────────────────────
# 4) Seasonal override selection (Req 4)
# ─────────────────────────────────────────────────────────────────────

class SeasonalSelectionTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)
        self.rt = _seed_room_type()
        # Property fallback / plan base_rate
        self.plan = RatePlan(code='BAR', name='Best Available',
                             room_type_id=self.rt.id,
                             base_rate=200.0, is_active=True)
        db.session.add(self.plan); db.session.commit()

        # High season override June 1-7 (type-wide)
        db.session.add(RateOverride(
            room_type_id=self.rt.id,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 7),
            nightly_rate=500.0, is_active=True,
        ))
        # Plan-scoped override June 5-6 = 700 (most-specific)
        db.session.add(RateOverride(
            room_type_id=self.rt.id,
            rate_plan_id=self.plan.id,
            start_date=date(2026, 6, 5),
            end_date=date(2026, 6, 6),
            nightly_rate=700.0, is_active=True,
        ))
        db.session.commit()

    def test_off_season_uses_plan_base_rate(self):
        rate = inv.nightly_rate_for(self.rt.id, date(2026, 1, 15),
                                     rate_plan_id=self.plan.id)
        self.assertEqual(rate, 200.0)

    def test_high_season_uses_type_override(self):
        rate = inv.nightly_rate_for(self.rt.id, date(2026, 6, 3),
                                     rate_plan_id=self.plan.id)
        self.assertEqual(rate, 500.0)

    def test_plan_scoped_override_wins(self):
        rate = inv.nightly_rate_for(self.rt.id, date(2026, 6, 5),
                                     rate_plan_id=self.plan.id)
        self.assertEqual(rate, 700.0)

    def test_inactive_override_ignored(self):
        # Deactivate the plan-scoped override — fallback to type-wide
        ov = RateOverride.query.filter_by(rate_plan_id=self.plan.id).first()
        ov.is_active = False
        db.session.commit()
        rate = inv.nightly_rate_for(self.rt.id, date(2026, 6, 5),
                                     rate_plan_id=self.plan.id)
        self.assertEqual(rate, 500.0)

    def test_price_stay_aggregates_per_night(self):
        result = inv.price_stay(self.rt.id,
                                 date(2026, 6, 4), date(2026, 6, 8),
                                 rate_plan_id=self.plan.id)
        # 4 nights: 500 (Jun 4) + 700 (Jun 5) + 700 (Jun 6) + 500 (Jun 7)
        self.assertEqual(len(result['nights']), 4)
        self.assertEqual(result['total'], 500 + 700 + 700 + 500)


# ─────────────────────────────────────────────────────────────────────
# 5) Min/max stay validation (Req 5)
# ─────────────────────────────────────────────────────────────────────

class MinMaxStayTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)
        self.rt = _seed_room_type()
        _seed_rooms_of_type(self.rt, count=2)

    def test_invalid_min_stay_rejected_via_route(self):
        r = self.client.post('/admin/inventory/restrictions/new', data={
            'room_type_id': str(self.rt.id),
            'start_date': _TODAY.isoformat(),
            'end_date':   (_TODAY + timedelta(days=10)).isoformat(),
            'min_stay': '0',
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(RateRestriction.query.count(), 0)

    def test_max_less_than_min_rejected(self):
        r = self.client.post('/admin/inventory/restrictions/new', data={
            'room_type_id': str(self.rt.id),
            'start_date': _TODAY.isoformat(),
            'end_date':   (_TODAY + timedelta(days=10)).isoformat(),
            'min_stay': '5', 'max_stay': '3',
        })
        self.assertEqual(r.status_code, 400)

    def test_min_stay_blocks_short_stay(self):
        db.session.add(RateRestriction(
            room_type_id=self.rt.id,
            start_date=_TODAY,
            end_date=_TODAY + timedelta(days=30),
            min_stay=3, is_active=True,
        ))
        db.session.commit()
        # 1-night stay should be flagged
        result = inv.check_restrictions(self.rt.id, _TODAY,
                                         _TODAY + timedelta(days=1))
        self.assertFalse(result['ok'])
        self.assertTrue(any('min_stay' in r for r in result['reasons']))

    def test_max_stay_blocks_long_stay(self):
        db.session.add(RateRestriction(
            room_type_id=self.rt.id,
            start_date=_TODAY,
            end_date=_TODAY + timedelta(days=30),
            max_stay=2, is_active=True,
        ))
        db.session.commit()
        result = inv.check_restrictions(self.rt.id, _TODAY,
                                         _TODAY + timedelta(days=5))
        self.assertFalse(result['ok'])
        self.assertTrue(any('max_stay' in r for r in result['reasons']))


# ─────────────────────────────────────────────────────────────────────
# 6 + 7 + 8 + 9) Availability (Reqs 6/7/8/9)
# ─────────────────────────────────────────────────────────────────────

class AvailabilityTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)
        self.rt = _seed_room_type()
        # 3 active rooms of this type
        self.rooms = _seed_rooms_of_type(self.rt, count=3)

    def test_full_inventory_when_clean(self):
        avail = inv.count_available(self.rt.id, _TODAY,
                                     _TODAY + timedelta(days=2))
        self.assertEqual(avail, 3)

    def test_stop_sell_blocks_check_bookable(self):
        # stop_sell blocks AVAILABILITY (it makes the request unbookable)
        # but does not change physical inventory.
        db.session.add(RateRestriction(
            room_type_id=self.rt.id,
            start_date=_TODAY,
            end_date=_TODAY + timedelta(days=30),
            stop_sell=True, is_active=True,
        ))
        db.session.commit()
        result = inv.check_bookable(self.rt.id, _TODAY,
                                     _TODAY + timedelta(days=2))
        self.assertFalse(result['ok'])
        self.assertTrue(any('stop_sell' in r for r in result['reasons']))

    def test_booked_room_reduces_availability(self):
        # Book one of the 3 rooms for the requested span
        _seed_booking(
            self.rooms[0],
            _TODAY, _TODAY + timedelta(days=2),
            status='confirmed',
        )
        avail = inv.count_available(self.rt.id, _TODAY,
                                     _TODAY + timedelta(days=2))
        self.assertEqual(avail, 2)

    def test_out_of_order_room_reduces_availability(self):
        self.rooms[1].housekeeping_status = 'out_of_order'
        db.session.commit()
        avail = inv.count_available(self.rt.id, _TODAY,
                                     _TODAY + timedelta(days=2))
        self.assertEqual(avail, 2)

    def test_maintenance_room_reduces_availability(self):
        self.rooms[2].status = 'maintenance'
        db.session.commit()
        avail = inv.count_available(self.rt.id, _TODAY,
                                     _TODAY + timedelta(days=2))
        self.assertEqual(avail, 2)

    def test_room_block_reduces_availability(self):
        block = RoomBlock(
            room_id=self.rooms[0].id,
            start_date=_TODAY,
            end_date=_TODAY + timedelta(days=2),
            reason='deep_clean',
        )
        db.session.add(block); db.session.commit()
        avail = inv.count_available(self.rt.id, _TODAY,
                                     _TODAY + timedelta(days=2))
        self.assertEqual(avail, 2)

    def test_no_overbooking_when_all_held(self):
        # Hold all three rooms with overlapping bookings
        for r in self.rooms:
            _seed_booking(r, _TODAY, _TODAY + timedelta(days=3),
                          status='checked_in')
        avail = inv.count_available(self.rt.id,
                                     _TODAY + timedelta(days=1),
                                     _TODAY + timedelta(days=2))
        self.assertEqual(avail, 0)
        result = inv.check_bookable(self.rt.id,
                                     _TODAY + timedelta(days=1),
                                     _TODAY + timedelta(days=2))
        self.assertFalse(result['ok'])
        self.assertTrue(any('no available rooms' in r
                             for r in result['reasons']))

    def test_non_overlapping_booking_does_not_reduce(self):
        # Booking ends before our request starts
        _seed_booking(self.rooms[0],
                      _TODAY - timedelta(days=10),
                      _TODAY - timedelta(days=5),
                      status='checked_out')
        avail = inv.count_available(self.rt.id, _TODAY,
                                     _TODAY + timedelta(days=2))
        self.assertEqual(avail, 3)


# ─────────────────────────────────────────────────────────────────────
# 10) ActivityLog (Req 10)
# ─────────────────────────────────────────────────────────────────────

class ActivityLogTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_room_type_create_writes_audit(self):
        self.client.post('/admin/inventory/room-types/new', data={
            'code': 'XX', 'name': 'X',
            'max_occupancy': 2, 'base_capacity': 2,
        })
        rows = ActivityLog.query.filter_by(
            action='inventory.room_type_created').all()
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0].metadata_json or '{}')
        self.assertEqual(meta['code'], 'XX')
        self.assertIn('room_type_id', meta)

    def test_rate_plan_create_writes_audit(self):
        rt = _seed_room_type()
        self.client.post('/admin/inventory/rate-plans/new', data={
            'code': 'BAR', 'name': 'BAR',
            'room_type_id': str(rt.id),
            'base_rate': '100',
        })
        rows = ActivityLog.query.filter_by(
            action='inventory.rate_plan_created').all()
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0].metadata_json or '{}')
        self.assertEqual(meta['code'], 'BAR')
        self.assertEqual(meta['room_type_id'], rt.id)

    def test_override_create_writes_audit(self):
        rt = _seed_room_type()
        self.client.post('/admin/inventory/overrides/new', data={
            'room_type_id': str(rt.id),
            'start_date': '2026-06-01',
            'end_date':   '2026-06-07',
            'nightly_rate': '500',
        })
        rows = ActivityLog.query.filter_by(
            action='inventory.rate_override_created').all()
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0].metadata_json or '{}')
        for k in ('override_id', 'room_type_id', 'start_date',
                  'end_date', 'nightly_rate'):
            self.assertIn(k, meta)

    def test_restriction_create_writes_audit(self):
        rt = _seed_room_type()
        self.client.post('/admin/inventory/restrictions/new', data={
            'room_type_id': str(rt.id),
            'start_date': _TODAY.isoformat(),
            'end_date':   (_TODAY + timedelta(days=7)).isoformat(),
            'stop_sell':  '1',
        })
        rows = ActivityLog.query.filter_by(
            action='inventory.restriction_updated').all()
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0].metadata_json or '{}')
        self.assertEqual(meta['stop_sell'], True)


# ─────────────────────────────────────────────────────────────────────
# 11) No external side effects (Req 11)
# ─────────────────────────────────────────────────────────────────────
#
# Implicit via _RouteBase. Belt-and-braces here:

class NoExternalSideEffectsTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_create_chain_does_not_call_whatsapp_or_ai(self):
        rt_resp = self.client.post('/admin/inventory/room-types/new', data={
            'code': 'X', 'name': 'X',
            'max_occupancy': 2, 'base_capacity': 2,
        })
        self.assertIn(rt_resp.status_code, (301, 302))
        rt = RoomType.query.filter_by(code='X').first()
        self.client.post('/admin/inventory/rate-plans/new', data={
            'code': 'X1', 'name': 'X1',
            'room_type_id': str(rt.id),
            'base_rate': '50',
        })
        self.client.post('/admin/inventory/overrides/new', data={
            'room_type_id': str(rt.id),
            'start_date': '2026-07-01', 'end_date': '2026-07-07',
            'nightly_rate': '99',
        })
        self.client.post('/admin/inventory/restrictions/new', data={
            'room_type_id': str(rt.id),
            'start_date': '2026-07-10', 'end_date': '2026-07-15',
            'stop_sell': '1',
        })
        self.assertEqual(wa._send.call_count, 0)
        self.assertEqual(wa._send_template.call_count, 0)
        self.assertEqual(ai_drafts.generate_draft.call_count, 0)


# ─────────────────────────────────────────────────────────────────────
# 12 + 13) Migration shape (Reqs 12/13)
# ─────────────────────────────────────────────────────────────────────

class MigrationShapeTests(unittest.TestCase):

    def test_migration_file_exists(self):
        self.assertTrue(_MIGRATION_PATH.exists(),
                        f'expected migration at {_MIGRATION_PATH}')

    def test_migration_has_correct_revision_metadata(self):
        text = _MIGRATION_PATH.read_text()
        self.assertIn("revision = 'c5d2a3f8e103'", text)
        self.assertIn("down_revision = 'b4c1f2d6e892'", text)

    def test_migration_creates_only_inventory_tables(self):
        text = _MIGRATION_PATH.read_text()
        creates = set(re.findall(r"op\.create_table\(\s*'([^']+)'", text))
        self.assertEqual(
            creates,
            {'room_types', 'rate_plans', 'rate_overrides',
             'rate_restrictions'},
            f'unexpected tables: {creates}',
        )
        # add_column targets only `rooms` (for room_type_id)
        added_to = set(re.findall(
            r"op\.add_column\(\s*'([^']+)',\s*sa\.Column\(\s*'([^']+)'", text))
        for tbl, col in added_to:
            self.assertEqual(tbl, 'rooms')
            self.assertEqual(col, 'room_type_id')


if __name__ == '__main__':
    unittest.main()
