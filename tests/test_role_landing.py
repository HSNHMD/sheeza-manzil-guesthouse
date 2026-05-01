"""Tests for the role-based landing dispatcher.

Covers:
  - services.landing.landing_endpoint_for / landing_url_for pure logic
  - POST /appadmin and POST /console redirect by role + department
  - ?next=<safe-path> still wins over the dispatcher
  - /rooms/ stays banned (regression guard from the prior sprint)
  - /admin/users department dropdown round-trip writes the column
  - Each landing target renders HTTP 200 for the authenticated user
  - Non-admin users cannot write other users' department field
  - No WhatsApp / Gemini calls anywhere in the code path
"""

from __future__ import annotations

import os
import unittest

for _v in ('DATABASE_URL', 'AI_DRAFT_PROVIDER', 'AI_DRAFT_MODEL',
           'GEMINI_API_KEY', 'ANTHROPIC_API_KEY',
           'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                  # noqa: E402
from app import create_app                                 # noqa: E402
from app.models import db, User                            # noqa: E402


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _make_user(*, username, role='staff', department=None,
               password='aaaaaaaaaa1'):
    u = User(username=username,
             email=f'{username}@example.com',
             role=role, department=department)
    u.set_password(password)
    db.session.add(u)
    db.session.commit()
    return u


# ── Pure dispatcher logic ──────────────────────────────────────────

class LandingDispatcherTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_admin_lands_on_dashboard_regardless_of_department(self):
        from app.services.landing import landing_endpoint_for, landing_url_for
        admin = _make_user(username='admin', role='admin',
                           department='restaurant')  # ← ignored for admins
        self.assertEqual(landing_endpoint_for(admin), 'dashboard.index')
        self.assertEqual(landing_url_for(admin), '/dashboard/')

    def test_front_office_staff_lands_on_front_office(self):
        from app.services.landing import landing_endpoint_for
        u = _make_user(username='fo', department='front_office')
        self.assertEqual(landing_endpoint_for(u), 'front_office.index')

    def test_housekeeping_staff_lands_on_housekeeping(self):
        from app.services.landing import landing_endpoint_for
        u = _make_user(username='hk', department='housekeeping')
        self.assertEqual(landing_endpoint_for(u), 'housekeeping.index')

    def test_restaurant_staff_lands_on_pos(self):
        from app.services.landing import landing_endpoint_for
        u = _make_user(username='rest', department='restaurant')
        self.assertEqual(landing_endpoint_for(u), 'pos.terminal')

    def test_accounting_staff_lands_on_accounting(self):
        from app.services.landing import landing_endpoint_for
        u = _make_user(username='acct', department='accounting')
        self.assertEqual(landing_endpoint_for(u), 'accounting.dashboard')

    def test_staff_without_department_falls_back_to_dashboard(self):
        from app.services.landing import landing_endpoint_for
        u = _make_user(username='noone')
        self.assertEqual(landing_endpoint_for(u), 'dashboard.index')

    def test_unknown_department_falls_back_to_dashboard(self):
        from app.services.landing import landing_endpoint_for
        u = _make_user(username='strange', department='spa')
        self.assertEqual(landing_endpoint_for(u), 'dashboard.index')

    def test_none_user_returns_dashboard(self):
        from app.services.landing import landing_endpoint_for
        self.assertEqual(landing_endpoint_for(None), 'dashboard.index')

    def test_describe_landing_returns_label(self):
        from app.services.landing import describe_landing
        u = _make_user(username='hk', department='housekeeping')
        ep, label = describe_landing(u)
        self.assertEqual(ep, 'housekeeping.index')
        self.assertEqual(label, 'Housekeeping Board')


# ── Login redirect end-to-end ──────────────────────────────────────

class LoginRedirectByRoleTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        # One user per role/department combination we test
        _make_user(username='admin',  role='admin')
        _make_user(username='fo',     department='front_office')
        _make_user(username='hk',     department='housekeeping')
        _make_user(username='rest',   department='restaurant')
        _make_user(username='acct',   department='accounting')
        _make_user(username='nodept')  # fallback
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, username, *, login_url='/appadmin'):
        return self.client.post(
            login_url,
            data={'username': username, 'password': 'aaaaaaaaaa1'},
            follow_redirects=False,
        )

    def test_admin_redirects_to_dashboard(self):
        r = self._login('admin')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/dashboard/', r.headers['Location'])

    def test_front_office_redirects_to_front_office(self):
        r = self._login('fo')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/front-office/', r.headers['Location'])

    def test_housekeeping_redirects_to_housekeeping(self):
        r = self._login('hk')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/housekeeping/', r.headers['Location'])

    def test_restaurant_redirects_to_pos(self):
        r = self._login('rest')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/pos/', r.headers['Location'])

    def test_accounting_redirects_to_accounting(self):
        r = self._login('acct')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/accounting/', r.headers['Location'])

    def test_nodept_falls_back_to_dashboard(self):
        r = self._login('nodept')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/dashboard/', r.headers['Location'])

    def test_console_login_uses_same_dispatcher(self):
        r = self._login('hk', login_url='/console')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/housekeeping/', r.headers['Location'])

    def test_safe_next_param_still_wins(self):
        # ?next=/bookings/ is honoured even though the dispatcher would
        # have sent the user to /front-office/.
        r = self.client.post(
            '/appadmin?next=/bookings/',
            data={'username': 'fo', 'password': 'aaaaaaaaaa1'},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        self.assertIn('/bookings/', r.headers['Location'])

    def test_rooms_next_still_blocked(self):
        # Regression guard: ?next=/rooms/ must still be ignored — it
        # was the legacy default landing the prior sprint banned.
        r = self.client.post(
            '/appadmin?next=/rooms/',
            data={'username': 'admin', 'password': 'aaaaaaaaaa1'},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        self.assertIn('/dashboard/', r.headers['Location'])
        self.assertNotIn('/rooms/', r.headers['Location'])


# ── Each resolved landing renders 200 ──────────────────────────────

class LandingPagesRenderTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        # Use an admin so the staff_guard doesn't redirect us away
        # from the department pages we want to assert render correctly.
        _make_user(username='admin', role='admin')
        self.client = self.app.test_client()
        self._login_admin()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login_admin(self):
        u = User.query.filter_by(username='admin').first()
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(u.id)
            sess['_fresh']   = True

    def test_dashboard_renders(self):
        r = self.client.get('/dashboard/')
        self.assertEqual(r.status_code, 200)

    def test_front_office_renders(self):
        r = self.client.get('/front-office/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Front Office', r.data)

    def test_housekeeping_renders(self):
        r = self.client.get('/housekeeping/')
        self.assertEqual(r.status_code, 200)

    def test_pos_terminal_renders(self):
        r = self.client.get('/pos/')
        self.assertEqual(r.status_code, 200)

    def test_accounting_renders(self):
        r = self.client.get('/accounting/')
        self.assertEqual(r.status_code, 200)


# ── /admin/users department CRUD ───────────────────────────────────

class AdminUsersDepartmentTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        admin = _make_user(username='admin', role='admin')
        target = _make_user(username='alice')
        self.admin_id = admin.id
        self.target_id = target.id
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(admin.id)
            sess['_fresh']   = True

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_create_with_department_persists(self):
        r = self.client.post(
            '/admin/users',
            data={'action': 'create',
                  'username': 'newhk',
                  'email': 'newhk@example.com',
                  'password': 'aaaaaaaaaa1',
                  'role': 'staff',
                  'department': 'housekeeping'},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (200, 302))
        u = User.query.filter_by(username='newhk').first()
        self.assertIsNotNone(u)
        self.assertEqual(u.department, 'housekeeping')

    def test_set_department_updates_existing_user(self):
        r = self.client.post(
            '/admin/users',
            data={'action': 'set_department',
                  'user_id': self.target_id,
                  'department': 'front_office'},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (200, 302))
        self.assertEqual(User.query.get(self.target_id).department,
                         'front_office')

    def test_set_department_to_none_clears_value(self):
        # Pre-set a department, then clear it via the empty option
        u = User.query.get(self.target_id)
        u.department = 'restaurant'
        db.session.commit()

        self.client.post(
            '/admin/users',
            data={'action': 'set_department',
                  'user_id': self.target_id,
                  'department': ''},
            follow_redirects=False,
        )
        self.assertIsNone(User.query.get(self.target_id).department)

    def test_unknown_department_rejected(self):
        self.client.post(
            '/admin/users',
            data={'action': 'set_department',
                  'user_id': self.target_id,
                  'department': 'spa'},  # not in DEPARTMENTS
            follow_redirects=False,
        )
        # Department stays NULL (or unchanged) — never the bogus value.
        self.assertNotEqual(User.query.get(self.target_id).department, 'spa')

    def test_non_admin_cannot_update_department(self):
        # Log in as the target staff user and try to update self
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.target_id)
            sess['_fresh']   = True
        r = self.client.post(
            '/admin/users',
            data={'action': 'set_department',
                  'user_id': self.target_id,
                  'department': 'accounting'},
            follow_redirects=False,
        )
        # Non-admin should be redirected away (302) OR see flash error.
        # Either way, the department must NOT have been written.
        self.assertNotEqual(User.query.get(self.target_id).department,
                            'accounting')

    def test_dept_dropdown_renders_in_users_page(self):
        # Visit /admin/users and confirm the new <select name="department">
        # appears in the page (per-row and in the create modal).
        r = self.client.get('/admin/users')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'name="department"', r.data)
        self.assertIn(b'Front Office', r.data)
        self.assertIn(b'Housekeeping', r.data)


