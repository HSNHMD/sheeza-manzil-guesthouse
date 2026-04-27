"""Tests for the AI Draft Assistant V1.

Covers:
  1. Draft-type whitelist contains exactly the 9 V1 types.
  2. Invalid draft_type is rejected by route + service.
  3. Prompt builder NEVER includes passport/ID number, address, or full
     uploaded filename even when the booking carries those values.
  4. Prompt builder DOES include booking_ref / room / dates / total when
     present.
  5. Missing ANTHROPIC_API_KEY → service returns ai_not_configured (no
     crash, no API call attempted).
  6. AI service errors are caught and surfaced as ai_unavailable, not
     re-raised.
  7. Route requires admin login (anonymous → redirect to /console;
     non-admin → 403).
  8. Route does not import _send / _send_template; no WhatsApp/email
     send is possible from the AI draft path.
  9. Route does not mutate booking.status / invoice.payment_status /
     room.status / amount_paid.
 10. ActivityLog receives ai.draft.created with metadata only (no draft
     body, no prompt text).
 11. Banned-key sanitizer drops anything containing api_key|token|secret
     in case future code regresses (smoke check).
 12. Service output is presented to the user with the 'AI-generated draft —
     review before sending.' banner (template renders the constant).

These tests use an in-memory SQLite DB and mock the Anthropic SDK so no
real API calls are made. They pass without ANTHROPIC_API_KEY set.
"""

from __future__ import annotations

import os
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

# Clean env BEFORE app import. We want NO live anthropic key during tests.
os.environ.pop('DATABASE_URL', None)
os.environ.pop('ANTHROPIC_API_KEY', None)
os.environ.pop('ANTHROPIC_MODEL', None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                         # noqa: E402
from app import create_app                                        # noqa: E402
from app.models import db, User, Room, Guest, Booking, Invoice    # noqa: E402
from app.models import ActivityLog                                # noqa: E402
from app.services import ai_drafts                                # noqa: E402
from app.services.ai_drafts import (                              # noqa: E402
    DRAFT_TYPES,
    DRAFT_LABELS,
    DRAFT_DISCLAIMER,
    build_prompt,
    can_draft,
    generate_draft,
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
    """Insert a fully-populated test booking and return it."""
    room = Room(number='99', name='Test Room', room_type='Test',
                floor=0, capacity=2, price_per_night=600.0)
    guest = Guest(
        first_name='Hassan', last_name='Hamid',
        phone='+9607000000',
        email='hassan@example.com',
        nationality='Maldivian',
        # PII that must NOT leak into the prompt:
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


class DraftTypeWhitelistTests(unittest.TestCase):
    """Tests 1, 2 — whitelist + invalid type rejection."""

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
        # No app context needed for this — service short-circuits.
        result = generate_draft('not_a_real_type', booking=None)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'invalid_draft_type')


class PromptBuilderPrivacyTests(unittest.TestCase):
    """Tests 3, 4 — privacy + content correctness of the prompt builder."""

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
        # The PRIVATE thing is the actual passport NUMBER value — this
        # must never appear in a prompt regardless of draft type.
        # The word "passport" itself is fine in instructions (e.g.
        # missing_id tells Claude to mention "ID or passport").
        for dt in DRAFT_TYPES:
            prompt = build_prompt(dt, self.booking)
            self.assertNotIn('ABCDE12345', prompt,
                             f'{dt}: passport number value leaked')

    def test_prompt_does_not_contain_id_type_or_number_field_names(self):
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
        # Filename strings include random hex suffix — the prompt should
        # only carry the boolean has_id_card_uploaded / has_payment_slip_uploaded.
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
        # System prompt is shared — assert the no-guess instruction exists.
        sys_prompt = ai_drafts._SYSTEM_PROMPT
        self.assertIn('Use ONLY the booking facts', sys_prompt)
        self.assertIn('admin: please verify', sys_prompt)
        self.assertIn('Sheeza Manzil', sys_prompt)

    def test_unknown_draft_type_raises_on_build(self):
        with self.assertRaises(ValueError):
            build_prompt('not_real', self.booking)


class StateGatingTests(unittest.TestCase):
    """can_draft soft-gate behavior."""

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

    def test_unknown_draft_type_returns_true_by_default(self):
        b = _seed_booking(db)
        # Future-proof: unknown types pass the soft-gate; route still
        # validates against DRAFT_TYPES allow-list.
        self.assertTrue(can_draft(b, 'future_unknown_type'))


class MissingApiKeyTests(unittest.TestCase):
    """Test 5 — clean degradation when key is absent."""

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.booking = _seed_booking(db)
        # Wipe singleton + env to simulate fresh start without key.
        ai_drafts._client = None
        os.environ.pop('ANTHROPIC_API_KEY', None)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_no_key_returns_not_configured(self):
        result = generate_draft('booking_confirmed', self.booking)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'ai_not_configured')
        self.assertIn('not configured', result['message'].lower())

    def test_no_key_does_not_call_anthropic(self):
        # If the SDK were called, it would raise auth errors — mock to
        # detect any accidental call.
        with mock.patch.object(ai_drafts, 'anthropic') as mocked_sdk:
            result = generate_draft('booking_confirmed', self.booking)
            self.assertFalse(result['success'])
            self.assertEqual(result['error'], 'ai_not_configured')
            mocked_sdk.Anthropic.assert_not_called()


class ApiFailureTests(unittest.TestCase):
    """Test 6 — API errors are caught and converted to safe error dict."""

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.booking = _seed_booking(db)
        os.environ['ANTHROPIC_API_KEY'] = 'sk-test-fake-do-not-use'
        ai_drafts._client = None  # force re-init

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        os.environ.pop('ANTHROPIC_API_KEY', None)
        ai_drafts._client = None

    def test_api_exception_returns_unavailable(self):
        fake_client = mock.MagicMock()
        fake_client.messages.create.side_effect = RuntimeError('boom')
        ai_drafts._client = fake_client

        result = generate_draft('booking_confirmed', self.booking)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'ai_unavailable')

    def test_empty_response_returns_empty_error(self):
        empty_resp = mock.MagicMock()
        empty_resp.content = []
        fake_client = mock.MagicMock()
        fake_client.messages.create.return_value = empty_resp
        ai_drafts._client = fake_client

        result = generate_draft('booking_confirmed', self.booking)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'ai_empty_response')

    def test_successful_response_extracts_text(self):
        fake_block = mock.MagicMock()
        fake_block.text = 'Dear Hassan, your booking BKAITEST is confirmed.'
        fake_resp = mock.MagicMock()
        fake_resp.content = [fake_block]
        fake_client = mock.MagicMock()
        fake_client.messages.create.return_value = fake_resp
        ai_drafts._client = fake_client

        result = generate_draft('booking_confirmed', self.booking)
        self.assertTrue(result['success'])
        self.assertIn('Dear Hassan', result['draft'])
        self.assertEqual(result['draft_type'], 'booking_confirmed')
        self.assertGreater(result['length_chars'], 0)
        self.assertTrue(result['model'])


