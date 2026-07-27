"""Legacy root booking flow retired; root serves the portal (BEv2).

- `/` serves the multi-type portal (not the old single-room landing).
- Legacy `/availability` and `/submit` are gone (404) — the engine-bypassing
  overbooking side door is closed.
- `/confirmation/<ref>` still works for an existing booking (guest links).
"""

from __future__ import annotations

import os
import unittest
from datetime import date, timedelta

for _v in ('DATABASE_URL', 'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                          # noqa: E402
from app import create_app                                        # noqa: E402
from app.models import db, Property, RoomType, Room, Guest, Booking  # noqa: E402


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


class RetireLegacyTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        db.session.add(Property(code='default', name='Test Inn'))
        db.session.commit()
        rt = RoomType(code='STD', name='Standard', max_occupancy=2,
                      base_capacity=2, is_active=True)
        db.session.add(rt); db.session.commit()
        room = Room(number='10', name='T', room_type='Standard', room_type_id=rt.id,
                    floor=1, capacity=2, price_per_night=500.0,
                    status='available', housekeeping_status='clean')
        g = Guest(first_name='Old', last_name='Guest', phone='+9600', email='o@x')
        db.session.add_all([room, g]); db.session.commit()
        b = Booking(booking_ref='LEGACY123', room_id=room.id, guest_id=g.id,
                    check_in_date=date.today() + timedelta(days=5),
                    check_out_date=date.today() + timedelta(days=7),
                    num_guests=1, total_amount=1000.0, status='confirmed')
        db.session.add(b); db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def test_root_serves_the_portal(self):
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Book your stay', r.data)       # portal search page
        self.assertIn(b'check_in', r.data)             # portal date form
        # and it renders identically to /book/
        self.assertEqual(self.client.get('/book/').status_code, 200)

    def test_legacy_availability_gone(self):
        self.assertEqual(self.client.get('/availability?check_in=2027-01-01&check_out=2027-01-03').status_code, 404)

    def test_legacy_submit_gone(self):
        self.assertEqual(self.client.post('/submit', data={'room_id': '1'}).status_code, 404)

    def test_confirmation_still_works(self):
        self.assertEqual(self.client.get('/confirmation/LEGACY123').status_code, 200)
        self.assertEqual(self.client.get('/confirmation/NOPE404').status_code, 404)


if __name__ == '__main__':
    unittest.main()
