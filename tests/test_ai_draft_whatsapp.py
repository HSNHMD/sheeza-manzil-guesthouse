"""Tests for AI Draft Approval + Manual WhatsApp Send (V2).

Covers (per spec):
  1.  Send route requires login/admin.
  2.  Missing phone blocks send.
  3.  Empty message blocks send.
  4.  Placeholder text ("[admin: ...") blocks send.
  5.  Overlong message (>1500) blocks send.
  6.  Valid message calls mocked send_text_message exactly once.
  7.  Route does not call Gemini / import ai_drafts.
  8.  Route does not modify booking.status / invoice.payment_status.
  9.  ActivityLog logs attempt + sent without full body / full phone.
 10.  Mocked failure logs attempt + failed without raw response.
 11.  Metadata stores recipient_phone_last4 only.
 12.  No real WhatsApp API call in tests (HTTP mocked at lowest layer too).
 13.  Template smoke: editable textarea + warning + Send button rendered.
 14.  send_text_message wrapper: validation paths + error_class mapping.
 15.  AST static check: route handler does NOT import ai_drafts.

All tests use an in-memory SQLite DB, mock the WhatsApp transport, and pass
without WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID / WHATSAPP_ENABLED set.
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

# Clean env BEFORE app import — no live keys, no DATABASE_URL.
for _v in ('DATABASE_URL',
           'AI_DRAFT_PROVIDER', 'AI_DRAFT_MODEL',
           'GEMINI_API_KEY', 'ANTHROPIC_API_KEY', 'ANTHROPIC_MODEL',
           'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN',
           'WHATSAPP_PHONE_NUMBER_ID', 'WHATSAPP_PHONE_ID'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                            # noqa: E402
from app import create_app                                           # noqa: E402
from app.models import (                                             # noqa: E402
    db, User, Room, Guest, Booking, Invoice, ActivityLog,
)
from app.services import whatsapp as wa                              # noqa: E402


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    SQLALCHEMY_ENGINE_OPTIONS = {'pool_pre_ping': True, 'pool_recycle': 300}
    WTF_CSRF_ENABLED = False
    # Defensive: even if a mock is missed, the underlying _send() will
    # short-circuit on this and return a 'config_disabled'-ish error.
    WHATSAPP_ENABLED = False


def _make_app():
    return create_app(_TestConfig)


def _seed_booking(db_, *, with_phone=True, status='confirmed',
                  payment_status='paid', amount_paid=1200.0):
    room = Room(number='99', name='Test Room', room_type='Test',
                floor=0, capacity=2, price_per_night=600.0)
    guest = Guest(
        first_name='Hassan', last_name='Hamid',
        phone='+9607001234' if with_phone else None,
        # PII that must NOT leak into audit metadata.
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
        num_guests=2, total_amount=1200.0,
        status=status,
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


# Body content used as the "edited" admin draft. Includes a recognizable
# substring so we can prove it never appears in any audit row.
_REAL_BODY = (
    'Dear Hassan, your booking BKAITEST is confirmed. We look forward to '
    'welcoming you on Friday. Sheeza Manzil Guesthouse.'
)
_BODY_TELL = 'Dear Hassan, your booking BKAITEST is confirmed'


# ─────────────────────────────────────────────────────────────────────────
# 14, 15 — wrapper unit tests (no Flask app needed)
# ─────────────────────────────────────────────────────────────────────────

class WrapperValidationTests(unittest.TestCase):
    """send_text_message validation paths — no HTTP call."""

    def test_missing_phone_returns_validation_phone(self):
        with mock.patch.object(wa, '_send') as m:
            r = wa.send_text_message('', 'hi')
            self.assertFalse(r['success'])
            self.assertEqual(r['error_class'], 'validation_phone')
            m.assert_not_called()

    def test_none_phone_returns_validation_phone(self):
        with mock.patch.object(wa, '_send') as m:
            r = wa.send_text_message(None, 'hi')
            self.assertFalse(r['success'])
            self.assertEqual(r['error_class'], 'validation_phone')
            m.assert_not_called()

    def test_empty_body_returns_validation_body(self):
        with mock.patch.object(wa, '_send') as m:
            r = wa.send_text_message('+9607001234', '')
            self.assertFalse(r['success'])
            self.assertEqual(r['error_class'], 'validation_body')
            m.assert_not_called()

    def test_whitespace_body_returns_validation_body(self):
        with mock.patch.object(wa, '_send') as m:
            r = wa.send_text_message('+9607001234', '    \n\t  ')
            self.assertFalse(r['success'])
            self.assertEqual(r['error_class'], 'validation_body')
            m.assert_not_called()

    def test_overlong_body_returns_validation_too_long(self):
        with mock.patch.object(wa, '_send') as m:
            r = wa.send_text_message('+9607001234', 'x' * 1501)
            self.assertFalse(r['success'])
            self.assertEqual(r['error_class'], 'validation_too_long')
            m.assert_not_called()

    def test_exactly_1500_body_passes_validation(self):
        # Boundary: 1500 should pass; _send is mocked to return success.
        with mock.patch.object(wa, '_send') as m:
            m.return_value = {
                'success': True, 'status_code': 200,
                'response_body': '{"messages":[{"id":"wamid.test"}]}',
                'error': None,
            }
            r = wa.send_text_message('+9607001234', 'x' * 1500)
            self.assertTrue(r['success'])
            self.assertEqual(r['message_id'], 'wamid.test')
            m.assert_called_once()


class WrapperSuccessParsingTests(unittest.TestCase):
    """send_text_message parses the wamid.… message_id correctly."""

    def test_parses_wamid_from_response(self):
        with mock.patch.object(wa, '_send') as m:
            m.return_value = {
                'success': True, 'status_code': 200,
                'response_body': json.dumps({
                    'messaging_product': 'whatsapp',
                    'contacts': [{'input': '...', 'wa_id': '...'}],
                    'messages': [{'id': 'wamid.HBgLOTYwNzAwMTIzNBUC'}],
                }),
                'error': None,
            }
            r = wa.send_text_message('+9607001234', 'hi')
            self.assertTrue(r['success'])
            self.assertEqual(r['message_id'], 'wamid.HBgLOTYwNzAwMTIzNBUC')
            self.assertIsNone(r['error_class'])

    def test_handles_malformed_success_body_gracefully(self):
        with mock.patch.object(wa, '_send') as m:
            m.return_value = {
                'success': True, 'status_code': 200,
                'response_body': 'not-json',
                'error': None,
            }
            r = wa.send_text_message('+9607001234', 'hi')
            self.assertTrue(r['success'])
            self.assertIsNone(r['message_id'])


class WrapperErrorClassMappingTests(unittest.TestCase):
    """_classify_send_error mapping for every documented error_class."""

    def _call_with_send_result(self, raw):
        with mock.patch.object(wa, '_send', return_value=raw):
            return wa.send_text_message('+9607001234', 'hi')

    def test_config_disabled(self):
        r = self._call_with_send_result({
            'success': False, 'status_code': None,
            'response_body': None,
            'error': 'WHATSAPP_ENABLED is not true',
        })
        self.assertEqual(r['error_class'], 'config_disabled')

    def test_config_invalid_token(self):
        r = self._call_with_send_result({
            'success': False, 'status_code': None,
            'response_body': None,
            'error': 'WHATSAPP_TOKEN is not set',
        })
        self.assertEqual(r['error_class'], 'config_invalid')

    def test_meta_window_closed_131047(self):
        r = self._call_with_send_result({
            'success': False, 'status_code': 400,
            'response_body': '{"error":{"code":131047,"message":"Re-engagement..."}}',
            'error': 'HTTP 400',
        })
        self.assertEqual(r['error_class'], 'meta_window_closed')

    def test_meta_token_invalid_401(self):
        r = self._call_with_send_result({
            'success': False, 'status_code': 401,
            'response_body': '{"error":{"code":190}}',
            'error': 'HTTP 401',
        })
        self.assertEqual(r['error_class'], 'meta_token_invalid')

    def test_meta_other_500(self):
        r = self._call_with_send_result({
            'success': False, 'status_code': 500,
            'response_body': '{"error":{"code":1}}',
            'error': 'HTTP 500',
        })
        self.assertEqual(r['error_class'], 'meta_other')

    def test_network_error_no_status(self):
        r = self._call_with_send_result({
            'success': False, 'status_code': None,
            'response_body': None,
            'error': 'Connection refused',
        })
        self.assertEqual(r['error_class'], 'network_error')


class WrapperConfigDisabledIntegrationTest(unittest.TestCase):
    """End-to-end (no HTTP) check that send_text_message returns
    'config_disabled' when WHATSAPP_ENABLED is unset — _check_config
    short-circuits before any HTTP call."""

    def setUp(self):
        for v in ('WHATSAPP_ENABLED', 'WHATSAPP_TOKEN',
                  'WHATSAPP_PHONE_NUMBER_ID', 'WHATSAPP_PHONE_ID'):
            os.environ.pop(v, None)

    def test_no_env_returns_config_disabled(self):
        # Patch _requests.post to assert NO HTTP attempt is made.
        with mock.patch.object(wa, '_requests') as req:
            r = wa.send_text_message('+9607001234', 'hi')
            req.post.assert_not_called()
        self.assertFalse(r['success'])
        self.assertEqual(r['error_class'], 'config_disabled')


# ─────────────────────────────────────────────────────────────────────────
# Route-level tests (Flask test client + DB)
# ─────────────────────────────────────────────────────────────────────────

class RouteAuthTests(unittest.TestCase):
    """Test 1 — auth gating."""

    def setUp(self):
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
        self.booking = _seed_booking(db)
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True

    def test_anonymous_redirected_to_login(self):
        with mock.patch.object(wa, 'send_text_message') as m:
            r = self.client.post(
                f'/bookings/{self.booking.id}/ai-draft/send-whatsapp',
                data={'message_body': _REAL_BODY},
            )
        self.assertIn(r.status_code, (301, 302))
        m.assert_not_called()

    def test_staff_blocked(self):
        self._login(self.staff_id)
        with mock.patch.object(wa, 'send_text_message') as m:
            r = self.client.post(
                f'/bookings/{self.booking.id}/ai-draft/send-whatsapp',
                data={'message_body': _REAL_BODY},
            )
        self.assertNotEqual(r.status_code, 200)
        m.assert_not_called()


class _RouteTestBase(unittest.TestCase):
    """Shared setup for route validation/integrity/audit tests."""

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        admin = User(username='admin1', email='a@x', role='admin')
        admin.set_password('a-very-strong-password-1!')
        db.session.add(admin)
        db.session.commit()
        self.admin_id = admin.id
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.admin_id)
            sess['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()


class ValidationTests(_RouteTestBase):
    """Tests 2, 3, 4, 5 — server-side validation rejects bad payloads
    BEFORE any audit row is written and BEFORE any send is attempted."""

    def test_missing_phone_blocks_send(self):
        booking = _seed_booking(db, with_phone=False)
        with mock.patch.object(wa, 'send_text_message') as m:
            r = self.client.post(
                f'/bookings/{booking.id}/ai-draft/send-whatsapp',
                data={'message_body': _REAL_BODY},
                follow_redirects=False,
            )
        self.assertIn(r.status_code, (301, 302))
        m.assert_not_called()
        # No audit row of any kind for invalid input
        self.assertEqual(
            ActivityLog.query.filter(
                ActivityLog.action.like('ai.draft.whatsapp_%')
            ).count(),
            0,
        )

    def test_empty_message_blocks_send(self):
        booking = _seed_booking(db)
        with mock.patch.object(wa, 'send_text_message') as m:
            r = self.client.post(
                f'/bookings/{booking.id}/ai-draft/send-whatsapp',
                data={'message_body': '   '},
                follow_redirects=False,
            )
        self.assertIn(r.status_code, (301, 302))
        m.assert_not_called()
        self.assertEqual(
            ActivityLog.query.filter(
                ActivityLog.action.like('ai.draft.whatsapp_%')
            ).count(),
            0,
        )

    def test_placeholder_text_blocks_send(self):
        booking = _seed_booking(db)
        body = (
            'Hi, please pay to [admin: please paste current bank details]. '
            'Thanks.'
        )
        with mock.patch.object(wa, 'send_text_message') as m:
            r = self.client.post(
                f'/bookings/{booking.id}/ai-draft/send-whatsapp',
                data={'message_body': body},
                follow_redirects=False,
            )
        self.assertIn(r.status_code, (301, 302))
        m.assert_not_called()
        self.assertEqual(
            ActivityLog.query.filter(
                ActivityLog.action.like('ai.draft.whatsapp_%')
            ).count(),
            0,
        )

    def test_overlong_message_blocks_send(self):
        booking = _seed_booking(db)
        with mock.patch.object(wa, 'send_text_message') as m:
            r = self.client.post(
                f'/bookings/{booking.id}/ai-draft/send-whatsapp',
                data={'message_body': 'x' * 1501},
                follow_redirects=False,
            )
        self.assertIn(r.status_code, (301, 302))
        m.assert_not_called()
        self.assertEqual(
            ActivityLog.query.filter(
                ActivityLog.action.like('ai.draft.whatsapp_%')
            ).count(),
            0,
        )

    def test_body_with_real_bank_details_passes_placeholder_guard(self):
        # Hotfix verification: a draft that includes the real Sheeza Manzil
        # bank details (and no '[admin:' placeholder) should pass the
        # placeholder guard and reach the wrapper.
        booking = _seed_booking(db)
        body = (
            'Dear Hassan, please send your payment to:\n'
            'Account Name: SHEEZA IMAD/MOHAMED S.R.\n'
            'Account Number: 7770000212622\n'
            'Then send us the payment slip. Sheeza Manzil Guesthouse.'
        )
        self.assertNotIn('[admin:', body.lower())
        with mock.patch.object(
            wa, 'send_text_message',
            return_value={'success': True, 'message_id': 'wamid.x',
                          'error_class': None},
        ) as m:
            r = self.client.post(
                f'/bookings/{booking.id}/ai-draft/send-whatsapp',
                data={'message_body': body},
                follow_redirects=False,
            )
        self.assertIn(r.status_code, (301, 302))
        m.assert_called_once()


class SuccessPathTests(_RouteTestBase):
    """Tests 6, 8, 9, 11 — happy path."""

    def test_valid_message_calls_wrapper_once(self):
        booking = _seed_booking(db)
        with mock.patch.object(
            wa, 'send_text_message',
            return_value={
                'success': True,
                'message_id': 'wamid.test123',
                'error_class': None,
            },
        ) as m:
            r = self.client.post(
                f'/bookings/{booking.id}/ai-draft/send-whatsapp',
                data={'message_body': _REAL_BODY,
                      'draft_type': 'booking_confirmed'},
                follow_redirects=False,
            )
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(m.call_count, 1)
        # Wrapper got the booking's phone + the edited body
        args, _kw = m.call_args
        self.assertEqual(args[0], '+9607001234')
        self.assertEqual(args[1], _REAL_BODY)

    def test_no_booking_or_invoice_mutation(self):
        booking = _seed_booking(db)
        before = (
            booking.status, booking.invoice.payment_status,
            booking.invoice.amount_paid, booking.room.status,
        )
        with mock.patch.object(
            wa, 'send_text_message',
            return_value={'success': True, 'message_id': 'wamid.x',
                          'error_class': None},
        ):
            self.client.post(
                f'/bookings/{booking.id}/ai-draft/send-whatsapp',
                data={'message_body': _REAL_BODY},
            )
        b = Booking.query.get(booking.id)
        after = (
            b.status, b.invoice.payment_status,
            b.invoice.amount_paid, b.room.status,
        )
        self.assertEqual(before, after)

    def test_audit_attempt_and_sent_with_safe_metadata(self):
        booking = _seed_booking(db)
        with mock.patch.object(
            wa, 'send_text_message',
            return_value={
                'success': True,
                'message_id': 'wamid.HBgLAA',
                'error_class': None,
            },
        ):
            self.client.post(
                f'/bookings/{booking.id}/ai-draft/send-whatsapp',
                data={'message_body': _REAL_BODY,
                      'draft_type': 'booking_confirmed'},
            )

        attempt = ActivityLog.query.filter_by(
            action='ai.draft.whatsapp_send_attempt'
        ).all()
        sent = ActivityLog.query.filter_by(
            action='ai.draft.whatsapp_sent'
        ).all()
        failed = ActivityLog.query.filter_by(
            action='ai.draft.whatsapp_failed'
        ).all()
        self.assertEqual(len(attempt), 1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(failed, [])

        # Both rows have admin actor and link to the right booking
        for row in (attempt[0], sent[0]):
            self.assertEqual(row.actor_type, 'admin')
            self.assertEqual(row.booking_id, booking.id)
            self.assertEqual(row.invoice_id, booking.invoice.id)

        # Privacy: full body never appears anywhere in either row
        for row in (attempt[0], sent[0]):
            for blob in (row.description or '', row.metadata_json or ''):
                self.assertNotIn(_BODY_TELL, blob,
                                 f'body leaked into {row.action} {blob!r}')
                self.assertNotIn('Dear Hassan', blob)
                self.assertNotIn('+9607001234', blob)         # full phone
                self.assertNotIn('9607001234', blob)          # digits-only phone
                # Last4 IS expected in metadata_json — but only the last4
                self.assertNotIn('700123', blob)              # 6-digit fragment
                # Banned tokens
                for forbidden in ('api_key', 'authorization',
                                  'whatsapp_token', 'bearer'):
                    self.assertNotIn(forbidden, blob.lower())

        # Verify metadata_json structure on the success row
        sent_meta = json.loads(sent[0].metadata_json)
        self.assertEqual(sent_meta['booking_ref'], 'BKAITEST')
        self.assertEqual(sent_meta['draft_type'], 'booking_confirmed')
        self.assertEqual(sent_meta['provider'], 'whatsapp')
        self.assertEqual(sent_meta['recipient_phone_last4'], '1234')
        self.assertEqual(sent_meta['message_length'], len(_REAL_BODY))
        self.assertEqual(sent_meta['whatsapp_message_id'], 'wamid.HBgLAA')
        self.assertTrue(sent_meta['success'])
        # Whitelist enforcement: no surprise keys
        self.assertEqual(
            set(sent_meta.keys()),
            {'booking_ref', 'draft_type', 'provider',
             'recipient_phone_last4', 'message_length',
             'whatsapp_message_id', 'success'},
        )


class FailurePathTests(_RouteTestBase):
    """Test 10 — failure path: 1 attempt + 1 failed; no raw response data."""

    def test_meta_window_closed_logs_failed_with_error_class(self):
        booking = _seed_booking(db)
        with mock.patch.object(
            wa, 'send_text_message',
            return_value={
                'success': False,
                'message_id': None,
                'error_class': 'meta_window_closed',
            },
        ) as m:
            r = self.client.post(
                f'/bookings/{booking.id}/ai-draft/send-whatsapp',
                data={'message_body': _REAL_BODY,
                      'draft_type': 'booking_confirmed'},
            )
        self.assertIn(r.status_code, (301, 302))
        m.assert_called_once()

        attempt = ActivityLog.query.filter_by(
            action='ai.draft.whatsapp_send_attempt').count()
        sent = ActivityLog.query.filter_by(
            action='ai.draft.whatsapp_sent').count()
        failed = ActivityLog.query.filter_by(
            action='ai.draft.whatsapp_failed').all()

        self.assertEqual(attempt, 1)
        self.assertEqual(sent, 0)
        self.assertEqual(len(failed), 1)

        meta = json.loads(failed[0].metadata_json)
        self.assertEqual(meta['error_class'], 'meta_window_closed')
        self.assertFalse(meta['success'])
        self.assertEqual(meta['recipient_phone_last4'], '1234')
        # Privacy: no body, no full phone, no raw response
        for blob in (failed[0].description or '', failed[0].metadata_json or ''):
            self.assertNotIn(_BODY_TELL, blob)
            self.assertNotIn('+9607001234', blob)
            self.assertNotIn('9607001234', blob)
            self.assertNotIn('131047', blob)   # raw Meta error code stays out
            self.assertNotIn('re-engagement', blob.lower())

    def test_config_disabled_friendly_message(self):
        booking = _seed_booking(db)
        with mock.patch.object(
            wa, 'send_text_message',
            return_value={
                'success': False,
                'message_id': None,
                'error_class': 'config_disabled',
            },
        ):
            r = self.client.post(
                f'/bookings/{booking.id}/ai-draft/send-whatsapp',
                data={'message_body': _REAL_BODY},
                follow_redirects=True,
            )
        # Follow the redirect; assert the friendly message reaches the page
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'WhatsApp sending is not enabled', r.data)


class NoExternalCallsDuringSendTests(_RouteTestBase):
    """Tests 7, 12 — route does NOT call Gemini, does NOT make real HTTP."""

    def test_no_gemini_call_during_send(self):
        booking = _seed_booking(db)
        # Patch the AI provider dispatcher; assert it's never called.
        with mock.patch('app.services.ai_drafts._call_provider') as gem, \
             mock.patch.object(
                 wa, 'send_text_message',
                 return_value={'success': True, 'message_id': 'wamid.x',
                               'error_class': None},
             ):
            self.client.post(
                f'/bookings/{booking.id}/ai-draft/send-whatsapp',
                data={'message_body': _REAL_BODY},
            )
        gem.assert_not_called()

    def test_no_real_http_call_during_send(self):
        booking = _seed_booking(db)
        # If the wrapper was somehow not mocked, this would catch a real
        # HTTP request to graph.facebook.com via the underlying _requests.
        with mock.patch.object(wa, '_requests') as req, \
             mock.patch.object(
                 wa, 'send_text_message',
                 return_value={'success': True, 'message_id': 'wamid.x',
                               'error_class': None},
             ):
            self.client.post(
                f'/bookings/{booking.id}/ai-draft/send-whatsapp',
                data={'message_body': _REAL_BODY},
            )
        req.post.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# 13 — template smoke
# ─────────────────────────────────────────────────────────────────────────

class TemplateSmokeTests(_RouteTestBase):
    """Test 13 — booking detail page after AI draft generation contains
    the editable textarea, the warning, and the Send button."""

    def test_detail_page_after_ai_draft_contains_send_form(self):
        booking = _seed_booking(db)
        # Drive the AI draft route with a mocked provider call.
        from app.services import ai_drafts
        block = mock.MagicMock()
        block.text = (
            'Dear Hassan, your booking is confirmed. Sheeza Manzil Guesthouse.'
        )
        resp = mock.MagicMock()
        resp.content = [block]
        client = mock.MagicMock()
        client.messages.create.return_value = resp

        os.environ['ANTHROPIC_API_KEY'] = 'sk-test-fake'
        os.environ['AI_DRAFT_PROVIDER'] = 'anthropic'
        ai_drafts._anthropic_client = client
        try:
            r = self.client.post(
                f'/bookings/{booking.id}/ai-draft',
                data={'draft_type': 'booking_confirmed'},
            )
        finally:
            ai_drafts._anthropic_client = None
            os.environ.pop('AI_DRAFT_PROVIDER', None)
            os.environ.pop('ANTHROPIC_API_KEY', None)

        self.assertEqual(r.status_code, 200)
        # Editable textarea (no `readonly` attribute on this id)
        self.assertIn(b'id="ai-draft-text"', r.data)
        self.assertIn(b'name="message_body"', r.data)
        self.assertNotIn(b'readonly', r.data.split(b'name="message_body"')[1][:200])
        # Warning text
        self.assertIn(b'AI-generated draft', r.data)
        self.assertIn(b'review before sending', r.data)
        self.assertIn(b'edited', r.data.lower())
        # Send button
        self.assertIn(b'Send this draft by WhatsApp', r.data)
        # Form posts to the right route
        self.assertIn(b'/ai-draft/send-whatsapp', r.data)
        # 24-hour disclaimer is rendered
        self.assertIn(b'24 hours', r.data)
        # Existing fallback wa.me deeplink still present
        self.assertIn(b'wa.me/', r.data)


# ─────────────────────────────────────────────────────────────────────────
# 15 — AST static check on bookings.py: ai_draft_send_whatsapp does NOT
#      import or call ai_drafts (so a regression that wires AI generation
#      into the send route would fail this test)
# ─────────────────────────────────────────────────────────────────────────

class StaticImportTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.repo = Path(__file__).resolve().parent.parent

    def test_send_whatsapp_handler_does_not_import_ai_drafts(self):
        import ast
        src = (self.repo / 'app' / 'routes' / 'bookings.py').read_text()
        tree = ast.parse(src)
        # Find the function def for ai_draft_send_whatsapp and inspect its body
        func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == 'ai_draft_send_whatsapp':
                func = node
                break
        self.assertIsNotNone(func, 'ai_draft_send_whatsapp not found')
        # Walk the function body for any import referencing ai_drafts
        for child in ast.walk(func):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                module = getattr(child, 'module', '') or ''
                names = [n.name for n in child.names]
                blob = f'{module} :: {names}'.lower()
                self.assertNotIn('ai_drafts', blob,
                                 f'ai_draft_send_whatsapp imports ai_drafts: {blob}')

    def test_send_whatsapp_route_registered(self):
        app = _make_app()
        endpoints = {r.endpoint: r for r in app.url_map.iter_rules()}
        self.assertIn('bookings.ai_draft_send_whatsapp', endpoints)
        rule = endpoints['bookings.ai_draft_send_whatsapp']
        self.assertEqual(rule.rule,
                         '/bookings/<int:booking_id>/ai-draft/send-whatsapp')
        self.assertEqual(rule.methods - {'HEAD', 'OPTIONS'}, {'POST'})


if __name__ == '__main__':
    unittest.main()