class RouteAuthAndIntegrityTests(unittest.TestCase):
    """Tests 7, 8, 9, 10 — auth gating, no-send, no mutation, audit log."""

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        # Seed admin + staff users
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
        # Wire a fake successful Anthropic response by default.
        self._patch_client_for_success()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        ai_drafts._client = None
        os.environ.pop('ANTHROPIC_API_KEY', None)

    def _patch_client_for_success(self):
        os.environ['ANTHROPIC_API_KEY'] = 'sk-test-fake'
        block = mock.MagicMock()
        block.text = 'Dear Hassan, your booking BKAITEST is confirmed.'
        resp = mock.MagicMock()
        resp.content = [block]
        client = mock.MagicMock()
        client.messages.create.return_value = resp
        ai_drafts._client = client

    def _login(self, user_id):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True

    # — Test 7 —
    def test_anonymous_redirected_to_login(self):
        resp = self.client.post(
            f'/bookings/{self.booking_id}/ai-draft',
            data={'draft_type': 'booking_confirmed'},
        )
        self.assertIn(resp.status_code, (301, 302))
        self.assertNotEqual(resp.status_code, 200)

    def test_staff_gets_403(self):
        self._login(self.staff_id)
        resp = self.client.post(
            f'/bookings/{self.booking_id}/ai-draft',
            data={'draft_type': 'booking_confirmed'},
        )
        # staff guard intercepts non-admin staff; either 403 or redirect away
        self.assertNotEqual(resp.status_code, 200)

    def test_admin_can_generate_draft(self):
        self._login(self.admin_id)
        resp = self.client.post(
            f'/bookings/{self.booking_id}/ai-draft',
            data={'draft_type': 'booking_confirmed'},
        )
        self.assertEqual(resp.status_code, 200)
        # The response renders the booking detail page with the draft panel.
        self.assertIn(b'AI-generated draft', resp.data)
        self.assertIn(b'Dear Hassan', resp.data)

    # — Test 8 (route does not import / call _send) —
    def test_route_module_does_not_import_whatsapp_send_for_ai(self):
        # Static check: ensure the AI service file does not IMPORT the
        # WhatsApp send helpers. We scan executable lines only — comments
        # and docstrings are stripped first so prose mentions like
        # "this module does NOT call _send_template" don't trip the test.
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
            if isinstance(node, ast.Call):
                # Catch any call whose function name string is a known
                # WhatsApp helper (would only matter if imported, but
                # belt-and-braces).
                func_name = ''
                if isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                elif isinstance(node.func, ast.Name):
                    func_name = node.func.id
                self.assertNotIn(func_name, {
                    '_send', '_send_template',
                    'send_booking_confirmation', 'send_booking_acknowledgment',
                    'send_staff_new_booking_notification',
                    'send_checkin_reminder', 'send_checkout_invoice_summary',
                }, f'ai_drafts.py calls a WhatsApp send fn: {func_name}')

    def test_no_actual_whatsapp_call_during_route_run(self):
        # Patch the WhatsApp send paths and assert they are NEVER invoked
        # during an AI draft request.
        with mock.patch('app.services.whatsapp._send_template') as send_tpl, \
             mock.patch('app.services.whatsapp._send') as send_text:
            self._login(self.admin_id)
            self.client.post(
                f'/bookings/{self.booking_id}/ai-draft',
                data={'draft_type': 'booking_confirmed'},
            )
        send_tpl.assert_not_called()
        send_text.assert_not_called()

    # — Test 9 (no booking/payment mutation) —
    def test_no_booking_or_invoice_mutation(self):
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
        # Reload from DB to be safe
        b = Booking.query.get(self.booking_id)
        after = (
            b.status, b.invoice.payment_status,
            b.invoice.amount_paid, b.room.status,
        )
        self.assertEqual(before, after,
                         'AI draft route must not mutate booking/invoice/room')

    # — Test 10 (ActivityLog logged WITHOUT draft body) —
    def test_activity_log_records_draft_event_without_body(self):
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
        # Description references the draft TYPE, never the body.
        self.assertIn('booking_confirmed', row.description)
        # Critical: full draft text must NOT be in the audit row anywhere.
        self.assertNotIn('Dear Hassan', row.description or '')
        self.assertNotIn('Dear Hassan', row.metadata_json or '')

    # — Test 11 (banned-key smoke check on metadata_json) —
    def test_metadata_json_does_not_leak_secret_keys(self):
        self._login(self.admin_id)
        self.client.post(
            f'/bookings/{self.booking_id}/ai-draft',
            data={'draft_type': 'booking_confirmed'},
        )
        row = ActivityLog.query.filter_by(action='ai.draft.created').first()
        meta = (row.metadata_json or '').lower()
        for forbidden in ('api_key', 'secret', 'token', 'credential',
                          'sk-test', 'authorization'):
            self.assertNotIn(forbidden, meta,
                             f'metadata_json contains banned token: {forbidden!r}')

    def test_invalid_draft_type_returns_redirect_no_log(self):
        self._login(self.admin_id)
        resp = self.client.post(
            f'/bookings/{self.booking_id}/ai-draft',
            data={'draft_type': 'evil_inject'},
        )
        self.assertIn(resp.status_code, (301, 302))
        # No audit row should be written for invalid types
        rows = ActivityLog.query.filter_by(action='ai.draft.created').all()
        self.assertEqual(rows, [])

    def test_failed_generation_logs_ai_draft_failed(self):
        # Force the Anthropic client to raise.
        ai_drafts._client.messages.create.side_effect = RuntimeError('fail')
        self._login(self.admin_id)
        self.client.post(
            f'/bookings/{self.booking_id}/ai-draft',
            data={'draft_type': 'booking_confirmed'},
        )
        ok_rows = ActivityLog.query.filter_by(action='ai.draft.created').all()
        fail_rows = ActivityLog.query.filter_by(action='ai.draft.failed').all()
        self.assertEqual(ok_rows, [])
        self.assertEqual(len(fail_rows), 1)


