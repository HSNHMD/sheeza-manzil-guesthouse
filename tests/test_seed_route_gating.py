"""Seed route (/admin/seed) is env-gated: absent by default.

The room-seed route is a dev/staging convenience. In production it must not
exist at all (404, not 403). It registers only when ENABLE_SEED_ROUTES=true.
These tests assert the default (flag unset) behaviour: route absent, endpoint
not in the URL map, and — critically — pages that extend base.html still render
(the seed nav link is guarded, so no url_for BuildError).
"""

from __future__ import annotations

import os
import unittest

# Ensure the flag is UNSET before app import (registration happens at import).
os.environ.pop('ENABLE_SEED_ROUTES', None)
for _v in ('DATABASE_URL', 'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                        # noqa: E402
from app import create_app                                       # noqa: E402
from app.models import db, User                                  # noqa: E402


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False
    ENABLE_SEED_ROUTES = False


class SeedRouteGatingTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        admin = User(username='seed_admin', email='a@x', role='admin')
        admin.set_password('aaaaaaaaaa1')
        db.session.add(admin)
        db.session.commit()
        self.admin_id = admin.id
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True

    def test_seed_route_absent_by_default(self):
        self._login(self.admin_id)
        r = self.client.get('/admin/seed')
        self.assertEqual(r.status_code, 404)

    def test_seed_endpoint_not_in_url_map(self):
        endpoints = {rule.endpoint for rule in self.app.url_map.iter_rules()}
        self.assertNotIn('auth.seed', endpoints)

    def test_base_template_renders_without_seed_route(self):
        # A page that extends base.html must still render: the seed nav link is
        # guarded by config.ENABLE_SEED_ROUTES, so no url_for('auth.seed') BuildError.
        self._login(self.admin_id)
        r = self.client.get('/bookings/')
        self.assertEqual(r.status_code, 200)


if __name__ == '__main__':
    unittest.main()
