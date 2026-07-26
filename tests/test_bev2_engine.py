"""Booking Engine V2 Phase 1 — engine logic tests (D1/D2/D4/D5 + invariant).

Runs on in-memory SQLite (no live DB, no writes outside tmp). Messaging is
disabled via _TestConfig. The concurrency RACE guarantee is proven separately
against Postgres in test_bev2_race.py (SQLite can't express the race).
"""

from __future__ import annotations

import os
import unittest
from datetime import date, timedelta, datetime

for _v in ('DATABASE_URL', 'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN',
           'AI_DRAFT_PROVIDER', 'GEMINI_API_KEY', 'ANTHROPIC_API_KEY'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                          # noqa: E402
from app import create_app                                        # noqa: E402
from app.models import (                                          # noqa: E402
    db, User, Room, Guest, Booking, RoomType, RoomBlock, Hold,
    BookingGroup, ActivityLog, PropertySettings, Property,
)
from app.services import inventory, holds, assignment, group_booking  # noqa: E402

_T0 = date.today() + timedelta(days=10)   # keep clear of any 'today' logic
_T2 = _T0 + timedelta(days=2)
_T3 = _T0 + timedelta(days=3)


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


class _Base(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        db.session.add(Property(code='default', name='Test Property'))
        db.session.commit()   # property_id=1 for FK integrity on Postgres
        self.admin = User(username='bev2_admin', email='a@x', role='admin')
        self.admin.set_password('aaaaaaaaaa1')
        db.session.add(self.admin)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    # helpers
    def _type(self, code='DEL', name='Deluxe', rooms=2, base=10, price=600.0):
        rt = RoomType(code=code, name=name, max_occupancy=2,
                      base_capacity=2, is_active=True)
        db.session.add(rt)
        db.session.commit()
        for i in range(rooms):
            db.session.add(Room(number=f'{base+i}', name='T', room_type=name,
                                room_type_id=rt.id, floor=1, capacity=2,
                                price_per_night=price, status='available',
                                housekeeping_status='clean'))
        db.session.commit()
        return rt

    def _guest(self):
        g = Guest(first_name='Lead', last_name='Guest', phone='+9607000001', email='g@x')
        db.session.add(g)
        db.session.commit()
        return g

    def _book(self, room, ci, co, status='confirmed'):
        g = self._guest()
        b = Booking(booking_ref=f'BK{room.id}{ci}', room_id=room.id, guest_id=g.id,
                    check_in_date=ci, check_out_date=co, num_guests=1,
                    total_amount=0.0, status=status)
        db.session.add(b)
        db.session.commit()
        return b


# ── D1: inventory truth + contiguity + OOO ──────────────────────────

class InventoryTests(_Base):
    def test_sellable_equals_physical_when_empty(self):
        rt = self._type(rooms=3)
        self.assertEqual(inventory.sellable(rt.id, _T0), 3)

    def test_available_for_stay_contiguity(self):
        rt = self._type(rooms=2)
        av = inventory.available_for_stay(rt.id, _T0, _T2, qty=2)
        self.assertTrue(av['ok'])
        self.assertEqual(av['contiguous_free'], 2)

    def test_booking_reduces_contiguous_and_sellable(self):
        rt = self._type(rooms=2)
        rooms = Room.query.filter_by(room_type_id=rt.id).all()
        self._book(rooms[0], _T0, _T2)                 # one room taken
        self.assertEqual(inventory.sellable(rt.id, _T0), 1)
        self.assertFalse(inventory.available_for_stay(rt.id, _T0, _T2, qty=2)['ok'])
        self.assertTrue(inventory.available_for_stay(rt.id, _T0, _T2, qty=1)['ok'])

    def test_room_block_ooo_reduces_sellable_immediately(self):
        rt = self._type(rooms=2)
        rooms = Room.query.filter_by(room_type_id=rt.id).all()
        db.session.add(RoomBlock(room_id=rooms[0].id, start_date=_T0,
                                 end_date=_T2, reason='maintenance'))
        db.session.commit()
        self.assertEqual(inventory.sellable(rt.id, _T0), 1)  # OOO the moment flagged

    def test_housekeeping_ooo_excluded(self):
        rt = self._type(rooms=2)
        r = Room.query.filter_by(room_type_id=rt.id).first()
        r.housekeeping_status = 'out_of_order'
        db.session.commit()
        self.assertEqual(inventory.sellable(rt.id, _T0), 1)

    def test_fragmented_not_contiguous(self):
        # room A free nights 1..2, room B free night 2..3; no single room spans 1..3
        rt = self._type(rooms=2)
        a, b = Room.query.filter_by(room_type_id=rt.id).all()
        self._book(a, _T2, _T3)   # A busy on the 3rd night
        self._book(b, _T0, _T2)   # B busy on the first two nights
        av = inventory.available_for_stay(rt.id, _T0, _T3, qty=1)
        self.assertFalse(av['ok'])          # fragmented -> not sold publicly
        self.assertEqual(av['contiguous_free'], 0)


# ── D2: holds ───────────────────────────────────────────────────────

class HoldTests(_Base):
    def test_selection_hold_consumes_inventory(self):
        rt = self._type(rooms=1)
        res = holds.acquire_selection_hold(rt.id, _T0, _T2, qty=1)
        self.assertTrue(res['ok'])
        self.assertEqual(inventory.sellable(rt.id, _T0), 0)
        # a second acquire for the last room fails cleanly
        res2 = holds.acquire_selection_hold(rt.id, _T0, _T2, qty=1)
        self.assertFalse(res2['ok'])

    def test_expired_unswept_hold_does_not_block(self):
        rt = self._type(rooms=1)
        res = holds.acquire_selection_hold(rt.id, _T0, _T2, qty=1)
        h = Hold.query.get(res['hold_id'])
        h.expires_at = datetime.utcnow() - timedelta(minutes=1)   # expired, not swept
        db.session.commit()
        self.assertEqual(inventory.sellable(rt.id, _T0), 1)       # effectively released

    def test_sweep_transitions_never_deletes(self):
        rt = self._type(rooms=1)
        res = holds.acquire_selection_hold(rt.id, _T0, _T2, qty=1)
        hid = res['hold_id']
        Hold.query.get(hid).expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        out = holds.sweep_expired()
        self.assertEqual(out['expired'], 1)
        h = Hold.query.get(hid)
        self.assertIsNotNone(h)                     # ROW STILL EXISTS
        self.assertEqual(h.state, 'expired')        # state transition
        self.assertTrue(ActivityLog.query.filter_by(action='hold.selection_expired').count() >= 1)
        self.assertIsNotNone(holds.last_sweep())    # diag observability

    def test_release_requires_reason_and_audits(self):
        rt = self._type(rooms=1)
        res = holds.acquire_selection_hold(rt.id, _T0, _T2, qty=1)
        self.assertFalse(holds.release_hold(res['hold_id'], reason='  ')['ok'])
        ok = holds.release_hold(res['hold_id'], reason='guest abandoned',
                                user_id=self.admin.id)
        self.assertTrue(ok['ok'])
        self.assertEqual(Hold.query.get(res['hold_id']).state, 'released')
        self.assertEqual(inventory.sellable(rt.id, _T0), 1)

    def test_extend_bumps_expiry(self):
        rt = self._type(rooms=1)
        res = holds.acquire_selection_hold(rt.id, _T0, _T2, qty=1)
        before = Hold.query.get(res['hold_id']).expires_at
        holds.extend_hold(res['hold_id'], minutes=30, user_id=self.admin.id)
        self.assertGreater(Hold.query.get(res['hold_id']).expires_at, before)

    def test_ttls_come_from_settings(self):
        # change the config TTL; a new hold must honour it
        s = PropertySettings.query.first() or PropertySettings(property_name='X')
        if s.id is None:
            db.session.add(s)
        s.selection_hold_ttl_minutes = 45
        db.session.commit()
        self.assertEqual(holds.get_ttls()['selection_minutes'], 45)


# ── D4: assignment ──────────────────────────────────────────────────

class AssignmentTests(_Base):
    def test_best_fit_prefers_tight_gap(self):
        rt = self._type(rooms=2, base=20)
        a, b = Room.query.filter_by(room_type_id=rt.id).order_by(Room.id).all()
        # Room A has a neighbouring booking right after the stay -> tighter fit.
        self._book(a, _T3, _T3 + timedelta(days=1))
        pick = assignment.best_fit_room(rt.id, _T0, _T3)
        self.assertEqual(pick.id, a.id)

    def test_contiguity_refusal_returns_none(self):
        rt = self._type(rooms=1, base=30)
        r = Room.query.filter_by(room_type_id=rt.id).first()
        self._book(r, _T0, _T2)
        self.assertIsNone(assignment.best_fit_room(rt.id, _T0, _T2))

    def test_ooo_excluded_from_assignment(self):
        rt = self._type(rooms=1, base=40)
        r = Room.query.filter_by(room_type_id=rt.id).first()
        r.housekeeping_status = 'out_of_order'
        db.session.commit()
        self.assertIsNone(assignment.best_fit_room(rt.id, _T0, _T2))

    def test_tie_break_deterministic(self):
        rt = self._type(rooms=2, base=50)
        a, b = Room.query.filter_by(room_type_id=rt.id).order_by(Room.id).all()
        pick = assignment.best_fit_room(rt.id, _T0, _T2)   # both wide open -> tie
        self.assertEqual(pick.id, a.id)                    # lowest id wins


# ── D5: group creation (atomic) ─────────────────────────────────────

class GroupTests(_Base):
    def test_multi_type_group_atomic_success(self):
        std = self._type(code='STD', name='Standard', rooms=2, base=10)
        dlx = self._type(code='DLX', name='DeluxeX', rooms=1, base=20)
        g = self._guest()
        res = group_booking.create_group_booking(
            [{'room_type_id': std.id, 'qty': 2},
             {'room_type_id': dlx.id, 'qty': 1}],
            _T0, _T2, lead_guest=g, created_by=self.admin.id)
        self.assertTrue(res['ok'], res['reasons'])
        self.assertEqual(len(res['booking_ids']), 3)
        grp = BookingGroup.query.get(res['group_id'])
        self.assertIsNotNone(grp.master_booking_id)
        self.assertEqual(Booking.query.filter_by(booking_group_id=grp.id).count(), 3)
        # inventory consumed (no counters — the bookings themselves)
        self.assertEqual(inventory.sellable(std.id, _T0), 0)
        self.assertEqual(inventory.sellable(dlx.id, _T0), 0)

    def test_partial_failure_rolls_back_everything(self):
        std = self._type(code='STD', name='Standard', rooms=1, base=10)  # only 1
        dlx = self._type(code='DLX', name='DeluxeX', rooms=1, base=20)
        g = self._guest()
        # ask for 2 Standard (impossible) + 1 Deluxe -> whole thing must roll back
        res = group_booking.create_group_booking(
            [{'room_type_id': std.id, 'qty': 2},
             {'room_type_id': dlx.id, 'qty': 1}],
            _T0, _T2, lead_guest=g, created_by=self.admin.id)
        self.assertFalse(res['ok'])
        self.assertEqual(BookingGroup.query.count(), 0)     # no orphan group
        self.assertEqual(Booking.query.count(), 0)          # no consumed inventory
        self.assertEqual(inventory.sellable(dlx.id, _T0), 1)  # deluxe untouched


# ── confirm (pending -> booking + assignment) + admin panel ─────────

class ConfirmAndPanelTests(_Base):
    def _login(self, uid):
        self.client = self.app.test_client()
        with self.client.session_transaction() as s:
            s['_user_id'] = str(uid); s['_fresh'] = True

    def test_confirm_pending_creates_assigned_booking(self):
        rt = self._type(rooms=1)
        g = self._guest()
        res = holds.acquire_pending_hold(rt.id, _T0, _T2, qty=1, lead_guest_id=g.id)
        self.assertTrue(res['ok'])
        conf = holds.confirm_pending(res['hold_id'], user_id=self.admin.id)
        self.assertTrue(conf['ok'], conf.get('reasons'))
        self.assertEqual(len(conf['booking_ids']), 1)
        b = Booking.query.get(conf['booking_ids'][0])
        self.assertEqual(b.status, 'confirmed')
        self.assertIsNotNone(b.room_id)                      # auto-assigned
        self.assertEqual(Hold.query.get(res['hold_id']).state, 'converted')

    def test_holds_panel_renders_200(self):
        rt = self._type(rooms=1)
        holds.acquire_selection_hold(rt.id, _T0, _T2, qty=1)
        self._login(self.admin.id)
        self.assertEqual(self.client.get('/admin/holds/').status_code, 200)

    def test_release_via_route_requires_reason(self):
        rt = self._type(rooms=1)
        res = holds.acquire_selection_hold(rt.id, _T0, _T2, qty=1)
        self._login(self.admin.id)
        # empty reason -> hold stays active
        self.client.post(f'/admin/holds/{res["hold_id"]}/release', data={'reason': ''})
        self.assertEqual(Hold.query.get(res['hold_id']).state, 'active')
        # with reason -> released
        self.client.post(f'/admin/holds/{res["hold_id"]}/release',
                         data={'reason': 'test release'})
        self.assertEqual(Hold.query.get(res['hold_id']).state, 'released')


# ── invariant ───────────────────────────────────────────────────────

class InvariantTests(_Base):
    def test_healthy_inventory_has_no_violations(self):
        rt = self._type(rooms=2)
        holds.acquire_selection_hold(rt.id, _T0, _T2, qty=1)
        self._book(Room.query.filter_by(room_type_id=rt.id).first(), _T0, _T2)
        self.assertEqual(inventory.invariant_violations(horizon_days=20), [])


if __name__ == '__main__':
    unittest.main()
