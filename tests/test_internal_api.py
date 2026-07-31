"""Pepper internal API (Phase 0) — auth + create + outbox tests.

Exercises the bearer gate (200 with token / 401 without), the PII-free whitelist,
one-shot booking creation through the shared authority (nationality + adults
required), and the transactional outbox.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta

for _v in ('DATABASE_URL', 'WHATSAPP_ENABLED', 'AI_DRAFT_PROVIDER',
           'GEMINI_API_KEY', 'ANTHROPIC_API_KEY'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                            # noqa: E402
from app.models import db, Property, RoomType, Room, PepperUser      # noqa: E402
from internal_wsgi import create_internal_app                        # noqa: E402

_CI = (date.today() + timedelta(days=20)).isoformat()
_CO = (date.today() + timedelta(days=22)).isoformat()
_TOKEN = 'test-pepper-internal-token-0123456789abcdef'


class InternalApiTest(unittest.TestCase):
    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix='.db')

        class _Cfg(Config):
            TESTING = True
            SQLALCHEMY_DATABASE_URI = f'sqlite:///{self._path}'
            WTF_CSRF_ENABLED = False
            WHATSAPP_ENABLED = False
            PEPPER_INTERNAL_TOKEN = _TOKEN

        self.app = create_internal_app(_Cfg)
        with self.app.app_context():
            db.create_all()
            db.session.add(Property(code='default', name='P'))
            rt = RoomType(code='STD', name='Standard', max_occupancy=2,
                          base_capacity=2, is_active=True)
            db.session.add(rt); db.session.commit()
            self.rt_id = rt.id
            for i in range(3):
                db.session.add(Room(number=str(10 + i), name='T',
                                    room_type='Standard', room_type_id=rt.id,
                                    floor=1, capacity=2, price_per_night=500.0,
                                    status='available', housekeeping_status='clean'))
            db.session.add(PepperUser(telegram_id=111, role='manager',
                                      display_name='Aisha'))
            db.session.commit()
        self.c = self.app.test_client()

    def tearDown(self):
        with self.app.app_context():
            db.drop_all()
        os.close(self._fd)
        try:
            os.remove(self._path)
        except OSError:
            pass

    def _auth(self):
        return {'Authorization': f'Bearer {_TOKEN}'}

    # --- auth gate ---
    def test_ping_without_token_401(self):
        self.assertEqual(self.c.get('/api/internal/pepper/ping').status_code, 401)

    def test_ping_wrong_token_401(self):
        r = self.c.get('/api/internal/pepper/ping',
                       headers={'Authorization': 'Bearer nope'})
        self.assertEqual(r.status_code, 401)

    def test_ping_with_token_200(self):
        r = self.c.get('/api/internal/pepper/ping', headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])

    # --- whitelist (PII-free) ---
    def test_whitelist_known_user(self):
        r = self.c.get('/api/internal/pepper/whitelist/111', headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {'allowed': True, 'role': 'manager'})

    def test_whitelist_unknown_user_denied(self):
        r = self.c.get('/api/internal/pepper/whitelist/999', headers=self._auth())
        self.assertEqual(r.get_json(), {'allowed': False, 'role': None})

    def test_whitelist_owner_from_env(self):
        os.environ['PEPPER_OWNER_ID'] = '777'
        try:
            r = self.c.get('/api/internal/pepper/whitelist/777',
                           headers=self._auth())
            self.assertEqual(r.get_json()['role'], 'owner')
        finally:
            os.environ.pop('PEPPER_OWNER_ID', None)

    # --- create via shared authority ---
    def _booking_body(self, **over):
        body = {'items': [{'room_type_id': self.rt_id, 'qty': 1}],
                'check_in': _CI, 'check_out': _CO, 'adults': 2,
                'guest': {'first_name': 'A', 'last_name': 'B', 'phone': '9',
                          'nationality': 'MDV'}}
        body.update(over)
        return body

    def test_create_requires_nationality(self):
        body = self._booking_body(guest={'first_name': 'A', 'phone': '9'})
        r = self.c.post('/api/internal/pepper/bookings', json=body,
                        headers=self._auth())
        self.assertEqual(r.status_code, 400)
        self.assertIn('nationality', r.get_json()['error'].lower())

    def test_create_requires_adults(self):
        body = self._booking_body(); body.pop('adults')
        r = self.c.post('/api/internal/pepper/bookings', json=body,
                        headers=self._auth())
        self.assertEqual(r.status_code, 400)

    def test_create_success_and_outbox_row(self):
        r = self.c.post('/api/internal/pepper/bookings',
                        json=self._booking_body(), headers=self._auth())
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        bid = r.get_json()['booking_ids'][0]
        # booking detail
        d = self.c.get(f'/api/internal/pepper/bookings/{bid}', headers=self._auth())
        self.assertEqual(d.status_code, 200)
        self.assertEqual(d.get_json()['adults'], 2)
        # outbox row was written in the create transaction
        o = self.c.get('/api/internal/pepper/outbox?undelivered=1',
                       headers=self._auth())
        events = o.get_json()['events']
        self.assertTrue(any(e['event_type'] == 'booking.created'
                            and e['booking_id'] == bid for e in events))
        # mark delivered
        row_id = next(e['id'] for e in events if e['booking_id'] == bid)
        m = self.c.post(f'/api/internal/pepper/outbox/{row_id}/delivered',
                        headers=self._auth())
        self.assertEqual(m.status_code, 200)
        o2 = self.c.get('/api/internal/pepper/outbox?undelivered=1',
                        headers=self._auth())
        self.assertFalse(any(e['id'] == row_id for e in o2.get_json()['events']))

    def test_create_over_capacity_rejected_409(self):
        body = self._booking_body(adults=5)  # 1 room, max_occ 2
        r = self.c.post('/api/internal/pepper/bookings', json=body,
                        headers=self._auth())
        self.assertEqual(r.status_code, 409)
        self.assertFalse(r.get_json()['ok'])


if __name__ == '__main__':
    unittest.main()
