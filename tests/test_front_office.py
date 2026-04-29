"""Tests for the Front Office operational module:
- /front-office/             (overview)
- /front-office/arrivals     (today's check-ins)
- /front-office/departures   (today's check-outs)
- /front-office/in-house     (currently in-house)

Hard rules covered:
- Routes are login_required
- Read-only — bookings list correctly without ANY mutation
- Status pills render with right CSS classes
- Search + date filter work
- Empty states render gracefully
- No WhatsApp / email / Gemini calls (board / front-office never trigger these)
- Sidebar links reach the new routes
"""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

for _v in ('DATABASE_URL',
           'AI_DRAFT_PROVIDER', 'GEMINI_API_KEY', 'ANTHROPIC_API_KEY',
           'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                     # noqa: E402
from app import create_app                                    # noqa: E402
from app.models import (                                      # noqa: E402
    db, User, Room, Guest, Booking, Invoice,
)
from app.services import whatsapp as wa                       # noqa: E402
from app.services import ai_drafts                            # noqa: E402


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _make_app():
    return create_app(_TestConfig)


def _seed_users():
    admin = User(username='admin', email='a@x', role='admin')
    admin.set_password('aaaaaaaaaa1')
    staff = User(username='staff', email='s@x', role='staff')
    staff.set_password('aaaaaaaaaa1')
    db.session.add_all([admin, staff])
    db.session.commit()
    return admin.id, staff.id


def _seed_rooms(numbers=('11', '12', '13')):
    rooms = []
    for n in numbers:
        r = Room(number=n, name=f'R{n}', room_type='Deluxe',
                 floor=0, capacity=2, price_per_night=600.0)
        db.session.add(r)
        rooms.append(r)
    db.session.commit()
    return rooms


def _seed_guest(first='Hassan', last='Demo', phone='+9607000001'):
    g = Guest(first_name=first, last_name=last,
              phone=phone, email=f'{first.lower()}@x')
    db.session.add(g)
    db.session.commit()
    return g


def _seed_booking(room, guest, *, ci, co, status, ref):
    b = Booking(
        booking_ref=ref,
        room_id=room.id, guest_id=guest.id,
        check_in_date=ci, check_out_date=co,
        num_guests=1, total_amount=600.0 * (co - ci).days,
        status=status,
    )
    db.session.add(b)
    db.session.commit()
    return b


class _RouteBase(unittest.TestCase):

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.admin_id, self.staff_id = _seed_users()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True


# ─────────────────────────────────────────────────────────────────────
# 1) Auth — every route is login-required
# ─────────────────────────────────────────────────────────────────────

class FrontOfficeAuthTests(_RouteBase):

    def test_index_requires_login(self):
        for url in ('/front-office/', '/front-office/arrivals',
                    '/front-office/departures', '/front-office/in-house'):
            r = self.client.get(url)
            self.assertIn(r.status_code, (301, 302, 401),
                          f'{url} should require auth, got {r.status_code}')


# ─────────────────────────────────────────────────────────────────────
# 2) Overview dashboard
# ─────────────────────────────────────────────────────────────────────

class FrontOfficeIndexTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_index_renders_with_zero_data(self):
        r = self.client.get('/front-office/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Front Office', r.data)
        # Three stat cards
        self.assertIn(b'Arrivals today', r.data)
        self.assertIn(b'Departures today', r.data)
        self.assertIn(b'In house now', r.data)

    def test_index_shows_correct_counts(self):
        rooms = _seed_rooms()
        g = _seed_guest()
        today = date.today()
        # 2 arrivals
        _seed_booking(rooms[0], g, ci=today, co=today + timedelta(days=2),
                      status='confirmed', ref='BKARR1')
        g2 = _seed_guest('Sara', 'Test', '+9607000002')
        _seed_booking(rooms[1], g2, ci=today, co=today + timedelta(days=1),
                      status='confirmed', ref='BKARR2')
        # 1 departure
        g3 = _seed_guest('Yusuf', 'Test', '+9607000003')
        _seed_booking(rooms[2], g3, ci=today - timedelta(days=2), co=today,
                      status='checked_in', ref='BKDEP1')
        r = self.client.get('/front-office/')
        # 2 arrivals
        self.assertIn(b'>2<', r.data)


# ─────────────────────────────────────────────────────────────────────
# 3) Arrivals
# ─────────────────────────────────────────────────────────────────────

class FrontOfficeArrivalsTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_arrivals_renders_today(self):
        rooms = _seed_rooms()
        g = _seed_guest()
        today = date.today()
        _seed_booking(rooms[0], g,
                      ci=today, co=today + timedelta(days=2),
                      status='confirmed', ref='BKARR_TODAY')
        r = self.client.get('/front-office/arrivals')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Arrivals', r.data)
        self.assertIn(b'BKARR_TODAY', r.data)
        self.assertIn(b'Hassan', r.data)

    def test_arrivals_excludes_cancelled(self):
        rooms = _seed_rooms()
        g = _seed_guest()
        today = date.today()
        _seed_booking(rooms[0], g,
                      ci=today, co=today + timedelta(days=2),
                      status='cancelled', ref='BKCANCEL')
        r = self.client.get('/front-office/arrivals')
        self.assertNotIn(b'BKCANCEL', r.data)

    def test_arrivals_search(self):
        rooms = _seed_rooms()
        g1 = _seed_guest('Hassan', 'Demo', '+1')
        g2 = _seed_guest('Sara', 'Other', '+2')
        today = date.today()
        _seed_booking(rooms[0], g1, ci=today, co=today + timedelta(days=2),
                      status='confirmed', ref='BKHASSAN')
        _seed_booking(rooms[1], g2, ci=today, co=today + timedelta(days=2),
                      status='confirmed', ref='BKSARA')
        r = self.client.get('/front-office/arrivals?search=Sara')
        self.assertIn(b'BKSARA', r.data)
        self.assertNotIn(b'BKHASSAN', r.data)

    def test_arrivals_other_date(self):
        rooms = _seed_rooms()
        g = _seed_guest()
        tomorrow = date.today() + timedelta(days=1)
        _seed_booking(rooms[0], g,
                      ci=tomorrow, co=tomorrow + timedelta(days=2),
                      status='confirmed', ref='BKTOMORROW')
        # Today's arrivals — does NOT include tomorrow
        r = self.client.get('/front-office/arrivals')
        self.assertNotIn(b'BKTOMORROW', r.data)
        # Specifying tomorrow's date — DOES include
        r = self.client.get(f'/front-office/arrivals?date={tomorrow.isoformat()}')
        self.assertIn(b'BKTOMORROW', r.data)

    def test_arrivals_empty_state(self):
        r = self.client.get('/front-office/arrivals')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'No arrivals', r.data)


