"""Tests for the AI Draft Assistant V1 (provider-pluggable).

Covers:
  1. Draft-type whitelist (9 V1 types).
  2. Invalid draft_type rejected by service + route.
  3. Privacy: prompt builder NEVER includes passport/ID number, address,
     or full uploaded filename.
  4. Content: prompt DOES include booking_ref / room / dates / total.
  5. Provider selection — gemini default, anthropic on env var.
  6. Missing API key for the active provider → ai_not_configured.
  7. Invalid AI_DRAFT_PROVIDER → invalid_provider error path.
  8. API failure handling (Anthropic SDK raise, Gemini HTTP non-200).
  9. Route requires admin login.
 10. Route does not import or call any WhatsApp send helper.
 11. Route does not mutate booking/payment/room state.
 12. ActivityLog has metadata only (no body, no prompt, no API key, no PII).
 13. Disclaimer banner rendered to user.

These tests use an in-memory SQLite DB. Both providers are mocked at the
public dispatcher boundary (`_call_provider`) so neither the Anthropic
SDK nor the Gemini REST endpoint is ever contacted.
"""

from __future__ import annotations

import os
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

# Clean env BEFORE app import — no live keys, no DATABASE_URL.
for _v in ('DATABASE_URL',
           'AI_DRAFT_PROVIDER', 'AI_DRAFT_MODEL',
           'GEMINI_API_KEY', 'ANTHROPIC_API_KEY', 'ANTHROPIC_MODEL'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                         # noqa: E402
from app import create_app                                        # noqa: E402
from app.models import db, User, Room, Guest, Booking, Invoice    # noqa: E402
from app.models import ActivityLog                                # noqa: E402
from app.services import ai_drafts                                # noqa: E402
from app.services.ai_drafts import (                              # noqa: E402
    DRAFT_TYPES, DRAFT_LABELS, DRAFT_DISCLAIMER, PROVIDERS,
    build_prompt, can_draft, generate_draft,
)


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
        'pool_recycle': 300,
    }
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _make_app():
    return create_app(_TestConfig)


def _seed_booking(db_, *, with_id=True, with_slip=False,
                  status='confirmed', payment_status='unpaid',
                  amount_paid=0.0):
    room = Room(number='99', name='Test Room', room_type='Test',
                floor=0, capacity=2, price_per_night=600.0)
    guest = Guest(
        first_name='Hassan', last_name='Hamid',
        phone='+9607000000',
        email='hassan@example.com',
        nationality='Maldivian',
        # PII that must NOT leak:
        id_type='passport', id_number='ABCDE12345',
        address='Maaveyo Magu, Hanimaadhoo, Hdh. Maldives',
    )
    db_.session.add_all([room, guest])
    db_.session.flush()
    booking = Booking(
        booking_ref='BKAITEST',
        room_id=room.id, guest_id=guest.id,
        check_in_date=date.today() + timedelta(days=3),
        check_out_date=date.today() + timedelta(days=5),
        num_guests=2,
        total_amount=1200.0,
        status=status,
        id_card_filename='id_hassan_abc123.jpg' if with_id else None,
        payment_slip_filename='slip_hassan_def456.jpg' if with_slip else None,
    )
    db_.session.add(booking)
    db_.session.flush()
    invoice = Invoice(
        invoice_number='INVAITEST',
        booking_id=booking.id,
        subtotal=1200.0, total_amount=1200.0,
        amount_paid=amount_paid,
        payment_status=payment_status,
    )
    db_.session.add(invoice)
    db_.session.commit()
    return booking


def _reset_module_state():
    """Wipe the lazy Anthropic singleton + provider env so each test starts
    from a clean state."""
    ai_drafts._anthropic_client = None
    for v in ('AI_DRAFT_PROVIDER', 'AI_DRAFT_MODEL',
              'GEMINI_API_KEY', 'ANTHROPIC_API_KEY', 'ANTHROPIC_MODEL'):
        os.environ.pop(v, None)


# ─────────────────────────────────────────────────────────────────────────
# 1, 2 — whitelist + invalid type
# ─────────────────────────────────────────────────────────────────────────

