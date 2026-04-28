"""Tests for centralized branding configuration.

Hard rules covered:
  - Default values match the historical Sheeza Manzil identity, so a
    production deployment with NO env vars produces unchanged output.
  - Env-var overrides apply at request time (no module-level cache).
  - Templates rendered with default env still contain "Sheeza Manzil".
  - Templates rendered with BRAND_NAME override show the new brand.
  - The system prompt swaps brand tokens dynamically while leaving the
    `_SYSTEM_PROMPT` constant untouched for backward-compat tests.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

# Clean env BEFORE app import — same pattern as the other suites.
for _v in ('DATABASE_URL',
           'AI_DRAFT_PROVIDER', 'AI_DRAFT_MODEL',
           'GEMINI_API_KEY', 'ANTHROPIC_API_KEY',
           'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN',
           'BRAND_NAME', 'BRAND_SHORT_NAME', 'BRAND_TAGLINE',
           'BRAND_LOGO_PATH', 'BRAND_PRIMARY_COLOR'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                       # noqa: E402
from app import create_app                                      # noqa: E402
from app.models import db, User                                 # noqa: E402
from app.services import branding, ai_drafts                    # noqa: E402


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _make_app():
    return create_app(_TestConfig)


# ─────────────────────────────────────────────────────────────────────
# 1) Pure get_brand() defaults
# ─────────────────────────────────────────────────────────────────────

class GetBrandDefaultsTests(unittest.TestCase):

    def setUp(self):
        for v in ('BRAND_NAME', 'BRAND_SHORT_NAME', 'BRAND_TAGLINE',
                  'BRAND_LOGO_PATH', 'BRAND_PRIMARY_COLOR'):
            os.environ.pop(v, None)

    def test_defaults_preserve_sheeza_identity(self):
        b = branding.get_brand()
        self.assertEqual(b['name'], 'Sheeza Manzil Guesthouse')
        self.assertEqual(b['short_name'], 'Sheeza Manzil')
        self.assertEqual(b['tagline'], '')
        self.assertEqual(b['logo_path'], '/static/img/logo.png')
        self.assertEqual(b['primary_color'], '#7B3F00')

    def test_all_keys_always_present(self):
        b = branding.get_brand()
        for key in ('name', 'short_name', 'tagline', 'logo_path',
                    'primary_color'):
            self.assertIn(key, b)


class GetBrandEnvOverrideTests(unittest.TestCase):

    def setUp(self):
        for v in ('BRAND_NAME', 'BRAND_SHORT_NAME', 'BRAND_TAGLINE',
                  'BRAND_LOGO_PATH', 'BRAND_PRIMARY_COLOR'):
            os.environ.pop(v, None)

    def tearDown(self):
        self.setUp()  # clear again

    def test_brand_name_override(self):
        os.environ['BRAND_NAME'] = 'Maakanaa Village Hotel'
        b = branding.get_brand()
        self.assertEqual(b['name'], 'Maakanaa Village Hotel')

    def test_short_name_override(self):
        os.environ['BRAND_SHORT_NAME'] = 'Maakanaa Village'
        b = branding.get_brand()
        self.assertEqual(b['short_name'], 'Maakanaa Village')

    def test_tagline_override(self):
        os.environ['BRAND_TAGLINE'] = 'Island Stay · Comfort · Simplicity'
        b = branding.get_brand()
        self.assertEqual(b['tagline'],
                         'Island Stay · Comfort · Simplicity')

    def test_logo_path_override(self):
        os.environ['BRAND_LOGO_PATH'] = '/static/img/maakanaa-logo.svg'
        b = branding.get_brand()
        self.assertEqual(b['logo_path'], '/static/img/maakanaa-logo.svg')

    def test_primary_color_normalizes_hash(self):
        os.environ['BRAND_PRIMARY_COLOR'] = '1FA6A6'  # no leading #
        b = branding.get_brand()
        self.assertEqual(b['primary_color'], '#1FA6A6')

    def test_empty_env_falls_back_to_default(self):
        os.environ['BRAND_NAME'] = ''
        b = branding.get_brand()
        self.assertEqual(b['name'], 'Sheeza Manzil Guesthouse')


# ─────────────────────────────────────────────────────────────────────
# 2) Template-level integration
# ─────────────────────────────────────────────────────────────────────

class TemplateRenderingTests(unittest.TestCase):

    def setUp(self):
        for v in ('BRAND_NAME', 'BRAND_SHORT_NAME', 'BRAND_TAGLINE'):
            os.environ.pop(v, None)
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_default_login_page_shows_sheeza(self):
        # Try the auth login route; fall back to staff login if the
        # route is named differently.
        for url in ('/login', '/staff/login', '/console'):
            r = self.client.get(url)
            if r.status_code == 200:
                # At least one of (full or short) name should appear.
                blob = r.data.decode('utf-8', errors='ignore')
                self.assertTrue(
                    'Sheeza Manzil' in blob,
                    f'login page at {url} did not contain default brand'
                )
                return
        self.skipTest('no public login URL returned 200')

    def test_branded_login_page_shows_override(self):
        os.environ['BRAND_NAME']       = 'Maakanaa Village Hotel'
        os.environ['BRAND_SHORT_NAME'] = 'Maakanaa Village'
        try:
            for url in ('/login', '/staff/login', '/console'):
                r = self.client.get(url)
                if r.status_code == 200:
                    blob = r.data.decode('utf-8', errors='ignore')
                    self.assertIn('Maakanaa', blob,
                                  f'override not visible on {url}')
                    self.assertNotIn('Sheeza', blob,
                                     f'Sheeza leaked through on {url}')
                    return
            self.skipTest('no public login URL returned 200')
        finally:
            os.environ.pop('BRAND_NAME', None)
            os.environ.pop('BRAND_SHORT_NAME', None)

    def test_default_privacy_page_shows_sheeza(self):
        r = self.client.get('/privacy')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Sheeza Manzil', r.data)

    def test_branded_privacy_page_shows_override(self):
        os.environ['BRAND_NAME'] = 'Maakanaa Village Hotel'
        try:
            r = self.client.get('/privacy')
            self.assertEqual(r.status_code, 200)
            self.assertIn(b'Maakanaa Village Hotel', r.data)
            self.assertNotIn(b'Sheeza Manzil', r.data)
        finally:
            os.environ.pop('BRAND_NAME', None)


# ─────────────────────────────────────────────────────────────────────
# 3) AI system prompt brand-token swapping
# ─────────────────────────────────────────────────────────────────────

class SystemPromptBrandSwapTests(unittest.TestCase):

    def setUp(self):
        for v in ('BRAND_NAME', 'BRAND_SHORT_NAME'):
            os.environ.pop(v, None)

    def tearDown(self):
        self.setUp()

    def test_constant_still_contains_sheeza(self):
        # Backward compat: the literal constant must still hold the
        # Sheeza identity, since older tests assert against it.
        self.assertIn('Sheeza Manzil', ai_drafts._SYSTEM_PROMPT)

    def test_default_prompt_unchanged(self):
        prompt = ai_drafts._get_system_prompt()
        self.assertEqual(prompt, ai_drafts._SYSTEM_PROMPT)

    def test_brand_override_swaps_tokens(self):
        os.environ['BRAND_NAME']       = 'Maakanaa Village Hotel'
        os.environ['BRAND_SHORT_NAME'] = 'Maakanaa Village'
        try:
            prompt = ai_drafts._get_system_prompt()
            self.assertIn('Maakanaa Village Hotel', prompt)
            self.assertNotIn('Sheeza Manzil Guesthouse', prompt)
            # The shorter token must also be swapped (no orphan "Sheeza Manzil")
            self.assertNotIn('Sheeza Manzil', prompt)
        finally:
            os.environ.pop('BRAND_NAME', None)
            os.environ.pop('BRAND_SHORT_NAME', None)


# ─────────────────────────────────────────────────────────────────────
# 4) Logo asset present
# ─────────────────────────────────────────────────────────────────────

class LogoAssetTests(unittest.TestCase):

    def test_default_logo_png_exists(self):
        from pathlib import Path
        repo = Path(__file__).resolve().parent.parent
        path = repo / 'app' / 'static' / 'img' / 'logo.png'
        self.assertTrue(path.exists(), f'default logo missing at {path}')

    def test_maakanaa_logo_svg_exists(self):
        from pathlib import Path
        repo = Path(__file__).resolve().parent.parent
        path = repo / 'app' / 'static' / 'img' / 'maakanaa-logo.svg'
        self.assertTrue(path.exists(), f'maakanaa logo missing at {path}')


if __name__ == '__main__':
    unittest.main()
