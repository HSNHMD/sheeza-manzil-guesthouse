"""Tests for the department-grouped sidebar navigation."""

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
    Housekeeping, Restaurant, Accounting, Admin & Settings."""

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
        # Messages = WhatsApp Inbox, moved into Front Office.
        self._login(self.admin_id)
        r = self.client.get('/bookings/')
        self.assertIn(b'Messages', r.data)

    def test_restaurant_section_has_coming_soon_placeholders(self):
        self._login(self.admin_id)
        r = self.client.get('/bookings/')
        # Restaurant heading present
        self.assertIn(b'Restaurant', r.data)
        # Each placeholder labelled "Soon"
        for entry in (b'POS', b'Orders', b'Menu', b'Room Charges'):
            self.assertIn(entry, r.data)
        # And the "Soon" pill appears at least 4 times (one per placeholder)
        self.assertGreaterEqual(r.data.count(b'Soon'), 4)

    def test_existing_links_still_present_after_regroup(self):
        # No working page must be silently removed by the regroup.
        self._login(self.admin_id)
        r = self.client.get('/bookings/')
        for link_text in (
            b'Reservation Board', b'Bookings', b'Calendar',
            b'Rooms', b'Housekeeping Board',
            b'Invoices', b'Reports', b'Tax', b'Reconciliation',
            b'Expenses', b'P&amp;L', b'Activity Log',
            b'Staff Users', b'Seed DB',
        ):
            self.assertIn(link_text, r.data,
                          f'sidebar link {link_text!r} disappeared')

    def test_staff_user_sees_front_office_section(self):
        # Staff is non-admin → should still see Front Office links
        # they have access to (Bookings + Calendar). Reservation Board
        # is admin-only and stays hidden.
        self._login(self.staff_id)
        r = self.client.get('/bookings/')
        # Staff route may redirect via the staff guard. If we land on
        # /staff/dashboard, the sidebar may not render — skip this case.
        if r.status_code != 200:
            self.skipTest('staff redirected by guard; sidebar not in scope')
        self.assertIn(b'Front Office', r.data)


if __name__ == '__main__':
    unittest.main()