class DraftTypeWhitelistTests(unittest.TestCase):

    def test_v1_draft_types_match_promised_set(self):
        expected = {
            'booking_received', 'payment_instructions',
            'payment_received_pending_review', 'booking_confirmed',
            'payment_mismatch', 'missing_id', 'missing_payment',
            'checkin_instructions', 'thank_you_review',
        }
        self.assertEqual(set(DRAFT_TYPES), expected)
        self.assertEqual(len(DRAFT_TYPES), 9)

    def test_every_draft_type_has_a_label(self):
        for dt in DRAFT_TYPES:
            self.assertIn(dt, DRAFT_LABELS)
            self.assertTrue(DRAFT_LABELS[dt].strip())

    def test_invalid_draft_type_rejected_by_service(self):
        result = generate_draft('not_a_real_type', booking=None)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'invalid_draft_type')


# ─────────────────────────────────────────────────────────────────────────
# 3, 4 — privacy + content of the prompt builder
# ─────────────────────────────────────────────────────────────────────────

class PromptBuilderPrivacyTests(unittest.TestCase):

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.booking = _seed_booking(db)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_prompt_does_not_contain_passport_number(self):
        for dt in DRAFT_TYPES:
            prompt = build_prompt(dt, self.booking)
            self.assertNotIn('ABCDE12345', prompt,
                             f'{dt}: passport NUMBER value leaked')

    def test_prompt_does_not_contain_id_field_names(self):
        for dt in DRAFT_TYPES:
            prompt = build_prompt(dt, self.booking)
            self.assertNotIn('id_number', prompt,
                             f'{dt}: id_number field leaked')
            self.assertNotIn('id_type:', prompt,
                             f'{dt}: id_type field leaked')

    def test_prompt_does_not_contain_full_address(self):
        for dt in DRAFT_TYPES:
            prompt = build_prompt(dt, self.booking)
            self.assertNotIn('Maaveyo Magu', prompt,
                             f'{dt}: full guest address leaked')

    def test_prompt_does_not_contain_full_uploaded_filenames(self):
        for dt in DRAFT_TYPES:
            prompt = build_prompt(dt, self.booking)
            self.assertNotIn('id_hassan_abc123.jpg', prompt,
                             f'{dt}: id-card filename leaked')
            self.assertNotIn('slip_hassan_def456', prompt,
                             f'{dt}: payment-slip filename leaked')

    def test_prompt_includes_booking_ref_room_dates_total(self):
        for dt in DRAFT_TYPES:
            prompt = build_prompt(dt, self.booking)
            self.assertIn('BKAITEST', prompt, f'{dt}: booking_ref missing')
            self.assertIn('99', prompt, f'{dt}: room number missing')
            self.assertIn('1200', prompt, f'{dt}: total amount missing')

    def test_no_fabrication_hedging_in_system_prompt(self):
        sys_prompt = ai_drafts._SYSTEM_PROMPT
        self.assertIn('Use ONLY the booking facts', sys_prompt)
        self.assertIn('admin: please verify', sys_prompt)
        self.assertIn('Sheeza Manzil', sys_prompt)


# ─────────────────────────────────────────────────────────────────────────
# State gating
# ─────────────────────────────────────────────────────────────────────────

class StateGatingTests(unittest.TestCase):

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_thank_you_only_after_checkout(self):
        b = _seed_booking(db, status='confirmed')
        self.assertFalse(can_draft(b, 'thank_you_review'))
        b.status = 'checked_out'
        self.assertTrue(can_draft(b, 'thank_you_review'))

    def test_missing_id_only_when_id_card_absent(self):
        b1 = _seed_booking(db, with_id=True)
        self.assertFalse(can_draft(b1, 'missing_id'))


# ─────────────────────────────────────────────────────────────────────────
# 5 — provider selection
# ─────────────────────────────────────────────────────────────────────────

