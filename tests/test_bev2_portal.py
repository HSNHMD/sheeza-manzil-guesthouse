"""Booking Engine V2 Phase 2 — portal integration tests (B6 blocking).

Full flow: search 2 types -> selection holds -> guest form/submit -> pending
group -> admin confirm -> group + master folio + assigned rooms. Plus the
expiry paths and the anti-abuse rule. SQLite; messaging off; tmp-safe.
The endpoint-level race is in test_bev2_race.py (Postgres).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta, datetime

for _v in ('DATABASE_URL', 'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN',
           'AI_DRAFT_PROVIDER', 'GEMINI_API_KEY', 'ANTHROPIC_API_KEY'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                          # noqa: E402
from app import create_app                                        # noqa: E402
from app.models import (db, User, Room, Guest, Booking, RoomType,  # noqa: E402
                        Hold, BookingGroup, Property)
from app.services import portal as portal_svc, holds as holds_svc  # noqa: E402

_CI = date.today() + timedelta(days=14)
_CO = _CI + timedelta(days=2)


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


class _Base(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        db.session.add(Property(code='default', name='Test Property'))
        db.session.commit()
        self.admin = User(username='pa', email='a@x', role='admin')
        self.admin.set_password('aaaaaaaaaa1')
        db.session.add(self.admin); db.session.commit()
        self.t1 = self._type('STD', 'Standard', 2, 10)
        self.t2 = self._type('DLX', 'Deluxe', 1, 20)
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def _type(self, code, name, rooms, base):
        rt = RoomType(code=code, name=name, max_occupancy=2, base_capacity=2, is_active=True)
        db.session.add(rt); db.session.commit()
        for i in range(rooms):
            db.session.add(Room(number=f'{base+i}', name='T', room_type=name,
                                room_type_id=rt.id, floor=1, capacity=2,
                                price_per_night=500.0, status='available',
                                housekeeping_status='clean'))
        db.session.commit()
        return rt

    def _admin_login(self):
        # fresh client = the admin actor (not carrying the guest's portal cookie)
        self.admin_client = self.app.test_client()
        with self.admin_client.session_transaction() as s:
            s['_user_id'] = str(self.admin.id); s['_fresh'] = True


class PortalEndpointFlow(unittest.TestCase):
    """Full guest flow through the ACTUAL /book endpoints. Uses a tmp-FILE DB and
    fresh app-contexts (not a persistent one) so test_client request commits are
    visible — the working pattern; a persistent :memory: context interleaves
    request/test sessions on one connection."""

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix='.db')

        class _Cfg(_TestConfig):
            SQLALCHEMY_DATABASE_URI = f'sqlite:///{self._path}'
        self.app = create_app(_Cfg)
        with self.app.app_context():
            db.create_all()
            db.session.add(Property(code='default', name='P'))
            db.session.commit()
            ad = User(username='pa', email='a@x', role='admin')
            ad.set_password('aaaaaaaaaa1')
            db.session.add(ad); db.session.commit()
            self.admin_id = ad.id
            self.t1 = self._seed_type('STD', 'Standard', 2, 10)
            self.t2 = self._seed_type('DLX', 'Deluxe', 1, 20)
        self.client = self.app.test_client()

    def tearDown(self):
        with self.app.app_context():
            db.drop_all()
        os.close(self._fd)
        try:
            os.remove(self._path)
        except OSError:
            pass

    def _seed_type(self, code, name, n, base):
        rt = RoomType(code=code, name=name, max_occupancy=2, base_capacity=2, is_active=True)
        db.session.add(rt); db.session.commit()
        for i in range(n):
            db.session.add(Room(number=str(base + i), name='T', room_type=name,
                                room_type_id=rt.id, floor=1, capacity=2,
                                price_per_night=500.0, status='available',
                                housekeeping_status='clean'))
        db.session.commit()
        return rt.id

    def test_full_flow_search_to_confirmed_group(self):
        self.assertEqual(self.client.get(
            f'/book/?check_in={_CI}&check_out={_CO}').status_code, 200)
        r = self.client.post('/book/hold', data={
            'check_in': _CI.isoformat(), 'check_out': _CO.isoformat(),
            f'qty_{self.t1}': '1', f'qty_{self.t2}': '1'})
        self.assertEqual(r.status_code, 302)
        self.assertIn('/book/guest', r.headers.get('Location', ''))
        self.assertEqual(self.client.get('/book/guest').status_code, 200)
        r = self.client.post('/book/submit', data={
            'first_name': 'Test', 'last_name': 'Guest', 'phone': '+9600000000'})
        self.assertEqual(r.status_code, 302)
        self.assertIn('/book/status', r.headers.get('Location', ''))
        self.assertEqual(self.client.get('/book/status').status_code, 200)

        with self.app.app_context():
            self.assertEqual(Hold.query.filter_by(hold_type='pending', state='active').count(), 2)
            pend = Hold.query.filter_by(hold_type='pending', state='active').first()
            conf = holds_svc.confirm_group(pend.session_token, user_id=self.admin_id)
            self.assertTrue(conf['ok'], conf.get('reasons'))
            self.assertEqual(BookingGroup.query.count(), 1)
            grp = BookingGroup.query.first()
            self.assertIsNotNone(grp.master_booking_id)
            bks = Booking.query.filter_by(booking_group_id=grp.id).all()
            self.assertEqual(len(bks), 2)
            self.assertTrue(all(b.room_id and b.status == 'confirmed' for b in bks))
            self.assertEqual(Hold.query.filter_by(state='converted').count(), 2)


class AdminConfirmRoute(_Base):
    """The admin confirm ROUTE (fresh admin client, no guest cookies) confirms a
    pending group created directly — covers the route auth + wiring."""
    def test_confirm_route_confirms_group(self):
        from app.services import portal as ps
        tok = 'route-token'
        ps.create_holds([{'room_type_id': self.t1.id, 'qty': 1},
                         {'room_type_id': self.t2.id, 'qty': 1}], _CI, _CO, tok)
        ps.submit(tok, {'first_name': 'R', 'last_name': 'T', 'phone': '+960'})
        pend = Hold.query.filter_by(hold_type='pending', state='active').first()
        c = self.app.test_client()
        with c.session_transaction() as s:
            s['_user_id'] = str(self.admin.id); s['_fresh'] = True
        self.assertEqual(c.get('/admin/holds/').status_code, 200)
        r = c.post(f'/admin/holds/{pend.id}/confirm')
        self.assertIn(r.status_code, (302, 200))
        db.session.remove()
        self.assertEqual(Hold.query.filter_by(state='converted').count(), 2)

    def assertin_redirect(self, resp, path):
        self.assertEqual(resp.status_code, 302)
        self.assertIn(path, resp.headers.get('Location', ''))


class PortalServiceRules(_Base):
    def test_single_type_qty1_makes_plain_booking_no_group(self):
        tok = 'tok-single'
        res = portal_svc.create_holds([{'room_type_id': self.t2.id, 'qty': 1}],
                                      _CI, _CO, tok)
        self.assertTrue(res['ok'])
        portal_svc.submit(tok, {'first_name': 'A', 'last_name': 'B', 'phone': '+9600'})
        conf = holds_svc.confirm_group(tok, user_id=self.admin.id)
        self.assertTrue(conf['ok'], conf.get('reasons'))
        self.assertIsNone(conf['group_id'])                  # plain booking, no group
        self.assertEqual(len(conf['booking_ids']), 1)
        self.assertEqual(BookingGroup.query.count(), 0)

    def test_anti_abuse_one_active_selection_group_per_session(self):
        tok = 'tok-reselect'
        portal_svc.create_holds([{'room_type_id': self.t1.id, 'qty': 1}], _CI, _CO, tok)
        first = holds_svc.holds_for_session(tok, hold_type='selection', state='active')
        self.assertEqual(len(first), 1)
        # re-select -> the prior selection is released (audited), only new remains
        portal_svc.create_holds([{'room_type_id': self.t1.id, 'qty': 2}], _CI, _CO, tok)
        active = holds_svc.holds_for_session(tok, hold_type='selection', state='active')
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].qty, 2)
        self.assertEqual(Hold.query.filter_by(state='released').count(), 1)

    def test_selection_expiry_blocks_submit(self):
        tok = 'tok-exp'
        portal_svc.create_holds([{'room_type_id': self.t1.id, 'qty': 1}], _CI, _CO, tok)
        for h in holds_svc.holds_for_session(tok, hold_type='selection', state='active'):
            h.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        res = portal_svc.submit(tok, {'first_name': 'A', 'last_name': 'B', 'phone': '+960'})
        self.assertFalse(res['ok'])                          # dead hold -> no submit

    def test_pending_expiry_status_is_rebook(self):
        tok = 'tok-pexp'
        portal_svc.create_holds([{'room_type_id': self.t1.id, 'qty': 1}], _CI, _CO, tok)
        portal_svc.submit(tok, {'first_name': 'A', 'last_name': 'B', 'phone': '+960'})
        for h in holds_svc.holds_for_session(tok, hold_type='pending', state='active'):
            h.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        self.assertEqual(portal_svc.status(tok)['state'], 'expired')

    def test_slip_transfers_to_booking_on_confirm(self):
        tok = 'tok-slip'
        portal_svc.create_holds([{'room_type_id': self.t2.id, 'qty': 1}], _CI, _CO, tok)
        portal_svc.submit(tok, {'first_name': 'S', 'last_name': 'L', 'phone': '+960'},
                          slip_filename='slip123.png',
                          slip_drive_id='payment-slips/slip123.png')
        h = holds_svc.holds_for_session(tok, hold_type='pending', state='active')[0]
        self.assertEqual(h.payment_slip_filename, 'slip123.png')       # on the hold
        conf = holds_svc.confirm_group(tok, user_id=self.admin.id)
        self.assertTrue(conf['ok'], conf.get('reasons'))
        b = Booking.query.get(conf['booking_ids'][0])
        # slip reference handed off to the booking -> findable via the normal
        # (admin-gated) booking UI after the hold archives
        self.assertEqual(b.payment_slip_filename, 'slip123.png')
        self.assertEqual(b.payment_slip_drive_id, 'payment-slips/slip123.png')

    def test_search_never_exposes_room_numbers(self):
        cards = portal_svc.search(_CI, _CO)
        # cards carry type + counts only; no room object/number leaks
        for c in cards:
            self.assertNotIn('room', {k.lower() for k in c.keys()} - {'room_type'})
            self.assertIn('available_qty', c)


if __name__ == '__main__':
    unittest.main()
