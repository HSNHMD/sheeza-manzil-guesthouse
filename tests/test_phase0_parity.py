"""Pepper Phase 0 — shared-layer parity tests.

Covers the two approved parity fixes on the create path:
  * Nationality REQUIRED — portal submit AND the shared create_group_booking
    new-guest (dict) branch; existing Guest instances (admin confirm) are NOT
    re-validated.
  * Capacity check UNCONDITIONAL — create_group_booking always runs the
    occupancy backstop; null adults/children coalesce to 1/0 (never a 500);
    confirm_pending sources counts from the pending hold.
"""

from __future__ import annotations

import os
import unittest
from datetime import date, timedelta, datetime

for _v in ('DATABASE_URL', 'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN',
           'AI_DRAFT_PROVIDER', 'GEMINI_API_KEY', 'ANTHROPIC_API_KEY'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                            # noqa: E402
from app import create_app                                          # noqa: E402
from app.models import (db, User, Room, Guest, Booking, RoomType,    # noqa: E402
                        Hold, Property)
from app.services import (portal as portal_svc, holds as holds_svc,  # noqa: E402
                          group_booking)

_CI = date.today() + timedelta(days=14)
_CO = _CI + timedelta(days=2)


class _Cfg(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


class _Base(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_Cfg)
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        db.session.add(Property(code='default', name='Test Property'))
        db.session.commit()
        self.admin = User(username='pa', email='a@x', role='admin')
        self.admin.set_password('aaaaaaaaaa1')
        db.session.add(self.admin); db.session.commit()
        self.t1 = self._type('STD', 'Standard', 3, 10)

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def _type(self, code, name, rooms, base):
        rt = RoomType(code=code, name=name, max_occupancy=2, base_capacity=2,
                      is_active=True)
        db.session.add(rt); db.session.commit()
        for i in range(rooms):
            db.session.add(Room(number=f'{base+i}', name='T', room_type=name,
                                room_type_id=rt.id, floor=1, capacity=2,
                                price_per_night=500.0, status='available',
                                housekeeping_status='clean'))
        db.session.commit()
        return rt

    def _guest(self, nationality='MDV'):
        g = Guest(first_name='A', last_name='B', phone='7', nationality=nationality)
        db.session.add(g); db.session.commit()
        return g


class NationalityRequiredTest(_Base):
    def test_shared_layer_rejects_new_guest_without_nationality(self):
        res = group_booking.create_group_booking(
            [{'room_type_id': self.t1.id, 'qty': 1}], _CI, _CO,
            lead_guest={'first_name': 'X', 'last_name': 'Y', 'phone': '9'},
            adults=1, force_group=False)
        self.assertFalse(res['ok'])
        self.assertTrue(any('nationality' in r.lower() for r in res['reasons']))
        self.assertEqual(Booking.query.count(), 0)  # rolled back, no orphan

    def test_shared_layer_accepts_new_guest_with_nationality(self):
        res = group_booking.create_group_booking(
            [{'room_type_id': self.t1.id, 'qty': 1}], _CI, _CO,
            lead_guest={'first_name': 'X', 'last_name': 'Y', 'phone': '9',
                        'nationality': 'MDV'},
            adults=1, force_group=False)
        self.assertTrue(res['ok'], res['reasons'])

    def test_existing_guest_with_null_nationality_not_revalidated(self):
        # Admin-confirm shape: a pre-existing Guest (even null nationality) must
        # still be bookable — we only enforce on NEW guest creation.
        g = self._guest(nationality=None)
        res = group_booking.create_group_booking(
            [{'room_type_id': self.t1.id, 'qty': 1}], _CI, _CO,
            lead_guest=g, adults=1, force_group=False)
        self.assertTrue(res['ok'], res['reasons'])

    def test_portal_submit_requires_nationality(self):
        tok = 'tok-nat-1'
        holds_svc  # ensure imported
        portal_svc.create_holds([{'room_type_id': self.t1.id, 'qty': 1}],
                                _CI, _CO, tok, guests=1)
        res = portal_svc.submit(tok, {'first_name': 'A', 'last_name': 'B',
                                      'phone': '9', 'adults': '1',
                                      'nationality': ''})
        self.assertFalse(res['ok'])
        self.assertTrue(any('nationality' in r.lower() for r in res['reasons']))

    def test_portal_submit_accepts_with_nationality(self):
        tok = 'tok-nat-2'
        portal_svc.create_holds([{'room_type_id': self.t1.id, 'qty': 1}],
                                _CI, _CO, tok, guests=1)
        res = portal_svc.submit(tok, {'first_name': 'A', 'last_name': 'B',
                                      'phone': '9', 'adults': '1',
                                      'nationality': 'MDV'})
        self.assertTrue(res['ok'], res.get('reasons'))


class UnconditionalCapacityTest(_Base):
    def test_adults_none_no_longer_skips_and_coalesces(self):
        # Previously adults=None skipped the whole guest-count/capacity block.
        # Now it runs unconditionally and coalesces to 1/0 (no 500).
        g = self._guest()
        res = group_booking.create_group_booking(
            [{'room_type_id': self.t1.id, 'qty': 1}], _CI, _CO,
            lead_guest=g, adults=None, children=None, force_group=False)
        self.assertTrue(res['ok'], res['reasons'])
        b = Booking.query.get(res['booking_ids'][0])
        self.assertEqual(b.adults, 1)
        self.assertEqual(b.children, 0)
        self.assertEqual(b.num_guests, 1)

    def test_over_capacity_still_blocked(self):
        g = self._guest()
        res = group_booking.create_group_booking(
            [{'room_type_id': self.t1.id, 'qty': 1}], _CI, _CO,
            lead_guest=g, adults=5, children=0, force_group=False)  # max_occ 2
        self.assertFalse(res['ok'])
        self.assertEqual(Booking.query.count(), 0)

    def test_confirm_pending_sources_counts_from_hold(self):
        g = self._guest()
        # 4 guests (max_occ 2/room) → needs 2 rooms; qty=2 so capacity passes and
        # we can assert the counts came from the hold, not a default.
        h = Hold(session_token='s1', hold_type='pending', state='active',
                 room_type_id=self.t1.id, qty=2,
                 check_in_date=_CI, check_out_date=_CO,
                 expires_at=datetime.utcnow() + timedelta(hours=6),
                 lead_guest_id=g.id, adults=3, children=1)
        db.session.add(h); db.session.commit()
        res = holds_svc.confirm_pending(h.id, user_id=self.admin.id)
        self.assertTrue(res['ok'], res.get('reasons'))
        b = Booking.query.get(res['booking_ids'][0])
        self.assertEqual(b.adults, 3)
        self.assertEqual(b.children, 1)
        self.assertEqual(b.num_guests, 4)

    def test_confirm_pending_null_counts_degrade_safely(self):
        # A legacy pending hold with null counts must NOT 500 — coalesces to 1/0.
        g = self._guest()
        h = Hold(session_token='s2', hold_type='pending', state='active',
                 room_type_id=self.t1.id, qty=1,
                 check_in_date=_CI, check_out_date=_CO,
                 expires_at=datetime.utcnow() + timedelta(hours=6),
                 lead_guest_id=g.id, adults=None, children=None)
        db.session.add(h); db.session.commit()
        res = holds_svc.confirm_pending(h.id, user_id=self.admin.id)
        self.assertTrue(res['ok'], res.get('reasons'))
        b = Booking.query.get(res['booking_ids'][0])
        self.assertEqual((b.adults, b.children), (1, 0))


if __name__ == '__main__':
    unittest.main()