class ProviderResolutionTests(unittest.TestCase):

    def setUp(self):
        _reset_module_state()
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.booking = _seed_booking(db)

    def tearDown(self):
        _reset_module_state()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_default_provider_is_gemini(self):
        self.assertEqual(ai_drafts._get_provider(), 'gemini')

    def test_supported_providers_set(self):
        self.assertIn('gemini', PROVIDERS)
        self.assertIn('anthropic', PROVIDERS)

    def test_explicit_anthropic_provider(self):
        os.environ['AI_DRAFT_PROVIDER'] = 'anthropic'
        self.assertEqual(ai_drafts._get_provider(), 'anthropic')

    def test_unknown_provider_falls_back_to_default_for_get_provider(self):
        os.environ['AI_DRAFT_PROVIDER'] = 'mystery-llm'
        # _get_provider() always returns a valid one for routing safety;
        # the user-facing 'invalid_provider' error is raised separately
        # by generate_draft() before _get_provider() is called.
        self.assertEqual(ai_drafts._get_provider(), 'gemini')

    def test_default_models_per_provider(self):
        self.assertEqual(ai_drafts._resolve_model('gemini'),
                         'gemini-2.5-flash-lite')
        self.assertEqual(ai_drafts._resolve_model('anthropic'),
                         'claude-sonnet-4-6')

    def test_unified_ai_draft_model_overrides_default(self):
        os.environ['AI_DRAFT_MODEL'] = 'gemini-2.0-flash'
        self.assertEqual(ai_drafts._resolve_model('gemini'),
                         'gemini-2.0-flash')
        # And applies to anthropic too:
        self.assertEqual(ai_drafts._resolve_model('anthropic'),
                         'gemini-2.0-flash')

    def test_legacy_anthropic_model_still_honored_for_anthropic(self):
        # When AI_DRAFT_MODEL is unset, ANTHROPIC_MODEL still works
        # for the anthropic path only.
        os.environ['ANTHROPIC_MODEL'] = 'claude-haiku-4-5-20251001'
        self.assertEqual(ai_drafts._resolve_model('anthropic'),
                         'claude-haiku-4-5-20251001')
        # Gemini is unaffected:
        self.assertEqual(ai_drafts._resolve_model('gemini'),
                         'gemini-2.5-flash-lite')


# ─────────────────────────────────────────────────────────────────────────
# 6 — missing API key → ai_not_configured
# ─────────────────────────────────────────────────────────────────────────

class MissingApiKeyTests(unittest.TestCase):

    def setUp(self):
        _reset_module_state()
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.booking = _seed_booking(db)

    def tearDown(self):
        _reset_module_state()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_gemini_no_key_returns_not_configured(self):
        # Default provider is gemini; no GEMINI_API_KEY set.
        result = generate_draft('booking_confirmed', self.booking)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'ai_not_configured')
        self.assertEqual(result['provider'], 'gemini')
        self.assertIn('not configured', result['message'].lower())

    def test_anthropic_no_key_returns_not_configured(self):
        os.environ['AI_DRAFT_PROVIDER'] = 'anthropic'
        result = generate_draft('booking_confirmed', self.booking)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'ai_not_configured')
        self.assertEqual(result['provider'], 'anthropic')

    def test_no_real_api_called_when_key_missing(self):
        # Patch the dispatcher boundary — assert it's NEVER called when
        # the key is absent (we short-circuit before dispatch).
        with mock.patch.object(ai_drafts, '_call_provider') as called:
            result = generate_draft('booking_confirmed', self.booking)
            self.assertFalse(result['success'])
            self.assertEqual(result['error'], 'ai_not_configured')
            called.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# 7 — invalid provider
# ─────────────────────────────────────────────────────────────────────────

class InvalidProviderTests(unittest.TestCase):

    def setUp(self):
        _reset_module_state()
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.booking = _seed_booking(db)

    def tearDown(self):
        _reset_module_state()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_invalid_provider_returns_invalid_provider_error(self):
        os.environ['AI_DRAFT_PROVIDER'] = 'mystery-llm'
        result = generate_draft('booking_confirmed', self.booking)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'invalid_provider')
        self.assertIn('mystery-llm', result['message'])

    def test_invalid_provider_does_not_call_api(self):
        os.environ['AI_DRAFT_PROVIDER'] = 'mystery-llm'
        with mock.patch.object(ai_drafts, '_call_provider') as called:
            generate_draft('booking_confirmed', self.booking)
            called.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# 8 — API failure handling for both providers
# ─────────────────────────────────────────────────────────────────────────

