"""Tests for Property Settings / Branding Foundation V1.

Covers the 9 requirements from the build spec, section I:

  1. property settings creation/loading
  2. helper/service returns current property settings
  3. settings page requires login/admin
  4. settings update works
  5. branding helper replaces visible display name
  6. payment instruction helper reads from settings
  7. no WhatsApp/Gemini calls
  8. migration file exists
  9. migration only creates property-settings-related table(s)
"""

from __future__ import annotations

import json
import os
import re
import unittest
from pathlib import Path
from unittest import mock

for _v in ('DATABASE_URL',
           'AI_DRAFT_PROVIDER', 'GEMINI_API_KEY', 'ANTHROPIC_API_KEY',
           'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                       # noqa: E402
from app import create_app                                      # noqa: E402
from app.models import db, User, ActivityLog, PropertySettings  # noqa: E402
from app.services import property_settings as ps_svc            # noqa: E402
from app.services import branding as branding_svc               # noqa: E402
from app.services import payment_instructions as pi_svc         # noqa: E402
from app.services import whatsapp as wa                         # noqa: E402
from app.services import ai_drafts                              # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = (
    _REPO_ROOT / 'migrations' / 'versions'
    / '0c5e7f3b842a_add_property_settings.py'
)


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _make_app():
    return create_app(_TestConfig)


_seed_counter = {'n': 0}


def _seed_users():
    _seed_counter['n'] += 1
    n = _seed_counter['n']
    admin = User(username=f'ps_admin_{n}', email=f'a{n}@x', role='admin')
    admin.set_password('aaaaaaaaaa1')
    staff = User(username=f'ps_staff_{n}', email=f's{n}@x', role='staff')
    staff.set_password('aaaaaaaaaa1')
    db.session.add_all([admin, staff])
    db.session.commit()
    return admin.id, staff.id


def _seed_settings(**overrides):
    """Insert a singleton PropertySettings row with sensible defaults
    that tests can override."""
    base = {
        'property_name': 'Test Inn',
        'short_name': 'TI',
        'tagline': '',
        'logo_path': '/static/img/test.png',
        'primary_color': '#123456',
        'phone': '+9609999999',
        'whatsapp_number': '+9609999999',
        'email': 'test@example.com',
        'address': 'Test St',
        'city': 'Testville',
        'country': 'Testland',
        'currency_code': 'USD',
        'timezone': 'UTC',
        'check_in_time': '14:00',
        'check_out_time': '11:00',
        'invoice_display_name': 'Test Inn Pty Ltd',
        'payment_instructions_text': '',
        'bank_name': 'Test Bank',
        'bank_account_name': 'Test Inn',
        'bank_account_number': '123456789',
        'tax_name': 'GST',
        'tax_rate': 12.0,
        'service_charge_rate': 10.0,
        'is_active': True,
    }
    base.update(overrides)
    s = PropertySettings(**base)
    db.session.add(s)
    db.session.commit()
    return s


class _BaseAppTest(unittest.TestCase):

    def setUp(self):
        # Hard-mock outbound side-effects (Req 7)
        self._patches = []
        self._wa_send = mock.patch.object(
            wa, '_send', side_effect=AssertionError(
                'WhatsApp must not be called by Property Settings V1'))
        self._patches.append(self._wa_send.start())
        self._wa_template = mock.patch.object(
            wa, '_send_template', side_effect=AssertionError(
                'WhatsApp templates must not be called by Property Settings V1'))
        self._patches.append(self._wa_template.start())
        self._ai_patch = mock.patch.object(
            ai_drafts, 'generate_draft',
            side_effect=AssertionError(
                'AI drafts must not be called by Property Settings V1'))
        self._patches.append(self._ai_patch.start())

        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.admin_id, self.staff_id = _seed_users()
        self.client = self.app.test_client()

    def tearDown(self):
        for p in (self._wa_send, self._wa_template, self._ai_patch):
            p.stop()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh']   = True


# ─────────────────────────────────────────────────────────────────────
# 1) Creation / loading (Req 1)
# ─────────────────────────────────────────────────────────────────────

