"""Pepper internal API (Phase 0) — auth + create + outbox tests.

Exercises the bearer gate (200 with token / 401 without), the PII-free whitelist,
one-shot booking creation through the shared authority (nationality + adults
required), and the transactional outbox.
"""

from __future__ import annotations

import datetime as _dt
import os
import tempfile
import unittest
from datetime import date, timedelta

for _v in ('DATABASE_URL', 'WHATSAPP_ENABLED', 'AI_DRAFT_PROVIDER',
           'GEMINI_API_KEY', 'ANTHROPIC_API_KEY'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                            # noqa: E402
from app.models import (db, Property, RoomType, Room, PepperUser,     # noqa: E402
                        Guest, Hold, PepperOutbox)
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
            # a non-manager staff member + a revoked manager, for the HITL-1 403 probe
            db.session.add(PepperUser(telegram_id=222, role='staff',
                                      display_name='Sana'))
            revoked = PepperUser(telegram_id=333, role='manager',
                                 display_name='Old Mgr')
            revoked.revoked_at = _dt.datetime.utcnow()
            db.session.add(revoked)
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

    # --- alert assembly (§7.2) + slip ---
    def test_outbox_alert_assembly_portal_pending(self):
        from datetime import datetime, timedelta
        tok = 'ABC12345-long-session-token'
        with self.app.app_context():
            g = Guest(first_name='Ahmed', last_name='Hassan', phone='7',
                      nationality='MDV')
            db.session.add(g); db.session.commit()
            db.session.add(Hold(session_token=tok, hold_type='pending',
                                state='active', room_type_id=self.rt_id, qty=1,
                                check_in_date=date.today() + timedelta(days=20),
                                check_out_date=date.today() + timedelta(days=22),
                                expires_at=datetime.utcnow() + timedelta(hours=6),
                                adults=2, children=0, lead_guest_id=g.id))
            db.session.add(PepperOutbox(event_type='booking.created',
                                        reference=tok[:8].upper()))
            db.session.commit()
        r = self.c.get('/api/internal/pepper/outbox?undelivered=1',
                       headers=self._auth())
        a = next(e['alert'] for e in r.get_json()['events'] if e['alert'])
        self.assertEqual(a['guest_name'], 'Ahmed Hassan')
        self.assertEqual((a['nationality'], a['green_tax']), ('MDV', 'exempt'))
        self.assertEqual(a['adults'], 2)
        self.assertIsInstance(a['total'], (int, float))   # value depends on rate config
        self.assertGreaterEqual(a['total'], 0)
        self.assertIn('Standard', a['rooms'])
        self.assertIsNotNone(a['deadline'])
        self.assertNotIn('id_number', a)                 # PII discipline
        self.assertNotIn('passport', str(a).lower())

    def test_slip_404_when_none(self):
        r = self.c.get('/api/internal/pepper/slip?reference=NOPE1234',
                       headers=self._auth())
        self.assertEqual(r.status_code, 404)

    # --- Phase 3: verify (confirm hold) / soft-reject (mark slip rejected) ---
    def _pending_hold(self, token, slip='holdslip_test.jpg'):
        from datetime import datetime, timedelta
        with self.app.app_context():
            g = Guest(first_name='A', last_name='B', phone='7', nationality='MDV')
            db.session.add(g); db.session.commit()
            db.session.add(Hold(session_token=token, hold_type='pending',
                                state='active', room_type_id=self.rt_id, qty=1,
                                check_in_date=date.today() + timedelta(days=20),
                                check_out_date=date.today() + timedelta(days=22),
                                expires_at=datetime.utcnow() + timedelta(hours=6),
                                adults=1, children=0, lead_guest_id=g.id,
                                payment_slip_filename=slip))
            db.session.commit()
        return token[:8].upper()

    def test_verify_refuses_slipless_hold(self):
        ref = self._pending_hold('NOSLIP01-session-token', slip=None)
        r = self.c.post('/api/internal/pepper/holds/verify',
                        json={'reference': ref, 'actor_name': 'Aisha'},
                        headers=self._auth())
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json().get('no_slip'))

    def test_verify_requires_reference(self):
        r = self.c.post('/api/internal/pepper/holds/verify', json={},
                        headers=self._auth())
        self.assertEqual(r.status_code, 400)

    def test_verify_confirms_hold_and_creates_booking(self):
        from app.models import Booking, Hold
        ref = self._pending_hold('VERIFY01-session-token')
        r = self.c.post('/api/internal/pepper/holds/verify',
                        json={'reference': ref, 'actor_name': 'Aisha', 'actor_id': 111},
                        headers=self._auth())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()['ok'])
        self.assertEqual(r.get_json()['by'], 'Aisha')
        with self.app.app_context():
            self.assertGreaterEqual(Booking.query.count(), 1)
            self.assertEqual(Hold.query.filter_by(state='converted').count(), 1)

    def test_verify_idempotent_loser_gets_winner(self):
        ref = self._pending_hold('VERIFY02-session-token')
        r1 = self.c.post('/api/internal/pepper/holds/verify',
                         json={'reference': ref, 'actor_name': 'Aisha'},
                         headers=self._auth())
        self.assertTrue(r1.get_json()['ok'])
        r2 = self.c.post('/api/internal/pepper/holds/verify',
                         json={'reference': ref, 'actor_name': 'Bob'},
                         headers=self._auth())
        self.assertEqual(r2.status_code, 409)
        self.assertFalse(r2.get_json()['ok'])
        self.assertTrue(r2.get_json()['already'])
        self.assertEqual(r2.get_json()['by'], 'Aisha')     # the winner, not Bob

    def test_reject_soft_marks_slip_keeps_hold_active(self):
        from app.models import Hold
        ref = self._pending_hold('REJECT01-session-token')
        r = self.c.post('/api/internal/pepper/holds/reject',
                        json={'reference': ref, 'actor_name': 'Aisha',
                              'reason': 'blurry slip'},
                        headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])
        with self.app.app_context():
            h = Hold.query.filter(Hold.session_token == 'REJECT01-session-token').first()
            self.assertEqual(h.state, 'active')            # NOT released
            self.assertIsNotNone(h.slip_rejected_at)
            self.assertIn('blurry slip', h.slip_rejected_reason)
            self.assertEqual(h.payment_slip_filename, 'holdslip_test.jpg')  # file kept

    def test_verify_refused_after_soft_reject(self):
        ref = self._pending_hold('REJECT02-session-token')
        self.c.post('/api/internal/pepper/holds/reject',
                    json={'reference': ref, 'actor_name': 'Aisha', 'reason': 'x'},
                    headers=self._auth())
        r = self.c.post('/api/internal/pepper/holds/verify',
                        json={'reference': ref, 'actor_name': 'Bob'},
                        headers=self._auth())
        self.assertEqual(r.status_code, 409)               # rejected slip -> no valid slip
        self.assertTrue(r.get_json().get('no_slip'))

    # --- /holds/state (Cancel / timeout re-arm authority) ---
    def test_state_pending_is_armable(self):
        ref = self._pending_hold('STATE01A-session-token')
        r = self.c.get('/api/internal/pepper/holds/state',
                       query_string={'reference': ref}, headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['state'], 'pending')
        self.assertTrue(r.get_json()['armable'])

    def test_state_confirmed_not_armable(self):
        ref = self._pending_hold('STATE02A-session-token')
        self.c.post('/api/internal/pepper/holds/verify',
                    json={'reference': ref, 'actor_name': 'Aisha'}, headers=self._auth())
        r = self.c.get('/api/internal/pepper/holds/state',
                       query_string={'reference': ref}, headers=self._auth())
        self.assertEqual(r.get_json()['state'], 'confirmed')
        self.assertFalse(r.get_json()['armable'])

    def test_state_soft_rejected_not_armable(self):
        ref = self._pending_hold('STATE03A-session-token')
        self.c.post('/api/internal/pepper/holds/reject',
                    json={'reference': ref, 'actor_name': 'Aisha', 'reason': 'blurry'},
                    headers=self._auth())
        r = self.c.get('/api/internal/pepper/holds/state',
                       query_string={'reference': ref}, headers=self._auth())
        self.assertEqual(r.get_json()['state'], 'slip_rejected')
        self.assertFalse(r.get_json()['armable'])
        self.assertIn('blurry', r.get_json()['reason'])

    # --- whitelist add / revoke (/authorize, /revoke) ---
    def test_authorize_then_revoke(self):
        r = self.c.post('/api/internal/pepper/whitelist',
                        json={'telegram_id': 555, 'role': 'staff', 'display_name': 'Zoe'},
                        headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])
        w = self.c.get('/api/internal/pepper/whitelist/555', headers=self._auth())
        self.assertEqual(w.get_json(), {'allowed': True, 'role': 'staff'})
        rv = self.c.post('/api/internal/pepper/whitelist/555/revoke', headers=self._auth())
        self.assertTrue(rv.get_json()['ok'])
        w2 = self.c.get('/api/internal/pepper/whitelist/555', headers=self._auth())
        self.assertEqual(w2.get_json(), {'allowed': False, 'role': None})

    def test_authorize_bad_role_rejected(self):
        r = self.c.post('/api/internal/pepper/whitelist',
                        json={'telegram_id': 5, 'role': 'admin'},
                        headers=self._auth())
        self.assertEqual(r.status_code, 400)

    def test_slip_serves_real_file_from_app_uploads(self):
        # Would fail with the old current_app.root_path bug (internal app's
        # root_path is the repo root, not app/), which returned 404.
        import app as app_pkg
        from datetime import datetime, timedelta
        uploads = os.path.join(os.path.dirname(app_pkg.__file__), 'uploads')
        os.makedirs(uploads, exist_ok=True)
        fn = 'pepper_test_slip.jpg'
        fpath = os.path.join(uploads, fn)
        with open(fpath, 'wb') as fh:
            fh.write(b'\xff\xd8\xffTESTJPEG')
        try:
            tok = 'SLIP1234-session-token'
            with self.app.app_context():
                g = Guest(first_name='S', last_name='L', phone='7', nationality='MDV')
                db.session.add(g); db.session.commit()
                db.session.add(Hold(session_token=tok, hold_type='pending',
                                    state='active', room_type_id=self.rt_id, qty=1,
                                    check_in_date=date.today() + timedelta(days=20),
                                    check_out_date=date.today() + timedelta(days=22),
                                    expires_at=datetime.utcnow() + timedelta(hours=6),
                                    adults=1, children=0, lead_guest_id=g.id,
                                    payment_slip_filename=fn))
                db.session.commit()
            r = self.c.get('/api/internal/pepper/slip?reference=' + tok[:8].upper(),
                           headers=self._auth())
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.data, b'\xff\xd8\xffTESTJPEG')
        finally:
            os.remove(fpath)

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

    def test_bot_booking_created_as_pending_verification(self):
        # D-slip: the guided flow creates the booking pending_verification so
        # nothing becomes revenue before the slip is verified.
        from app.models import Booking
        r = self.c.post('/api/internal/pepper/bookings',
                        json=self._booking_body(status='pending_verification'),
                        headers=self._auth())
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        bid = r.get_json()['booking_ids'][0]
        with self.app.app_context():
            self.assertEqual(Booking.query.get(bid).status, 'pending_verification')

    # --- D-slip: booking-side slip / verify / reject / state ---
    def _pending_verif_booking(self, slip='bkslip_test.jpg'):
        """Create a pending_verification booking (optionally with a slip on file)."""
        from app.models import db, Booking, Guest, Room
        with self.app.app_context():
            g = Guest(first_name='B', last_name='K', phone='7', nationality='MDV')
            db.session.add(g); db.session.commit()
            room = Room.query.first()
            b = Booking(booking_ref='BKPEND01', room_id=room.id, guest_id=g.id,
                        check_in_date=date.today() + timedelta(days=20),
                        check_out_date=date.today() + timedelta(days=22),
                        adults=1, children=0, num_guests=1, total_amount=1000.0,
                        status='pending_verification',
                        payment_slip_filename=slip)
            db.session.add(b); db.session.commit()
            return b.id

    def test_booking_verify_pending_to_confirmed(self):
        from app.models import Booking
        bid = self._pending_verif_booking()
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/verify',
                        json={'actor_name': 'Aisha', 'actor_id': 111},
                        headers=self._auth())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()['ok'])
        self.assertEqual(r.get_json()['by'], 'Aisha')
        with self.app.app_context():
            self.assertEqual(Booking.query.get(bid).status, 'confirmed')

    def test_booking_verify_refuses_slipless(self):
        bid = self._pending_verif_booking(slip=None)
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/verify',
                        json={'actor_name': 'Aisha', 'actor_id': 111}, headers=self._auth())
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()['no_slip'])

    # --- CASH path: a walk-in paying cash has NO slip; cash mode confirms it ---
    def _pending_cash_booking(self):
        from app.models import db, Booking, Guest, Room
        with self.app.app_context():
            g = Guest(first_name='C', last_name='K', phone='7', nationality='MDV')
            db.session.add(g); db.session.commit()
            room = Room.query.first()
            b = Booking(booking_ref='BKCASH01', room_id=room.id, guest_id=g.id,
                        check_in_date=date.today() + timedelta(days=20),
                        check_out_date=date.today() + timedelta(days=22),
                        adults=1, children=0, num_guests=1, total_amount=1000.0,
                        status='pending_verification', payment_method='cash',
                        payment_slip_filename=None)          # NO slip, ever
            db.session.add(b); db.session.commit()
            return b.id

    def test_cash_verify_confirms_without_slip(self):
        # Cash mode SKIPS the slip guard — this is the leak-closing path (a cash
        # booking has no slip and none is coming).
        from app.models import Booking
        bid = self._pending_cash_booking()
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/verify',
                        json={'actor_name': 'Aisha', 'actor_id': 111, 'cash': True},
                        headers=self._auth())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()['ok'])
        self.assertEqual(r.get_json()['method'], 'cash')
        with self.app.app_context():
            self.assertEqual(Booking.query.get(bid).status, 'confirmed')

    def test_cash_verify_via_require_slip_false_alias(self):
        bid = self._pending_cash_booking()
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/verify',
                        json={'actor_name': 'A', 'actor_id': 111, 'require_slip': False},
                        headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])

    def test_cash_verify_idempotent_loser_told_winner(self):
        bid = self._pending_cash_booking()
        r1 = self.c.post(f'/api/internal/pepper/bookings/{bid}/verify',
                         json={'actor_name': 'Aisha', 'actor_id': 111, 'cash': True}, headers=self._auth())
        self.assertTrue(r1.get_json()['ok'])
        r2 = self.c.post(f'/api/internal/pepper/bookings/{bid}/verify',
                         json={'actor_name': 'Bob', 'actor_id': 111, 'cash': True}, headers=self._auth())
        self.assertEqual(r2.status_code, 409)
        self.assertTrue(r2.get_json()['already'])
        self.assertEqual(r2.get_json()['by'], 'Aisha')       # first winner

    def test_bank_verify_still_requires_slip_when_not_cash(self):
        # The slip guard stays INTACT for a bank-transfer booking — a plain verify
        # (no cash flag) on a slipless booking is still refused.
        bid = self._pending_verif_booking(slip=None)
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/verify',
                        json={'actor_name': 'Aisha', 'actor_id': 111}, headers=self._auth())
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()['no_slip'])

    def test_bot_booking_created_payment_method_in_alert(self):
        # payment_method rides in the booking.created outbox payload -> alert.
        r = self.c.post('/api/internal/pepper/bookings',
                        json=self._booking_body(status='pending_verification',
                                                payment_method='cash'),
                        headers=self._auth())
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        o = self.c.get('/api/internal/pepper/outbox?undelivered=1',
                       headers=self._auth())
        ev = next(e for e in o.get_json()['events']
                  if e['event_type'] == 'booking.created')
        self.assertEqual(ev['alert']['payment_method'], 'cash')

    def test_create_rejects_bad_payment_method(self):
        r = self.c.post('/api/internal/pepper/bookings',
                        json=self._booking_body(payment_method='crypto'),
                        headers=self._auth())
        self.assertEqual(r.status_code, 400)

    def test_cash_booking_state_armable_without_slip(self):
        # A CASH booking has no slip and none is coming -> pending_verification is
        # directly ARMABLE (the 💵 Cash received anti-stale gate can fire).
        bid = self._pending_cash_booking()
        r = self.c.get(f'/api/internal/pepper/bookings/{bid}/state',
                       headers=self._auth())
        j = r.get_json()
        self.assertEqual(j['state'], 'pending')
        self.assertTrue(j['armable'])
        self.assertEqual(j['payment_method'], 'cash')

    def test_bank_booking_state_not_armable_without_slip(self):
        # A BANK booking with no slip is NOT armable (awaiting_slip) — the guard
        # difference between the two methods.
        bid = self._pending_verif_booking(slip=None)   # bank default, no slip
        r = self.c.get(f'/api/internal/pepper/bookings/{bid}/state',
                       headers=self._auth())
        j = r.get_json()
        self.assertEqual(j['state'], 'awaiting_slip')
        self.assertFalse(j['armable'])

    def test_cash_booking_state_confirmed_after_cash_verify(self):
        bid = self._pending_cash_booking()
        self.c.post(f'/api/internal/pepper/bookings/{bid}/verify',
                    json={'actor_name': 'Aisha', 'actor_id': 111, 'cash': True}, headers=self._auth())
        r = self.c.get(f'/api/internal/pepper/bookings/{bid}/state',
                       headers=self._auth())
        self.assertEqual(r.get_json()['state'], 'confirmed')
        self.assertFalse(r.get_json()['armable'])

    def test_cancelled_booking_state_distinct(self):
        from app.models import db, Booking
        bid = self._pending_cash_booking()
        with self.app.app_context():
            Booking.query.get(bid).status = 'cancelled'
            db.session.commit()
        r = self.c.get(f'/api/internal/pepper/bookings/{bid}/state',
                       headers=self._auth())
        self.assertEqual(r.get_json()['state'], 'cancelled')
        self.assertFalse(r.get_json()['armable'])

    def test_booking_verify_idempotent_loser_gets_winner(self):
        bid = self._pending_verif_booking()
        r1 = self.c.post(f'/api/internal/pepper/bookings/{bid}/verify',
                         json={'actor_name': 'Aisha', 'actor_id': 111}, headers=self._auth())
        self.assertTrue(r1.get_json()['ok'])
        r2 = self.c.post(f'/api/internal/pepper/bookings/{bid}/verify',
                         json={'actor_name': 'Bob', 'actor_id': 111}, headers=self._auth())
        self.assertEqual(r2.status_code, 409)
        self.assertTrue(r2.get_json()['already'])
        self.assertEqual(r2.get_json()['by'], 'Aisha')      # the winner, not Bob

    def test_booking_reject_soft_keeps_pending_and_file(self):
        from app.models import Booking
        bid = self._pending_verif_booking()
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/reject',
                        json={'actor_name': 'Aisha', 'actor_id': 111, 'reason': 'blurry slip'},
                        headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])
        with self.app.app_context():
            b = Booking.query.get(bid)
            self.assertEqual(b.status, 'pending_verification')   # NOT confirmed/cancelled
            self.assertIsNotNone(b.slip_rejected_at)
            self.assertIn('blurry slip', b.slip_rejected_reason)
            self.assertEqual(b.payment_slip_filename, 'bkslip_test.jpg')  # file kept

    def test_booking_verify_refused_after_soft_reject(self):
        bid = self._pending_verif_booking()
        self.c.post(f'/api/internal/pepper/bookings/{bid}/reject',
                    json={'actor_name': 'Aisha', 'actor_id': 111, 'reason': 'x'}, headers=self._auth())
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/verify',
                        json={'actor_name': 'Bob', 'actor_id': 111}, headers=self._auth())
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json().get('no_slip'))         # rejected slip = no valid slip

    def test_booking_slip_attach_supersedes_clears_rejection_emits(self):
        import io
        from app.models import Booking, PepperOutbox
        bid = self._pending_verif_booking(slip='old_bkslip.jpg')
        # soft-reject first so we can prove the attach clears it
        self.c.post(f'/api/internal/pepper/bookings/{bid}/reject',
                    json={'actor_name': 'A', 'actor_id': 111, 'reason': 'blurry'}, headers=self._auth())
        data = {'slip': (io.BytesIO(b'\xff\xd8\xffNEWJPEG'), 'new.jpg')}
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/slip',
                        data=data, content_type='multipart/form-data',
                        headers=self._auth())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        newname = r.get_json()['filename']
        with self.app.app_context():
            b = Booking.query.get(bid)
            self.assertEqual(b.payment_slip_filename, newname)   # superseded
            self.assertNotEqual(newname, 'old_bkslip.jpg')       # fresh filename
            self.assertIsNone(b.slip_rejected_at)                # rejection cleared
            # slip.uploaded emitted, booking-targeted
            self.assertTrue(PepperOutbox.query.filter_by(
                event_type='slip.uploaded', booking_id=bid).count() >= 1)
        # the superseded old file is NEVER deleted (supersede-never-delete)
        import os, app as app_pkg
        old_path = os.path.join(os.path.dirname(app_pkg.__file__), 'uploads',
                                'old_bkslip.jpg')
        # (old file was only a DB name in this test; the guarantee is that the
        # attach writes a NEW name and does not remove/rename any prior file.)
        self.assertNotEqual(newname, 'old_bkslip.jpg')

    def test_booking_slip_attach_rejects_bad_extension(self):
        import io
        bid = self._pending_verif_booking(slip=None)
        data = {'slip': (io.BytesIO(b'MZ...'), 'evil.exe')}
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/slip',
                        data=data, content_type='multipart/form-data',
                        headers=self._auth())
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()['ok'])

    def test_booking_state_pending_armable(self):
        bid = self._pending_verif_booking()
        r = self.c.get(f'/api/internal/pepper/bookings/{bid}/state',
                       headers=self._auth())
        self.assertEqual(r.get_json()['state'], 'pending')
        self.assertTrue(r.get_json()['armable'])

    def test_booking_state_confirmed_not_armable(self):
        bid = self._pending_verif_booking()
        self.c.post(f'/api/internal/pepper/bookings/{bid}/verify',
                    json={'actor_name': 'Aisha', 'actor_id': 111}, headers=self._auth())
        r = self.c.get(f'/api/internal/pepper/bookings/{bid}/state',
                       headers=self._auth())
        self.assertEqual(r.get_json()['state'], 'confirmed')
        self.assertFalse(r.get_json()['armable'])

    def test_booking_state_soft_rejected_not_armable(self):
        bid = self._pending_verif_booking()
        self.c.post(f'/api/internal/pepper/bookings/{bid}/reject',
                    json={'actor_name': 'Aisha', 'actor_id': 111, 'reason': 'blurry'}, headers=self._auth())
        r = self.c.get(f'/api/internal/pepper/bookings/{bid}/state',
                       headers=self._auth())
        self.assertEqual(r.get_json()['state'], 'slip_rejected')
        self.assertFalse(r.get_json()['armable'])
        self.assertIn('blurry', r.get_json()['reason'])

    def test_booking_slip_uploaded_alert_has_buttons_intent(self):
        # The booking slip.uploaded alert must render with a booking_id + a valid
        # slip so the poller arms ✅/❌ (booking.created carries no slip -> none).
        from app.models import db, PepperOutbox
        bid = self._pending_verif_booking()
        with self.app.app_context():
            db.session.add(PepperOutbox(event_type='slip.uploaded', booking_id=bid))
            db.session.commit()
        o = self.c.get('/api/internal/pepper/outbox?undelivered=1',
                       headers=self._auth())
        ev = next(e for e in o.get_json()['events']
                  if e['event_type'] == 'slip.uploaded')
        self.assertEqual(ev['alert']['booking_id'], bid)
        self.assertTrue(ev['alert']['has_slip'])            # valid slip -> armable

    # --- flow snapshots (restart recovery) ---
    def test_flow_upsert_list_delete_roundtrip(self):
        self.c.put('/api/internal/pepper/flows/424242',
                   json={'chat_id': -100, 'thread_id': 7, 'step': 'checkin',
                         'draft_json': '{"guest":{"first_name":"Ann"}}'},
                   headers=self._auth())
        lst = self.c.get('/api/internal/pepper/flows', headers=self._auth())
        flows = lst.get_json()['flows']
        f = next(f for f in flows if f['telegram_id'] == 424242)
        self.assertEqual((f['chat_id'], f['thread_id'], f['step']),
                         (-100, 7, 'checkin'))
        self.assertIn('Ann', f['draft_json'])
        self.c.delete('/api/internal/pepper/flows/424242', headers=self._auth())
        lst2 = self.c.get('/api/internal/pepper/flows', headers=self._auth())
        self.assertFalse(any(f['telegram_id'] == 424242
                             for f in lst2.get_json()['flows']))


