"""Sidebar 'Holds ⏳' nav link + live-pending badge (Phase 0 follow-up).

The pending-holds admin screen (/admin/holds/) existed but was orphaned from the
nav. These tests lock in: the link is present for admins, and the badge reflects
the count of LIVE pending holds (hidden when zero).
"""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta

os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                          # noqa: E402
from app import create_app                                        # noqa: E402
from app.models import db, User, RoomType, Room, Property, Hold    # noqa: E402


class _Cfg(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


class HoldsNavTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_Cfg)
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        db.session.add(Property(code='default', name='P'))
        self.admin = User(username='pa', email='a@x', role='admin')
        self.admin.set_password('aaaaaaaaaa1')
        db.session.add(self.admin)
        rt = RoomType(code='STD', name='Standard', max_occupancy=2,
                      base_capacity=2, is_active=True)
        db.session.add(rt); db.session.commit()
        self.rt_id = rt.id
        db.session.add(Room(number='1', name='T', room_type='Standard',
                            room_type_id=rt.id, floor=1, capacity=2,
                            price_per_night=500.0, status='available',
                            housekeeping_status='clean'))
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as s:
            s['_user_id'] = str(self.admin.id); s['_fresh'] = True

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def _add_pending_hold(self, live=True):
        exp = datetime.utcnow() + (timedelta(hours=6) if live
                                   else timedelta(hours=-1))
        db.session.add(Hold(session_token='t', hold_type='pending',
                            state='active', room_type_id=self.rt_id, qty=1,
                            check_in_date=date.today() + timedelta(days=5),
                            check_out_date=date.today() + timedelta(days=6),
                            expires_at=exp, adults=1, children=0))
        db.session.commit()

    def test_nav_link_present_for_admin(self):
        html = self.client.get('/admin/holds/').get_data(as_text=True)
        self.assertIn('/admin/holds/', html)
        self.assertIn('Holds', html)

    def test_badge_shows_live_pending_count(self):
        self._add_pending_hold(live=True)
        self._add_pending_hold(live=True)
        html = self.client.get('/admin/holds/').get_data(as_text=True)
        # context processor exposed the count; badge markup rendered
        self.assertIn('pending', html.lower())
        # the badge span carries the numeric count (2)
        self.assertRegex(html, r'rounded-full[^>]*>\s*2\s*<')

    def test_badge_hidden_when_zero(self):
        html = self.client.get('/admin/holds/').get_data(as_text=True)
        # no amber count badge span when there are no live pending holds
        self.assertNotRegex(html, r'bg-amber-100 text-amber-700 border '
                                  r'border-amber-200[^>]*>\s*[1-9]')

    def test_expired_pending_not_counted(self):
        self._add_pending_hold(live=False)   # expired -> not live -> not counted
        html = self.client.get('/admin/holds/').get_data(as_text=True)
        self.assertNotRegex(html, r'bg-amber-100 text-amber-700 border '
                                  r'border-amber-200[^>]*>\s*[1-9]')


if __name__ == '__main__':
    unittest.main()