# ── No external calls in the landing path ─────────────────────────

class StaffGuardDepartmentAccessTests(unittest.TestCase):
    """A non-admin staff user with a department must be able to actually
    USE the page their department lands on, not bounce off the staff_guard.

    These tests pin the staff_guard whitelist expansion done in this
    sprint:
      - front_office staff can load /front-office/, /bookings/,
        /guests/, /invoices/
      - housekeeping + restaurant staff already worked
      - accounting + admin-only paths still redirect (route-level
        @admin_required will 403 anyway, so the guard's bounce is the
        cleaner UX for V1)
    """

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        # One staff user per department we care about. NOT admin.
        self.fo  = _make_user(username='fo',  department='front_office').id
        self.hk  = _make_user(username='hk',  department='housekeeping').id
        self.rs  = _make_user(username='rs',  department='restaurant').id
        self.ac  = _make_user(username='ac',  department='accounting').id
        # Seed one room so /housekeeping/ has rows
        from app.models import Room
        db.session.add(Room(number='101', name='Standard Room',
                            room_type='Standard', floor=1, capacity=2,
                            price_per_night=800.0, is_active=True))
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True

    def _expect_ok(self, path):
        r = self.client.get(path, follow_redirects=False)
        self.assertEqual(
            r.status_code, 200,
            f'expected HTTP 200 for {path}, got {r.status_code}; '
            f'staff_guard probably bouncing the route to /staff/dashboard',
        )
        return r

    def _expect_bounced(self, path):
        # 302 to /staff/dashboard means the guard caught it (correct
        # for routes still gated to admin).
        r = self.client.get(path, follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn('/staff/dashboard', r.headers.get('Location', ''))
        return r

    # ── Front-office staff: can use front-office paths ────────────

    def test_front_office_staff_can_open_front_office_index(self):
        self._login(self.fo)
        r = self._expect_ok('/front-office/')
        self.assertIn(b'Front Office', r.data)

    def test_front_office_staff_can_open_arrivals(self):
        self._login(self.fo)
        self._expect_ok('/front-office/arrivals')

    def test_front_office_staff_can_open_bookings_list(self):
        self._login(self.fo)
        r = self._expect_ok('/bookings/')
        self.assertIn(b'Booking', r.data)  # page title or column header

    def test_front_office_staff_can_open_guests_list(self):
        self._login(self.fo)
        self._expect_ok('/guests/')

    def test_front_office_staff_can_open_invoices_list(self):
        self._login(self.fo)
        self._expect_ok('/invoices/')

    def test_front_office_staff_can_open_calendar(self):
        # Calendar is the legacy module; still reachable for any
        # bookmarks that survived the IA cleanup.
        self._login(self.fo)
        r = self.client.get('/calendar/', follow_redirects=False)
        # 200 (renders) or 302 (legacy redirect to /board) are both fine —
        # what we need is "NOT bounced to /staff/dashboard".
        self.assertNotEqual(r.status_code, 302,
            'calendar should not be bounced to /staff/dashboard') \
            if r.status_code == 302 and '/staff/dashboard' in r.headers.get('Location', '') \
            else None
        self.assertIn(r.status_code, (200, 302))

    # ── Housekeeping + restaurant staff (already worked) ──────────

    def test_housekeeping_staff_can_open_housekeeping(self):
        self._login(self.hk)
        self._expect_ok('/housekeeping/')

    def test_restaurant_staff_can_open_pos_terminal(self):
        self._login(self.rs)
        self._expect_ok('/pos/')

    # ── Admin-only paths: still bounced for non-admin ─────────────

    def test_accounting_staff_cannot_use_accounting(self):
        # /accounting/ has @admin_required. For V1 we acknowledge this
        # by leaving the guard to bounce the user. (When per-department
        # permissions land we'll let accounting staff in via a
        # principled check.)
        self._login(self.ac)
        self._expect_bounced('/accounting/')

    def test_non_admin_staff_cannot_use_reservation_board(self):
        # Reservation Board is admin-only at the route. Non-admin staff
        # who somehow hit it (e.g. via stale link) get cleanly bounced.
        self._login(self.fo)
        self._expect_bounced('/board')

    def test_non_admin_staff_cannot_use_reports(self):
        self._login(self.fo)
        self._expect_bounced('/reports/')


class NoExternalCallsTests(unittest.TestCase):
    """Sanity check: the landing dispatcher must not import or call
    anything that talks to WhatsApp / Gemini / external APIs. This is a
    static import check — if a future edit pulled in the wrong module,
    this test would fail before it could ship to staging."""

    def test_landing_module_imports_no_externals(self):
        import importlib, inspect
        m = importlib.import_module('app.services.landing')
        src = inspect.getsource(m)
        for banned in ('whatsapp', 'gemini', 'requests.', 'urllib.request',
                       'send_email'):
            self.assertNotIn(banned, src.lower(),
                             f'app.services.landing must not reference {banned!r}')


if __name__ == '__main__':
    unittest.main()
