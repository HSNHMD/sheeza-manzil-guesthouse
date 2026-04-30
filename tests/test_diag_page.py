"""Tests for /admin/diag — the deployment proof page.

Why these tests exist:
  Operators rely on /admin/diag to verify a staging deploy actually
  landed. If the page silently breaks (broken url_for, blueprint not
  registered, template missing), they'd be diagnosing the diagnostic
  itself. These tests make sure the page renders, gates on admin, and
  surfaces the brand row + sidebar structure.
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


class DiagPageTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
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

    def test_admin_can_load_diag_page(self):
        self._login(self.admin_id)
        r = self.client.get('/admin/diag')
        self.assertEqual(r.status_code, 200)
        # Surfaces the data points operators care about
        for needle in (b'Deployment diag', b'PropertySettings.id',
                       b'design-system.css', b'login redirect',
                       b'Sidebar'):
            self.assertIn(needle, r.data, f'missing {needle!r}')

    def test_staff_user_blocked_by_guard(self):
        # Non-admins are bounced by the staff_guard before the route runs.
        # The page lives under /admin/diag which IS whitelisted, but the
        # @admin gate inside the route returns 403.
        self._login(self.staff_id)
        r = self.client.get('/admin/diag', follow_redirects=False)
        # Either a redirect (guard) or 403 (route check) is acceptable.
        self.assertIn(r.status_code, (302, 403))

    def test_unauthenticated_redirected_to_login(self):
        r = self.client.get('/admin/diag', follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn('console', r.headers.get('Location', '') + '/appadmin')

    def test_diag_link_in_sidebar_for_admin(self):
        # The Diag link should appear in the Admin section so operators
        # can find it without typing the URL.
        self._login(self.admin_id)
        r = self.client.get('/bookings/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Deploy Diag', r.data)
        self.assertIn(b'/admin/diag', r.data)


class StagingRibbonTests(unittest.TestCase):
    """The orange STAGING ribbon must show only when STAGING=1."""

    def setUp(self):
        self.prev = os.environ.get('STAGING')

    def tearDown(self):
        if self.prev is None:
            os.environ.pop('STAGING', None)
        else:
            os.environ['STAGING'] = self.prev

    def _login_admin(self, app):
        with app.app_context():
            db.create_all()
            u = User(username='admin', email='a@x', role='admin')
            u.set_password('aaaaaaaaaa1')
            db.session.add(u)
            db.session.commit()
            client = app.test_client()
            with client.session_transaction() as sess:
                sess['_user_id'] = str(u.id)
                sess['_fresh'] = True
            return client

    def test_ribbon_visible_when_staging_env_set(self):
        os.environ['STAGING'] = '1'
        app = create_app(_TestConfig)
        client = self._login_admin(app)
        r = client.get('/dashboard/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'STAGING', r.data)
        self.assertIn(b'build ', r.data)

    def test_ribbon_hidden_when_staging_env_unset(self):
        os.environ.pop('STAGING', None)
        app = create_app(_TestConfig)
        client = self._login_admin(app)
        r = client.get('/dashboard/')
        self.assertEqual(r.status_code, 200)
        # Note: the word "STAGING" can appear elsewhere; we look for the
        # specific ribbon text "STAGING · build".
        self.assertNotIn(b'STAGING \xc2\xb7 build', r.data)


class CacheBustTests(unittest.TestCase):
    """The design-system.css link must carry a ?v=<sha> query param so
    a deploy never lands silently behind a cached asset."""

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        u = User(username='admin', email='a@x', role='admin')
        u.set_password('aaaaaaaaaa1')
        db.session.add(u)
        db.session.commit()
        self.uid = u.id
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(u.id)
            sess['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_css_link_has_version_query(self):
        r = self.client.get('/dashboard/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'design-system.css?v=', r.data)


if __name__ == '__main__':
    unittest.main()
