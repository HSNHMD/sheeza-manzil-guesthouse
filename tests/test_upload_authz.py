"""Tests for guest-upload file-serving authorization (H2 fix).

The `/bookings/uploads/<filename>` route serves guest ID/passport images and
payment slips. It must be admin-gated — a logged-in non-admin (staff) must NOT
be able to download these sensitive files. Mirrors the admin-gate convention
already covered for cashiering/folios.

Hard rules covered:
  - Anonymous is redirected / 401 (never served a file)
  - Staff (non-admin) gets 403 (the H2 fix — previously 200)
  - Admin is served the file (200)
"""

from __future__ import annotations

import os
import unittest
from datetime import date, timedelta

for _v in ('DATABASE_URL', 'AI_DRAFT_PROVIDER', 'GEMINI_API_KEY',
           'ANTHROPIC_API_KEY', 'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from flask import current_app                                    # noqa: E402
from config import Config                                        # noqa: E402
from app import create_app                                       # noqa: E402
from app.models import db, User, Room, Guest, Booking            # noqa: E402


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


_UPLOAD_NAME = 'authz_test_idcard.bin'


class UploadAuthzTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        admin = User(username='up_admin', email='a@x', role='admin')
        admin.set_password('aaaaaaaaaa1')
        staff = User(username='up_staff', email='s@x', role='staff')
        staff.set_password('aaaaaaaaaa1')
        db.session.add_all([admin, staff])
        db.session.commit()
        self.admin_id, self.staff_id = admin.id, staff.id

        guest = Guest(first_name='Test', last_name='Guest',
                      phone='+9607000001', email='g@x')
        db.session.add(guest)
        room = Room(number='90', name='T', room_type='T',
                    floor=0, capacity=2, price_per_night=500.0)
        db.session.add(room)
        db.session.commit()
        booking = Booking(
            booking_ref='BKUP001', room_id=room.id, guest_id=guest.id,
            check_in_date=date.today() + timedelta(days=1),
            check_out_date=date.today() + timedelta(days=3),
            num_guests=1, total_amount=1000.0, status='confirmed',
            id_card_filename=_UPLOAD_NAME,
        )
        db.session.add(booking)
        db.session.commit()

        # Put a real file on disk so the admin path reaches a 200 send_from_directory.
        self.upload_dir = os.path.join(current_app.root_path, 'uploads')
        os.makedirs(self.upload_dir, exist_ok=True)
        self.upload_path = os.path.join(self.upload_dir, _UPLOAD_NAME)
        with open(self.upload_path, 'wb') as fh:
            fh.write(b'not-a-real-id-doc')

        self.client = self.app.test_client()

    def tearDown(self):
        try:
            os.remove(self.upload_path)
        except OSError:
            pass
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True

    def test_upload_anonymous_blocked(self):
        r = self.client.get(f'/bookings/uploads/{_UPLOAD_NAME}')
        self.assertIn(r.status_code, (301, 302, 401))

    def test_upload_staff_forbidden(self):
        self._login(self.staff_id)
        r = self.client.get(f'/bookings/uploads/{_UPLOAD_NAME}')
        self.assertEqual(r.status_code, 403)

    def test_upload_admin_ok(self):
        self._login(self.admin_id)
        r = self.client.get(f'/bookings/uploads/{_UPLOAD_NAME}')
        self.assertEqual(r.status_code, 200)


if __name__ == '__main__':
    unittest.main()