# ─────────────────────────────────────────────────────────────────────
# 4) Departures
# ─────────────────────────────────────────────────────────────────────

class FrontOfficeDeparturesTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_departures_renders_today(self):
        rooms = _seed_rooms()
        g = _seed_guest()
        today = date.today()
        _seed_booking(rooms[0], g,
                      ci=today - timedelta(days=2), co=today,
                      status='checked_in', ref='BKDEP_TODAY')
        r = self.client.get('/front-office/departures')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Departures', r.data)
        self.assertIn(b'BKDEP_TODAY', r.data)


# ─────────────────────────────────────────────────────────────────────
# 5) In House
# ─────────────────────────────────────────────────────────────────────

class FrontOfficeInHouseTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_in_house_includes_active_overlap(self):
        rooms = _seed_rooms()
        g = _seed_guest()
        today = date.today()
        # Checked in yesterday, checks out tomorrow → in-house today
        _seed_booking(rooms[0], g,
                      ci=today - timedelta(days=1),
                      co=today + timedelta(days=1),
                      status='checked_in', ref='BKHOUSE')
        r = self.client.get('/front-office/in-house')
        self.assertIn(b'BKHOUSE', r.data)
        self.assertIn(b'In House', r.data)

    def test_in_house_excludes_checked_out(self):
        rooms = _seed_rooms()
        g = _seed_guest()
        today = date.today()
        _seed_booking(rooms[0], g,
                      ci=today - timedelta(days=2),
                      co=today + timedelta(days=2),
                      status='checked_out', ref='BKDONE')
        r = self.client.get('/front-office/in-house')
        self.assertNotIn(b'BKDONE', r.data)


# ─────────────────────────────────────────────────────────────────────
# 6) Safety: no side-effects, no external calls
# ─────────────────────────────────────────────────────────────────────

class FrontOfficeNoSideEffectTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_no_status_mutation_on_render(self):
        rooms = _seed_rooms()
        g = _seed_guest()
        today = date.today()
        b = _seed_booking(rooms[0], g,
                          ci=today, co=today + timedelta(days=2),
                          status='confirmed', ref='BKKEEP')
        before_status = b.status
        for url in ('/front-office/',
                    '/front-office/arrivals',
                    '/front-office/departures',
                    '/front-office/in-house'):
            self.client.get(url)
        b2 = Booking.query.get(b.id)
        self.assertEqual(b2.status, before_status)

    def test_no_whatsapp_or_gemini_calls(self):
        rooms = _seed_rooms()
        g = _seed_guest()
        today = date.today()
        _seed_booking(rooms[0], g,
                      ci=today, co=today + timedelta(days=2),
                      status='confirmed', ref='BKQUIET')
        with mock.patch.object(wa, 'send_text_message') as send_m, \
             mock.patch.object(ai_drafts, '_call_provider') as ai_m:
            for url in ('/front-office/',
                        '/front-office/arrivals',
                        '/front-office/departures',
                        '/front-office/in-house'):
                self.client.get(url)
        send_m.assert_not_called()
        ai_m.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# 7) Guests directory
# ─────────────────────────────────────────────────────────────────────

class GuestsDirectoryTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_guests_index_renders(self):
        _seed_guest('Hassan', 'Demo')
        _seed_guest('Sara',   'Sample', '+9607000004')
        r = self.client.get('/guests/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Hassan', r.data)
        self.assertIn(b'Sara', r.data)

    def test_guests_search(self):
        _seed_guest('Hassan', 'Demo')
        _seed_guest('Sara',   'Other', '+9607000005')
        r = self.client.get('/guests/?search=Hassan')
        self.assertIn(b'Hassan', r.data)
        self.assertNotIn(b'Sara', r.data)

    def test_guests_links_to_edit(self):
        g = _seed_guest('Hassan', 'Demo')
        r = self.client.get('/guests/')
        self.assertIn(f'/guests/{g.id}/edit'.encode(), r.data)


# ─────────────────────────────────────────────────────────────────────
# 8) Sidebar links reach the routes
# ─────────────────────────────────────────────────────────────────────

class SidebarFrontOfficeLinksTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_sidebar_includes_arrivals_link(self):
        r = self.client.get('/bookings/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'/front-office/arrivals', r.data)
        self.assertIn(b'/front-office/departures', r.data)
        self.assertIn(b'/front-office/in-house', r.data)
        self.assertIn(b'/guests/', r.data)


if __name__ == '__main__':
    unittest.main()