class DisclaimerSurfacingTests(unittest.TestCase):
    """Test 12 — banner is shown to the user."""

    def test_disclaimer_constant_value(self):
        self.assertEqual(
            DRAFT_DISCLAIMER,
            'AI-generated draft — review before sending.',
        )

    def test_template_renders_disclaimer_on_success(self):
        # Smoke-render a booking detail page with a fake successful draft;
        # assert the banner text appears.
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

            os.environ['ANTHROPIC_API_KEY'] = 'sk-test-fake'
            block = mock.MagicMock()
            block.text = 'Sample draft body.'
            resp = mock.MagicMock()
            resp.content = [block]
            client = mock.MagicMock()
            client.messages.create.return_value = resp
            ai_drafts._client = client

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
            self.assertNotIn(b'<form method="POST" action="/bookings/'
                             + str(booking_id).encode() + b'/send"', r.data,
                             'AI draft panel must not include any send form')

            db.drop_all()
            ai_drafts._client = None
            os.environ.pop('ANTHROPIC_API_KEY', None)


class WiringSmokeTests(unittest.TestCase):
    """Confirm imports + route registration without running a request."""

    @classmethod
    def setUpClass(cls):
        cls.repo = Path(__file__).resolve().parent.parent

    def test_route_registered(self):
        app = _make_app()
        rules = {r.endpoint for r in app.url_map.iter_rules()}
        self.assertIn('bookings.ai_draft', rules)
        self.assertIn('bookings.detail', rules)

    def test_bookings_route_imports_ai_drafts_only_inside_handler(self):
        # The AI service is imported inside detail() / ai_draft() to keep
        # module load cheap and to avoid a hard dep on anthropic at import
        # time. Confirm the imports are NOT at module top level.
        src = (self.repo / 'app' / 'routes' / 'bookings.py').read_text()
        # Top-of-file imports section ends at the first blank line after
        # the imports. Just check 'from ..services.ai_drafts import' does
        # NOT appear before line 20 (where the imports block ends).
        head = '\n'.join(src.splitlines()[:20])
        self.assertNotIn('from ..services.ai_drafts', head,
                         'ai_drafts must be imported lazily inside handlers')


if __name__ == '__main__':
    unittest.main()
