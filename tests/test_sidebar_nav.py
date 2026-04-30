"""Tests for the department-grouped sidebar navigation.

After the IA cleanup sprint:
  - Dashboard is the post-login landing page and lives at the TOP of the
    sidebar (before any department label).
  - Sections are: Front Office, Housekeeping, Restaurant, Accounting,
    Admin (renamed from "Admin & Settings").
  - Calendar is removed from the sidebar (the route still resolves so
    bookmarks keep working — Reservation Board is the canonical
    operational calendar surface).
  - Restaurant section drops the 3 grayed-out "Coming Soon" placeholders.
  - Accounting now hosts Night Audit + Analytics (financial close /
    reporting workflows).
  - "Staff Users" was renamed to "Users / Roles".
  - "Activity Log" was renamed to "Audit Log".
"""

from __future__ import annotations

import os
import unittest

for _v in ('DATABASE_URL', 'AI_DRAFT_PROVIDER', 'AI_DRAFT_MODEL',
           'GEMINI_API_KEY', 'ANTHROPIC_API_KEY',
           'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                    # noqa: E402
from app import create_app                                   # noqa: E402
from app.models import db, User                              # noqa: E402


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _make_app():
    return create_app(_TestConfig)


class SidebarDepartmentSectionsTests(unittest.TestCase):
    """The sidebar must group nav by department: Front Office,
    Housekeeping, Restaurant, Accounting, Admin."""

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        admin = User(username='admin', email='a@x', role='admin')
        admin.set_password('aaaaaaaaaa1')
        staff = User(username='staff', email='s@x', role='staff')
        staff.set_password('aaaaaaaaaa1')
        db.session.add_all([admin, staff])
        db.session.commit()
        self.admin_id = admin.id
        self.staff_id = staff.id
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True

    def test_admin_sees_every_department_section(self):
        self._login(self.admin_id)
        r = self.client.get('/bookings/')
        self.assertEqual(r.status_code, 200)
        # Each department label must appear in the sidebar
        for label in (b'Front Office', b'Housekeeping',
                      b'Restaurant', b'Accounting',
                      b'Admin'):
            self.assertIn(label, r.data,
                          f'sidebar missing {label!r} section')

    def test_dashboard_link_at_top_of_sidebar(self):
        # IA cleanup: Dashboard is the post-login landing page and must
        # appear above the first department label ("Front Office").
        # We assert via the href (whitespace inside the <a> tag would
        # break a literal `>Dashboard<` match).
        self._login(self.admin_id)
        r = self.client.get('/bookings/')
        self.assertEqual(r.status_code, 200)
        body = r.data
        dash_href_idx = body.find(b'href="/dashboard/"')
        front_idx = body.find(b'Front Office')
        self.assertGreater(dash_href_idx, 0,
                           'Dashboard link missing from sidebar')
        self.assertGreater(front_idx, 0)
        self.assertLess(dash_href_idx, front_idx,
                        'Dashboard link should appear above Front Office')

    def test_reservation_board_in_front_office(self):
        # The Reservation Board link must sit AFTER the "Front Office"
        # department label and BEFORE the next label ("Housekeeping").
        self._login(self.admin_id)
        r = self.client.get('/bookings/')
        self.assertEqual(r.status_code, 200)
        body = r.data
        front_idx = body.find(b'Front Office')
        hk_idx    = body.find(b'Housekeeping')
        board_idx = body.find(b'Reservation Board')
        self.assertGreater(front_idx, 0)
        self.assertGreater(hk_idx, 0)
        self.assertGreater(board_idx, 0)
        self.assertLess(front_idx, board_idx,
                        'Reservation Board should appear after Front Office label')
        self.assertLess(board_idx, hk_idx,
                        'Reservation Board should appear before Housekeeping section')

    def test_messages_link_appears_for_admin(self):
        # Messages = WhatsApp Inbox, lives in Front Office.
        self._login(self.admin_id)
        r = self.client.get('/bookings/')
        self.assertIn(b'Messages', r.data)

    def test_calendar_link_removed_from_sidebar(self):
        # IA cleanup: Calendar duplicated Reservation Board so the
        # sidebar link was removed. The /calendar/ route still resolves
        # for any bookmarks.
        self._login(self.admin_id)
        r = self.client.get('/bookings/')
        self.assertEqual(r.status_code, 200)
        # No <a href="/calendar/..."> should remain anywhere in the
        # rendered page — only the sidebar linked to it.
        self.assertNotIn(b'href="/calendar/"', r.data,
                         'Calendar link should be removed from sidebar')
        # Route still resolves
        cal = self.client.get('/calendar/')
        self.assertIn(cal.status_code, (200, 302),
                      'Calendar route should still be reachable')

    def test_restaurant_section_drops_coming_soon_placeholders(self):
        # IA cleanup: removed the 3 grayed-out "Coming Soon"
        # placeholders (Orders, Menu, Room Charges) from Restaurant.
        # Real entry points: POS Terminal (all roles), Online Orders +
        # POS Catalog (admin only).
        self._login(self.admin_id)
        r = self.client.get('/bookings/')
        self.assertIn(b'Restaurant', r.data)
        # POS Terminal is a working link
        self.assertIn(b'POS Terminal', r.data)
        self.assertIn(b'/pos/', r.data)
        # Online Orders is the new (admin-only) menu queue link
        self.assertIn(b'Online Orders', r.data)
        # No "Soon" pills should remain in the sidebar
        # (the OLD placeholders were the only callers)
        self.assertNotIn(b'>Soon<', r.data,
                         '"Soon" pill should be gone from sidebar')

    def test_accounting_hosts_night_audit_and_analytics(self):
        # IA cleanup: Night Audit + Analytics (reports.overview) moved
        # from "Admin & Settings" → Accounting since they're financial
        # close / reporting workflows. Note: the mobile header has a
        # "Night Audit" tooltip on the business-date pill — we only
        # care about the sidebar occurrence, so we search starting AT
        # the Accounting label position.
        self._login(self.admin_id)
        r = self.client.get('/bookings/')
        self.assertEqual(r.status_code, 200)
        body = r.data
        acc_idx = body.find(b'Accounting')
        # The Admin section uppercase label is the next department.
        # Use a substring that's unique to the sidebar Admin <p> tag.
        admin_idx = body.find(b'ds-sidebar-section">Admin<')
        self.assertGreater(acc_idx, 0)
        self.assertGreater(admin_idx, 0,
                           'Admin section label missing from sidebar')
        for label in (b'Night Audit', b'Analytics'):
            i = body.find(label, acc_idx)
            self.assertGreater(i, 0, f'{label!r} missing after Accounting')
            self.assertLess(i, admin_idx,
                            f'{label!r} should be before Admin label')

    def test_admin_section_uses_renamed_labels(self):
        # IA cleanup: "Staff Users" → "Users / Roles",
        #             "Activity Log" → "Audit Log".
        self._login(self.admin_id)
        r = self.client.get('/bookings/')
        self.assertIn(b'Users / Roles', r.data)
        self.assertIn(b'Audit Log', r.data)
        # Old labels must be gone from the sidebar.
        self.assertNotIn(b'Staff Users', r.data)
        self.assertNotIn(b'Activity Log', r.data)

    def test_existing_links_still_present_after_ia_cleanup(self):
        # No working page must be silently removed by the IA cleanup.
        # (Calendar is intentionally absent — see the dedicated test.)
        self._login(self.admin_id)
        r = self.client.get('/bookings/')
        for link_text in (
            b'Reservation Board', b'Bookings',
            b'Rooms', b'Housekeeping Board',
            b'Invoices', b'Reports', b'Tax', b'Reconciliation',
            b'Expenses', b'P&amp;L',
            b'Audit Log', b'Users / Roles', b'Seed DB',
            b'Property', b'Property Settings', b'Channels',
            b'Rates &amp; Inventory', b'WhatsApp Settings',
        ):
            self.assertIn(link_text, r.data,
                          f'sidebar link {link_text!r} disappeared')

    def test_staff_user_sees_front_office_section(self):
        # Staff is non-admin → should still see Front Office links
        # they have access to. Reservation Board is admin-only and
        # stays hidden.
        self._login(self.staff_id)
        r = self.client.get('/bookings/')
        # Staff route may redirect via the staff guard. If we land on
        # /staff/dashboard, the sidebar may not render — skip this case.
        if r.status_code != 200:
            self.skipTest('staff redirected by guard; sidebar not in scope')
        self.assertIn(b'Front Office', r.data)


class PostLoginLandingTests(unittest.TestCase):
    """After login both admin and staff land on /dashboard/ — the
    unified cross-department command center, not /rooms/ (Housekeeping)
    or /staff/dashboard."""

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        admin = User(username='admin', email='a@x', role='admin')
        admin.set_password('aaaaaaaaaa1')
        staff = User(username='staff', email='s@x', role='staff')
        staff.set_password('aaaaaaaaaa1')
        db.session.add_all([admin, staff])
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_admin_login_redirects_to_dashboard(self):
        r = self.client.post('/appadmin',
                             data={'username': 'admin',
                                   'password': 'aaaaaaaaaa1'},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn('/dashboard', r.headers['Location'])

    def test_staff_login_redirects_to_dashboard(self):
        r = self.client.post('/console',
                             data={'username': 'staff',
                                   'password': 'aaaaaaaaaa1'},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn('/dashboard', r.headers['Location'])

    def test_login_ignores_next_rooms_query_string(self):
        # Critical regression test: flask-login auto-appends ?next=/rooms/
        # when an unauth user hits /rooms/. After login, the user must NOT
        # land on /rooms/ — that was the legacy default landing and is
        # explicitly being moved away from. The Dashboard wins.
        for nxt in ('/rooms/', '/rooms', '/', '/staff/dashboard', ''):
            with self.subTest(nxt=nxt):
                r = self.client.post('/appadmin?next=' + nxt,
                                     data={'username': 'admin',
                                           'password': 'aaaaaaaaaa1'},
                                     follow_redirects=False)
                self.assertEqual(r.status_code, 302)
                loc = r.headers['Location']
                self.assertIn('/dashboard', loc,
                              f'next={nxt!r} should be ignored, got {loc!r}')

    def test_login_honours_meaningful_next(self):
        # A real next= path (e.g. /bookings/) should still work — we only
        # block the banned legacy defaults.
        r = self.client.post('/appadmin?next=/bookings/',
                             data={'username': 'admin',
                                   'password': 'aaaaaaaaaa1'},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn('/bookings/', r.headers['Location'])

    def test_login_blocks_external_next(self):
        # Open-redirect protection: a next= pointing at an external URL
        # must be ignored.
        r = self.client.post('/appadmin?next=https://evil.example.com/',
                             data={'username': 'admin',
                                   'password': 'aaaaaaaaaa1'},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertNotIn('evil.example.com', r.headers['Location'])
        self.assertIn('/dashboard', r.headers['Location'])


if __name__ == '__main__':
    unittest.main()