class GeminiCallTests(unittest.TestCase):

    def setUp(self):
        _reset_module_state()
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.booking = _seed_booking(db)
        os.environ['GEMINI_API_KEY'] = 'fake-test-key-do-not-use'

    def tearDown(self):
        _reset_module_state()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_gemini_success_path(self):
        # Mock requests.post to return a valid Gemini response.
        fake_resp = mock.MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            'candidates': [{
                'content': {'parts': [{'text': 'Dear Hassan, your booking is confirmed.'}]}
            }]
        }
        with mock.patch.object(ai_drafts, '_requests') as req:
            req.post.return_value = fake_resp
            result = generate_draft('booking_confirmed', self.booking)
        self.assertTrue(result['success'])
        self.assertEqual(result['provider'], 'gemini')
        self.assertIn('Dear Hassan', result['draft'])
        self.assertEqual(result['model'], 'gemini-2.5-flash-lite')

    def test_gemini_http_non_200_returns_unavailable(self):
        fake_resp = mock.MagicMock()
        fake_resp.status_code = 500
        fake_resp.text = 'irrelevant — must not be logged'
        with mock.patch.object(ai_drafts, '_requests') as req:
            req.post.return_value = fake_resp
            result = generate_draft('booking_confirmed', self.booking)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'ai_unavailable')
        self.assertEqual(result['provider'], 'gemini')

    def test_gemini_exception_returns_unavailable(self):
        with mock.patch.object(ai_drafts, '_requests') as req:
            req.post.side_effect = RuntimeError('network-down')
            result = generate_draft('booking_confirmed', self.booking)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'ai_unavailable')

    def test_gemini_empty_candidates_returns_empty_response(self):
        fake_resp = mock.MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {'candidates': []}
        with mock.patch.object(ai_drafts, '_requests') as req:
            req.post.return_value = fake_resp
            result = generate_draft('booking_confirmed', self.booking)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'ai_empty_response')


class AnthropicCallTests(unittest.TestCase):

    def setUp(self):
        _reset_module_state()
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.booking = _seed_booking(db)
        os.environ['AI_DRAFT_PROVIDER'] = 'anthropic'
        os.environ['ANTHROPIC_API_KEY'] = 'sk-test-fake'

    def tearDown(self):
        _reset_module_state()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _make_client(self, *, raises=None, body='ok'):
        client = mock.MagicMock()
        if raises is not None:
            client.messages.create.side_effect = raises
        else:
            block = mock.MagicMock()
            block.text = body
            resp = mock.MagicMock()
            resp.content = [block] if body else []
            client.messages.create.return_value = resp
        return client

    def test_anthropic_success(self):
        ai_drafts._anthropic_client = self._make_client(
            body='Dear Hassan, your booking is confirmed.',
        )
        result = generate_draft('booking_confirmed', self.booking)
        self.assertTrue(result['success'])
        self.assertEqual(result['provider'], 'anthropic')
        self.assertIn('Dear Hassan', result['draft'])
        self.assertEqual(result['model'], 'claude-sonnet-4-6')

    def test_anthropic_exception_returns_unavailable(self):
        ai_drafts._anthropic_client = self._make_client(
            raises=RuntimeError('boom'),
        )
        result = generate_draft('booking_confirmed', self.booking)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'ai_unavailable')
        self.assertEqual(result['provider'], 'anthropic')

    def test_anthropic_empty_response(self):
        ai_drafts._anthropic_client = self._make_client(body='')
        result = generate_draft('booking_confirmed', self.booking)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'ai_empty_response')


# ─────────────────────────────────────────────────────────────────────────
# 9, 10, 11, 12 — route auth + integrity + audit log
# ─────────────────────────────────────────────────────────────────────────