class CreationLoadingTests(_BaseAppTest):

    def test_get_settings_autoseeds_when_missing(self):
        # Fresh DB with no PropertySettings row — get_settings must
        # lazily create one rather than crashing.
        self.assertIsNone(PropertySettings.query.first())
        s = ps_svc.get_settings()
        self.assertIsNotNone(s)
        self.assertEqual(PropertySettings.query.count(), 1)
        self.assertTrue(s.property_name)
        self.assertEqual(s.currency_code, 'USD')

    def test_autoseed_disabled_returns_none(self):
        self.assertIsNone(ps_svc.get_settings(autoseed=False))
        # Must not have inserted anything
        self.assertEqual(PropertySettings.query.count(), 0)

    def test_loads_existing_row_unchanged(self):
        seeded = _seed_settings(property_name='ExistingInn',
                                  primary_color='#abcdef')
        s = ps_svc.get_settings()
        self.assertEqual(s.id, seeded.id)
        self.assertEqual(s.property_name, 'ExistingInn')
        self.assertEqual(s.primary_color, '#abcdef')
        self.assertEqual(PropertySettings.query.count(), 1)


# ─────────────────────────────────────────────────────────────────────
# 2) Helpers return current settings (Req 2)
# ─────────────────────────────────────────────────────────────────────

class HelperTests(_BaseAppTest):

    def test_get_branding_returns_full_dict(self):
        _seed_settings(property_name='HelperHaus',
                       short_name='HH',
                       primary_color='123abc',           # missing #
                       phone='+9601111111',
                       bank_account_number='999000111')
        b = ps_svc.get_branding()
        self.assertEqual(b['name'], 'HelperHaus')
        self.assertEqual(b['short_name'], 'HH')
        self.assertEqual(b['primary_color'], '#123abc')   # # injected
        self.assertEqual(b['phone'], '+9601111111')
        self.assertEqual(b['contact_phone'], '+9601111111')
        self.assertEqual(b['bank_account_number'], '999000111')
        self.assertEqual(b['bank_account'], '999000111')
        # All expected keys present
        for k in ('name', 'short_name', 'tagline', 'logo_path',
                   'primary_color', 'phone', 'contact_phone',
                   'whatsapp_number', 'email', 'website_url',
                   'address', 'city', 'country', 'currency_code',
                   'check_in_time', 'check_out_time',
                   'bank_name', 'bank_account_name',
                   'bank_account_number', 'bank_account',
                   'invoice_display_name'):
            self.assertIn(k, b)

    def test_get_contact_info(self):
        _seed_settings(phone='+9601112222',
                        whatsapp_number='+9603334444',
                        email='hello@test.com')
        c = ps_svc.get_contact_info()
        self.assertEqual(c['phone'], '+9601112222')
        self.assertEqual(c['whatsapp_number'], '+9603334444')
        self.assertEqual(c['email'], 'hello@test.com')

    def test_get_branding_falls_back_for_blank_short_name(self):
        _seed_settings(property_name='Long Property Name',
                        short_name=None)
        b = ps_svc.get_branding()
        # short_name falls back to property_name when blank
        self.assertEqual(b['short_name'], 'Long Property Name')


# ─────────────────────────────────────────────────────────────────────
# 3) Auth gate on settings page (Req 3)
# ─────────────────────────────────────────────────────────────────────

class AuthTests(_BaseAppTest):

    def test_anonymous_redirected(self):
        r = self.client.get('/admin/property-settings/')
        self.assertIn(r.status_code, (301, 302, 401))

    def test_staff_blocked(self):
        self._login(self.staff_id)
        r = self.client.get('/admin/property-settings/')
        self.assertIn(r.status_code, (302, 401, 403))

    def test_admin_allowed(self):
        self._login(self.admin_id)
        r = self.client.get('/admin/property-settings/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Property Settings', r.data)

    def test_anonymous_post_blocked(self):
        r = self.client.post('/admin/property-settings/',
                              data={'property_name': 'Hijack'})
        self.assertIn(r.status_code, (301, 302, 401))
        # No settings row created
        self.assertEqual(PropertySettings.query.count(), 0)


# ─────────────────────────────────────────────────────────────────────
# 4) Settings update works (Req 4) + ActivityLog metadata
# ─────────────────────────────────────────────────────────────────────

class UpdateTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)
        self.s = _seed_settings()

    def test_update_changes_persist(self):
        r = self.client.post('/admin/property-settings/', data={
            'property_name':  'Updated Property',
            'short_name':     'UP',
            'tagline':        'Tagline now',
            'logo_path':      '/static/img/test.png',
            'primary_color':  '#ff0000',
            'phone':          '+9601000000',
            'whatsapp_number': '+9601000000',
            'email':          'mail@test.com',
            'address':        'New Address',
            'city':           'NewCity',
            'country':        'Testland',
            'currency_code':  'USD',
            'timezone':       'UTC',
            'check_in_time':  '15:00',
            'check_out_time': '12:00',
            'invoice_display_name': 'Updated Inv',
            'payment_instructions_text': '',
            'bank_name':           'Test Bank',
            'bank_account_name':   'Updated Acct',
            'bank_account_number': '111222333',
            'tax_name':            'VAT',
            'tax_rate':            '15',
            'service_charge_rate': '5',
            'booking_terms':       '',
            'cancellation_policy': '',
            'wifi_info':           'SSID Home / pwd 1234',
        })
        self.assertIn(r.status_code, (301, 302))
        s = ps_svc.get_settings()
        self.assertEqual(s.property_name, 'Updated Property')
        self.assertEqual(s.short_name,    'UP')
        self.assertEqual(s.primary_color, '#ff0000')
        self.assertEqual(s.tax_rate,      15.0)
        self.assertEqual(s.service_charge_rate, 5.0)
        self.assertEqual(s.bank_account_number, '111222333')

    def test_blank_property_name_rejected(self):
        # Empty required field — service rejects with no DB write
        result = ps_svc.update_settings({'property_name': '   '})
        self.assertFalse(result['ok'])
        s = ps_svc.get_settings()
        self.assertEqual(s.property_name, 'Test Inn')   # unchanged

    def test_invalid_tax_rate_rejected(self):
        result = ps_svc.update_settings({'tax_rate': 'abc'})
        self.assertFalse(result['ok'])

    def test_negative_tax_rate_rejected(self):
        result = ps_svc.update_settings({'tax_rate': '-5'})
        self.assertFalse(result['ok'])

    def test_audit_row_records_changed_field_names_only(self):
        result = ps_svc.update_settings({
            'property_name': 'Renamed',
            'phone':         '+9605555555',
        }, user=User.query.get(self.admin_id))
        db.session.commit()
        self.assertTrue(result['ok'])
        rows = ActivityLog.query.filter_by(
            action='property_settings.updated').all()
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0].metadata_json or '{}')
        self.assertIn('changed_fields', meta)
        self.assertEqual(set(meta['changed_fields'].split(',')),
                         {'property_name', 'phone'})
        # NEVER log values
        self.assertNotIn('property_name_value', meta)
        self.assertNotIn('phone_value', meta)
        self.assertNotIn('+9605555555', json.dumps(meta))

    def test_no_audit_when_nothing_changes(self):
        result = ps_svc.update_settings({
            'property_name': 'Test Inn',  # already this
        })
        db.session.commit()
        self.assertTrue(result['ok'])
        self.assertEqual(result['changed_fields'], [])
        self.assertEqual(
            ActivityLog.query.filter_by(
                action='property_settings.updated').count(),
            0,
        )

    def test_color_normalization(self):
        ps_svc.update_settings({'primary_color': 'aabbcc'})
        s = ps_svc.get_settings()
        self.assertEqual(s.primary_color, '#aabbcc')


# ─────────────────────────────────────────────────────────────────────
# 5) Branding helper replaces visible display name (Req 5)
# ─────────────────────────────────────────────────────────────────────

