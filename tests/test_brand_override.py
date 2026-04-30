"""Tests for the BRAND_*_OVERRIDE env-var escape hatch.

Why this exists: staging needs to display a different property name
than the DB row, without a DB write that could leak into production
backups or audit logs. The override layer in services/branding.py:
get_brand() lets the operator drop a line in .env and instantly
rebrand the visible chrome.

These tests pin three things:
  1. When BRAND_NAME_OVERRIDE is set, get_brand()['name'] returns it
     even though the DB row says something else.
  2. When the override is unset, the DB value still wins (so
     production is never accidentally affected).
  3. The override flows through to base.html (header wordmark) and
     the /healthz JSON probe.
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
from app.models import db, User, PropertySettings            # noqa: E402


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _seed_property(short='Sheeza Manzil',
                   name='Sheeza Manzil Guesthouse'):
    """Force the singleton PropertySettings row to a known state."""
    s = PropertySettings.query.first()
    if s is None:
        s = PropertySettings(property_name=name, short_name=short,
                             primary_color='#7B3F00',
                             logo_path='/static/img/logo.png',
                             currency_code='USD',
                             timezone='Indian/Maldives',
                             is_active=True)
        db.session.add(s)
    else:
        s.property_name = name
        s.short_name = short
    db.session.commit()


class BrandOverrideTests(unittest.TestCase):

    def setUp(self):
        # Snapshot env so mutations don't leak between tests
        self._snap = {k: os.environ.get(k) for k in
                      ('BRAND_NAME_OVERRIDE',
                       'BRAND_SHORT_NAME_OVERRIDE',
                       'BRAND_PRIMARY_COLOR_OVERRIDE')}
        for k in self._snap:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._snap.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_no_override_returns_db_value(self):
        os.environ.pop('BRAND_NAME_OVERRIDE', None)
        app = create_app(_TestConfig)
        with app.app_context():
            db.create_all()
            _seed_property(name='Sheeza Manzil Guesthouse',
                           short='Sheeza Manzil')
            from app.services.branding import get_brand
            b = get_brand()
            self.assertEqual(b['name'], 'Sheeza Manzil Guesthouse')
            self.assertEqual(b['short_name'], 'Sheeza Manzil')

    def test_override_wins_over_db_value(self):
        os.environ['BRAND_NAME_OVERRIDE'] = 'Maakanaa Village Hotel'
        os.environ['BRAND_SHORT_NAME_OVERRIDE'] = 'Maakanaa'
        app = create_app(_TestConfig)
        with app.app_context():
            db.create_all()
            _seed_property(name='Sheeza Manzil Guesthouse',
                           short='Sheeza Manzil')
            from app.services.branding import get_brand
            b = get_brand()
            self.assertEqual(b['name'], 'Maakanaa Village Hotel')
            self.assertEqual(b['short_name'], 'Maakanaa')
            self.assertEqual(b['invoice_display_name'],
                             'Maakanaa Village Hotel',
                             'invoice name should follow the override')

    def test_color_override(self):
        os.environ['BRAND_PRIMARY_COLOR_OVERRIDE'] = '0d9488'
        app = create_app(_TestConfig)
        with app.app_context():
            db.create_all()
            _seed_property()
            from app.services.branding import get_brand
            b = get_brand()
            # Hex prefix added even when the env value omits it
            self.assertEqual(b['primary_color'], '#0d9488')

    def test_override_visible_in_header_wordmark(self):
        os.environ['BRAND_NAME_OVERRIDE'] = 'Maakanaa Village Hotel'
        os.environ['BRAND_SHORT_NAME_OVERRIDE'] = 'Maakanaa'
        app = create_app(_TestConfig)
        with app.app_context():
            db.create_all()
            _seed_property(name='Sheeza Manzil Guesthouse',
                           short='Sheeza Manzil')
            u = User(username='admin', email='a@x', role='admin')
            u.set_password('aaaaaaaaaa1')
            db.session.add(u)
            db.session.commit()
            client = app.test_client()
            with client.session_transaction() as sess:
                sess['_user_id'] = str(u.id)
                sess['_fresh'] = True
            r = client.get('/dashboard/')
            self.assertEqual(r.status_code, 200)
            self.assertIn(b'Maakanaa Village Hotel', r.data,
                          'desktop wordmark must show full property_name')
            self.assertIn(b'Maakanaa', r.data,
                          'mobile wordmark must show short_name')

    def test_override_visible_in_login_page(self):
        os.environ['BRAND_NAME_OVERRIDE'] = 'Maakanaa Village Hotel'
        app = create_app(_TestConfig)
        with app.app_context():
            db.create_all()
            _seed_property(name='Sheeza Manzil Guesthouse',
                           short='Sheeza Manzil')
            r = app.test_client().get('/appadmin')
            self.assertEqual(r.status_code, 200)
            self.assertIn(b'Maakanaa Village Hotel', r.data)


class BrandLogoOverrideTests(unittest.TestCase):
    """Branding hotfix: BRAND_LOGO_PATH_OVERRIDE must take precedence
    over the PropertySettings.logo_path DB value, and every rendered
    img/favicon must carry the deploy-SHA cache-buster query string.

    Regression guard for the POS-logo-flash incident: the prior brand
    override layer covered name + short_name + primary_color but NOT
    the logo path, so the visible <img src> still pointed at the old
    /static/img/logo.png while the alt text said the new property
    name. These tests pin both ends of the fix.
    """

    def setUp(self):
        self._snap = {k: os.environ.get(k) for k in
                      ('BRAND_LOGO_PATH_OVERRIDE',
                       'BRAND_NAME_OVERRIDE',
                       'BRAND_SHORT_NAME_OVERRIDE')}
        for k in self._snap:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._snap.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_logo_override_wins_over_db(self):
        os.environ['BRAND_LOGO_PATH_OVERRIDE'] = '/static/img/maakanaa-logo.svg'
        app = create_app(_TestConfig)
        with app.app_context():
            db.create_all()
            _seed_property()  # leaves logo_path = '/static/img/logo.png' in DB
            from app.services.branding import get_brand
            self.assertEqual(get_brand()['logo_path'],
                             '/static/img/maakanaa-logo.svg')

    def test_no_override_keeps_db_logo_path(self):
        os.environ.pop('BRAND_LOGO_PATH_OVERRIDE', None)
        app = create_app(_TestConfig)
        with app.app_context():
            db.create_all()
            _seed_property()
            from app.services.branding import get_brand
            # _seed_property uses PropertySettings's bootstrap default
            # which is the legacy '/static/img/logo.png'.
            self.assertEqual(get_brand()['logo_path'],
                             '/static/img/logo.png')

    def test_login_page_renders_overridden_logo_with_cache_buster(self):
        os.environ['BRAND_LOGO_PATH_OVERRIDE'] = '/static/img/maakanaa-logo.svg'
        os.environ['BRAND_NAME_OVERRIDE'] = 'Maakanaa Village Hotel'
        app = create_app(_TestConfig)
        with app.app_context():
            db.create_all()
            _seed_property()
            r = app.test_client().get('/appadmin')
            self.assertEqual(r.status_code, 200)
            # New logo path is in <img src> and the favicon link
            self.assertIn(b'/static/img/maakanaa-logo.svg', r.data)
            # Old logo path must not leak through the override
            self.assertNotIn(b'src="/static/img/logo.png"', r.data)
            # Cache-buster query string is present on the asset URL
            self.assertIn(b'maakanaa-logo.svg?v=', r.data)

    def test_authenticated_header_renders_new_logo(self):
        os.environ['BRAND_LOGO_PATH_OVERRIDE'] = '/static/img/maakanaa-logo.svg'
        os.environ['BRAND_NAME_OVERRIDE'] = 'Maakanaa Village Hotel'
        app = create_app(_TestConfig)
        with app.app_context():
            db.create_all()
            _seed_property()
            u = User(username='admin', email='a@x', role='admin')
            u.set_password('aaaaaaaaaa1')
            db.session.add(u)
            db.session.commit()
            client = app.test_client()
            with client.session_transaction() as sess:
                sess['_user_id'] = str(u.id)
                sess['_fresh'] = True
            r = client.get('/dashboard/')
            self.assertEqual(r.status_code, 200)
            # The desktop header img + favicon must both use the SVG
            self.assertIn(b'maakanaa-logo.svg', r.data)
            # And cache-busted
            self.assertIn(b'maakanaa-logo.svg?v=', r.data)
            # No stale reference to the old PNG anywhere on the page
            self.assertNotIn(b'src="/static/img/logo.png"', r.data)
            self.assertNotIn(b'href="/static/img/logo.png"', r.data)


class HealthzProbeTests(unittest.TestCase):
    """Public /healthz endpoint must be reachable without auth and
    surface enough to verify a deploy."""

    def setUp(self):
        self._snap = os.environ.get('BRAND_NAME_OVERRIDE')
        os.environ['BRAND_NAME_OVERRIDE'] = 'Maakanaa Village Hotel'
        self.app = create_app(_TestConfig)
        with self.app.app_context():
            db.create_all()

    def tearDown(self):
        if self._snap is None:
            os.environ.pop('BRAND_NAME_OVERRIDE', None)
        else:
            os.environ['BRAND_NAME_OVERRIDE'] = self._snap
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def test_healthz_no_auth_required(self):
        r = self.app.test_client().get('/healthz')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['brand_name'], 'Maakanaa Village Hotel')
        self.assertIn('sha', body)
        self.assertEqual(body['login_redirect'], '/dashboard/')


if __name__ == '__main__':
    unittest.main()