class RouteAuthAndIntegrityTests(unittest.TestCase):

    def setUp(self):
        _reset_module_state()
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        admin = User(username='admin1', email='a@x', role='admin')
        admin.set_password('a-very-strong-password-1!')
        staff = User(username='staff1', email='s@x', role='staff')
        staff.set_password('a-very-strong-password-1!')
        db.session.add_all([admin, staff])
        db.session.commit()
        self.admin_id = admin.id
        self.staff_id = staff.id
        self.booking = _seed_booking(db, status='confirmed',
                                     payment_status='paid',
                                     amount_paid=1200.0)
        self.booking_id = self.booking.id
        self.client = self.app.test_client()
        self._patch_dispatcher_for_success()

    def tearDown(self):
        _reset_module_state()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _patch_dispatcher_for_success(self):
        # Default: Gemini provider configured + dispatcher returns OK text.
        os.environ['AI_DRAFT_PROVIDER'] = 'gemini'
        os.environ['GEMINI_API_KEY'] = 'fake-test-key'
        self._dispatch_patcher = mock.patch.object(
            ai_drafts, '_call_provider',
            return_value={'success': True,
                          'text': 'Dear Hassan, your booking is confirmed.'},
        )
        self._dispatch_patcher.start()

    def _login(self, user_id):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True

    def test_anonymous_redirected(self):
        resp = self.client.post(
            f'/bookings/{self.booking_id}/ai-draft',
            data={'draft_type': 'booking_confirmed'},
        )
        self.assertIn(resp.status_code, (301, 302))

    def test_staff_blocked(self):
        self._login(self.staff_id)
        resp = self.client.post(
            f'/bookings/{self.booking_id}/ai-draft',
            data={'draft_type': 'booking_confirmed'},
        )
        self.assertNotEqual(resp.status_code, 200)

    def test_admin_can_generate_draft(self):
        self._login(self.admin_id)
        resp = self.client.post(
            f'/bookings/{self.booking_id}/ai-draft',
            data={'draft_type': 'booking_confirmed'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'AI-generated draft', resp.data)
        self.assertIn(b'Dear Hassan', resp.data)
        self.assertIn(b'gemini', resp.data)  # provider shown in panel

    # Test 10 — no whatsapp/email path is reachable from this route.
    def test_no_whatsapp_or_email_call(self):
        with mock.patch('app.services.whatsapp._send_template') as t, \
             mock.patch('app.services.whatsapp._send') as s:
            self._login(self.admin_id)
            self.client.post(
                f'/bookings/{self.booking_id}/ai-draft',
                data={'draft_type': 'booking_confirmed'},
            )
        t.assert_not_called()
        s.assert_not_called()

    def test_ai_drafts_module_does_not_import_whatsapp(self):
        import ast
        repo = Path(__file__).resolve().parent.parent
        ai_path = repo / 'app' / 'services' / 'ai_drafts.py'
        tree = ast.parse(ai_path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, 'module', '') or ''
                names = [n.name for n in node.names]
                blob = f'{module} :: {names}'
                self.assertNotIn('whatsapp', blob.lower(),
                                 f'ai_drafts.py imports whatsapp: {blob}')

    # Test 11 — no booking/payment/room mutation
    def test_no_state_mutation(self):
        before = (
            self.booking.status,
            self.booking.invoice.payment_status,
            self.booking.invoice.amount_paid,
            self.booking.room.status,
        )
        self._login(self.admin_id)
        self.client.post(
            f'/bookings/{self.booking_id}/ai-draft',
            data={'draft_type': 'booking_confirmed'},
        )
        b = Booking.query.get(self.booking_id)
        after = (
            b.status, b.invoice.payment_status,
            b.invoice.amount_paid, b.room.status,
        )
        self.assertEqual(before, after)

    # Test 12a — audit row has metadata only, no body, no prompt.
    def test_audit_log_has_metadata_only(self):
        self._login(self.admin_id)
        self.client.post(
            f'/bookings/{self.booking_id}/ai-draft',
            data={'draft_type': 'booking_confirmed'},
        )
        rows = ActivityLog.query.filter_by(action='ai.draft.created').all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.actor_type, 'admin')
        self.assertEqual(row.booking_id, self.booking_id)
        self.assertIn('booking_confirmed', row.description)
        # Body never persisted:
        self.assertNotIn('Dear Hassan', row.description or '')
        self.assertNotIn('Dear Hassan', row.metadata_json or '')

    # Test 12b — provider name IS recorded in metadata.
    def test_audit_metadata_includes_provider_and_model(self):
        self._login(self.admin_id)
        self.client.post(
            f'/bookings/{self.booking_id}/ai-draft',
            data={'draft_type': 'booking_confirmed'},
        )
        row = ActivityLog.query.filter_by(action='ai.draft.created').first()
        meta = (row.metadata_json or '')
        self.assertIn('"provider": "gemini"', meta)
        self.assertIn('"model"', meta)
        self.assertIn('"draft_type"', meta)
        self.assertIn('"booking_ref"', meta)

    # Test 13 — no secret-like keys in metadata.
    def test_metadata_has_no_secret_like_keys(self):
        self._login(self.admin_id)
        self.client.post(
            f'/bookings/{self.booking_id}/ai-draft',
            data={'draft_type': 'booking_confirmed'},
        )
        row = ActivityLog.query.filter_by(action='ai.draft.created').first()
        meta = (row.metadata_json or '').lower()
        for forbidden in ('api_key', 'apikey', 'gemini_api_key',
                          'anthropic_api_key', 'authorization',
                          'sk-test', 'fake-test'):
            self.assertNotIn(forbidden, meta,
                             f'metadata contains banned token: {forbidden!r}')

    def test_invalid_draft_type_redirects_no_log(self):
        self._login(self.admin_id)
        resp = self.client.post(
            f'/bookings/{self.booking_id}/ai-draft',
            data={'draft_type': 'evil_inject'},
        )
        self.assertIn(resp.status_code, (301, 302))
        rows = ActivityLog.query.filter_by(action='ai.draft.created').all()
        self.assertEqual(rows, [])

    def test_failed_generation_logs_ai_draft_failed(self):
        # Force the dispatcher to return a failure.
        self._dispatch_patcher.stop()
        with mock.patch.object(
            ai_drafts, '_call_provider',
            return_value={'success': False, 'error': 'ai_unavailable'},
        ):
            self._login(self.admin_id)
            self.client.post(
                f'/bookings/{self.booking_id}/ai-draft',
                data={'draft_type': 'booking_confirmed'},
            )
        ok = ActivityLog.query.filter_by(action='ai.draft.created').all()
        fail = ActivityLog.query.filter_by(action='ai.draft.failed').all()
        self.assertEqual(ok, [])
        self.assertEqual(len(fail), 1)
        # Provider still recorded on failure for ops debugging:
        self.assertIn('"provider"', fail[0].metadata_json or '')
        # Restart the auto-success patcher so tearDown doesn't double-stop.
        self._dispatch_patcher.start()