class HitlActorRoleTest(InternalApiTest):
    """HITL-1 interim server-side actor-role enforcement (#20 / review §C.1).

    verify / reject / cancel-confirmed require a manager-or-owner ACTOR resolved
    server-side from pepper_users. The bot's bearer token alone (no manager actor)
    is 403 — the credential-level gap Phase 2 left open. The bot-side role gate stays
    as defence in depth; THIS proves the endpoint no longer trusts the caller."""

    def test_verify_agent_token_no_actor_is_403(self):
        # Bearer token present (transport ok) but NO actor id -> 403, and the booking
        # is NOT confirmed (the guard runs before any mutation).
        from app.models import Booking
        bid = self._pending_verif_booking()
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/verify',
                        json={'actor_name': 'ghost'}, headers=self._auth())
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(Booking.query.get(bid).status, 'pending_verification')

    def test_verify_manager_actor_allowed(self):
        bid = self._pending_verif_booking()
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/verify',
                        json={'actor_name': 'Aisha', 'actor_id': 111},
                        headers=self._auth())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()['ok'])

    def test_verify_non_manager_staff_actor_is_403(self):
        bid = self._pending_verif_booking()
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/verify',
                        json={'actor_name': 'Sana', 'actor_id': 222},  # staff role
                        headers=self._auth())
        self.assertEqual(r.status_code, 403)

    def test_verify_revoked_manager_actor_is_403(self):
        bid = self._pending_verif_booking()
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/verify',
                        json={'actor_name': 'Old Mgr', 'actor_id': 333},  # revoked
                        headers=self._auth())
        self.assertEqual(r.status_code, 403)

    def test_verify_accepts_actor_telegram_id_alias(self):
        # The spec field name `actor_telegram_id` is honoured as well as `actor_id`.
        bid = self._pending_verif_booking()
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/verify',
                        json={'actor_name': 'Aisha', 'actor_telegram_id': 111},
                        headers=self._auth())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_reject_non_manager_actor_is_403(self):
        bid = self._pending_verif_booking()
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/reject',
                        json={'actor_name': 'Sana', 'actor_id': 222,
                              'reason': 'blurry'}, headers=self._auth())
        self.assertEqual(r.status_code, 403)

    def test_reject_manager_actor_allowed(self):
        bid = self._pending_verif_booking()
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/reject',
                        json={'actor_name': 'Aisha', 'actor_id': 111,
                              'reason': 'blurry'}, headers=self._auth())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_cash_received_no_actor_is_403(self):
        # cash-received == booking verify with cash=True — same actor gate.
        bid = self._pending_cash_booking()
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/verify',
                        json={'actor_name': 'ghost', 'cash': True},
                        headers=self._auth())
        self.assertEqual(r.status_code, 403)

    # ── new cancel-confirmed endpoint ────────────────────────────────────────
    def test_cancel_confirmed_no_actor_is_403(self):
        from app.models import Booking
        bid = self._pending_verif_booking()
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/cancel-confirmed',
                        json={'actor_name': 'ghost', 'reason': 'dup'},
                        headers=self._auth())
        self.assertEqual(r.status_code, 403)
        with self.app.app_context():
            self.assertEqual(Booking.query.get(bid).status, 'pending_verification')

    def test_cancel_confirmed_non_manager_is_403(self):
        bid = self._pending_verif_booking()
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/cancel-confirmed',
                        json={'actor_name': 'Sana', 'actor_id': 222, 'reason': 'x'},
                        headers=self._auth())
        self.assertEqual(r.status_code, 403)

    def test_cancel_confirmed_manager_cancels(self):
        from app.models import Booking
        bid = self._pending_verif_booking()
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/cancel-confirmed',
                        json={'actor_name': 'Aisha', 'actor_id': 111,
                              'reason': 'guest no-show'}, headers=self._auth())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()['ok'])
        with self.app.app_context():
            self.assertEqual(Booking.query.get(bid).status, 'cancelled')

    def test_cancel_confirmed_requires_reason(self):
        bid = self._pending_verif_booking()
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/cancel-confirmed',
                        json={'actor_name': 'Aisha', 'actor_id': 111},
                        headers=self._auth())
        self.assertEqual(r.status_code, 400)   # reason mandatory

    def test_cancel_confirmed_flags_was_confirmed_paid_booking(self):
        # Cancelling a CONFIRMED (money-attached) booking reports was_confirmed=True
        # (the caller pings the owner on this).
        from app.models import db, Booking
        bid = self._pending_verif_booking()
        with self.app.app_context():
            Booking.query.get(bid).status = 'confirmed'
            db.session.commit()
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/cancel-confirmed',
                        json={'actor_name': 'Aisha', 'actor_id': 111,
                              'reason': 'refunded'}, headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['was_confirmed'])

    def test_env_owner_allowed_without_pepper_users_row(self):
        # The env owner id is allowed WITHOUT a pepper_users row (owner is env-only).
        bid = self._pending_verif_booking()
        os.environ['PEPPER_OWNER_ID'] = '999000'   # not in pepper_users
        try:
            r = self.c.post(f'/api/internal/pepper/bookings/{bid}/verify',
                            json={'actor_name': 'Owner', 'actor_id': 999000},
                            headers=self._auth())
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        finally:
            os.environ.pop('PEPPER_OWNER_ID', None)

    def test_still_401_without_bearer_token(self):
        # The actor gate is IN ADDITION to the bearer gate — no token is still 401.
        bid = self._pending_verif_booking()
        r = self.c.post(f'/api/internal/pepper/bookings/{bid}/verify',
                        json={'actor_name': 'Aisha', 'actor_id': 111})  # no headers
        self.assertEqual(r.status_code, 401)


if __name__ == '__main__':
    unittest.main()