class BrandingIntegrationTests(_BaseAppTest):

    def test_brand_context_uses_db_property_name(self):
        # Before login: render the public booking engine landing page
        # which prints {{ brand.name }} in <title>.
        _seed_settings(property_name='BrandedHaus',
                        bank_account_number='999000111',
                        bank_account_name='HausCo Ltd',
                        bank_name='Test Bank Plc',
                        whatsapp_number='+9608888888')
        r = self.client.get('/book/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'BrandedHaus', r.data)

    def test_get_brand_legacy_function_now_uses_db(self):
        _seed_settings(property_name='LegacyShouldChange',
                        primary_color='#000fff')
        b = branding_svc.get_brand()
        self.assertEqual(b['name'], 'LegacyShouldChange')
        self.assertEqual(b['primary_color'], '#000fff')


# ─────────────────────────────────────────────────────────────────────
# 6) Payment instruction helper reads from settings (Req 6)
# ─────────────────────────────────────────────────────────────────────

class PaymentInstructionTests(_BaseAppTest):

    def test_explicit_text_is_returned_verbatim(self):
        custom = ('Please transfer to:\n'
                  'My Bank · 9999000099\n'
                  'Reference your booking ref.')
        _seed_settings(payment_instructions_text=custom,
                        bank_account_number='9999000099',
                        bank_account_name='Custom Account')
        out = pi_svc.get_payment_instruction_block()
        self.assertEqual(out, custom)

    def test_falls_back_to_synthesized_block(self):
        # No explicit text — service should compose from bank fields.
        _seed_settings(payment_instructions_text='',
                        bank_name='Bank ABC',
                        bank_account_name='Synth Co.',
                        bank_account_number='555444333')
        out = pi_svc.get_payment_instruction_block()
        self.assertIn('Bank: Bank ABC', out)
        self.assertIn('Account Name: Synth Co.', out)
        self.assertIn('Account Number: 555444333', out)
        self.assertIn('Please send the payment slip', out)

    def test_block_always_contains_signature_phrase(self):
        # The synthesized fallback block always ends with the standard
        # confirmation request. Even an auto-seeded row (no bank info)
        # produces a non-empty block that ends with this phrase.
        block = pi_svc.get_payment_instruction_block()
        self.assertIn('Please send the payment slip', block)

    def test_module_constants_still_defined_for_offline_callers(self):
        # The hard-coded constants remain importable as a last-resort
        # fallback when the DB is unavailable mid-migration.
        self.assertEqual(pi_svc.ACCOUNT_NAME,   'SHEEZA IMAD/MOHAMED S.R.')
        self.assertEqual(pi_svc.ACCOUNT_NUMBER, '7770000212622')
        self.assertIn('Account Number', pi_svc.PAYMENT_INSTRUCTION_BLOCK)


# ─────────────────────────────────────────────────────────────────────
# 7) No external coupling (Req 7)
# ─────────────────────────────────────────────────────────────────────

class NoExternalCouplingTests(_BaseAppTest):

    def test_full_flow_no_external_calls(self):
        self._login(self.admin_id)
        _seed_settings()
        self.client.get('/admin/property-settings/')
        self.client.post('/admin/property-settings/', data={
            'property_name': 'Renamed',
            'currency_code': 'USD', 'timezone': 'UTC',
        })
        # And a few read paths
        self.client.get('/book/')
        self.assertEqual(wa._send.call_count,           0)
        self.assertEqual(wa._send_template.call_count,  0)
        self.assertEqual(ai_drafts.generate_draft.call_count, 0)


# ─────────────────────────────────────────────────────────────────────
# 8 + 9) Migration shape (Reqs 8, 9)
# ─────────────────────────────────────────────────────────────────────

class MigrationShapeTests(unittest.TestCase):

    def test_migration_file_exists(self):
        self.assertTrue(_MIGRATION_PATH.exists(),
                        f'expected migration at {_MIGRATION_PATH}')

    def test_migration_revision_metadata(self):
        text = _MIGRATION_PATH.read_text()
        self.assertIn("revision = '0c5e7f3b842a'", text)
        self.assertIn("down_revision = 'f9a4b8d2c531'", text)

    def test_migration_creates_only_property_settings(self):
        text = _MIGRATION_PATH.read_text()
        creates = set(re.findall(r"op\.create_table\(\s*'([^']+)'", text))
        self.assertEqual(creates, {'property_settings'},
                          f'unexpected tables: {creates}')
        # No mutation of existing tables
        self.assertNotIn('op.add_column', text)
        self.assertNotIn('op.alter_column', text)
        # Round-trip drop in downgrade
        self.assertIn("op.drop_table('property_settings')", text)

    def test_migration_seeds_singleton(self):
        text = _MIGRATION_PATH.read_text()
        self.assertIn('op.bulk_insert', text)
        # Seeded with a sensible default name so the app works pre-edit
        self.assertIn("'property_name'", text)


if __name__ == '__main__':
    unittest.main()