# ─────────────────────────────────────────────────────────────────────────
# 13 — disclaimer banner rendered to user
# ─────────────────────────────────────────────────────────────────────────

class DisclaimerSurfacingTests(unittest.TestCase):

    def test_disclaimer_constant_value(self):
        self.assertEqual(
            DRAFT_DISCLAIMER,
            'AI-generated draft — review before sending.',
        )

    def test_template_renders_disclaimer_on_success(self):
        _reset_module_state()
        app = _make_app()
        with app.app_context():
            db.create_all()
            admin = User(username='admin1', email='a@x', role='admin')
            admin.set_password('a-very-strong-password-1!')
            db.session.add(admin)
            db.session.commit()
            booking = _seed_booking(db, status='confirmed',
                                    payment_status='paid', amount_paid=1200.0)
            booking_id = booking.id
            admin_id = admin.id

            os.environ['AI_DRAFT_PROVIDER'] = 'gemini'
            os.environ['GEMINI_API_KEY'] = 'fake-test-key'
            with mock.patch.object(
                ai_drafts, '_call_provider',
                return_value={'success': True, 'text': 'Sample draft body.'},
            ):
                client_t = app.test_client()
                with client_t.session_transaction() as sess:
                    sess['_user_id'] = str(admin_id)
                    sess['_fresh'] = True
                r = client_t.post(
                    f'/bookings/{booking_id}/ai-draft',
                    data={'draft_type': 'booking_confirmed'},
                )
                self.assertEqual(r.status_code, 200)
                self.assertIn(b'AI-generated draft', r.data)
                self.assertIn(b'review before sending', r.data)
                self.assertIn(b'not sent automatically', r.data)
            db.drop_all()
            _reset_module_state()


# ─────────────────────────────────────────────────────────────────────────
# Wiring sanity
# ─────────────────────────────────────────────────────────────────────────

class WiringSmokeTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.repo = Path(__file__).resolve().parent.parent

    def test_route_registered(self):
        app = _make_app()
        rules = {r.endpoint for r in app.url_map.iter_rules()}
        self.assertIn('bookings.ai_draft', rules)
        self.assertIn('bookings.detail', rules)

    def test_ai_drafts_imported_lazily_in_routes(self):
        src = (self.repo / 'app' / 'routes' / 'bookings.py').read_text()
        head = '\n'.join(src.splitlines()[:20])
        self.assertNotIn('from ..services.ai_drafts', head,
                         'ai_drafts must be imported lazily inside handlers')


if __name__ == '__main__':
    unittest.main()
