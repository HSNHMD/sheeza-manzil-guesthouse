"""Tests for Inbound WhatsApp Message Handling V1.

Covers (per spec):
  - GET webhook verification success / wrong-token rejection
  - POST webhook signature verification (valid / invalid / missing)
  - POST webhook payload parsing (text / unsupported / multiple)
  - Deduplication on wa_message_id
  - Phone hashing (HMAC w/ SECRET_KEY) + last4 extraction
  - Booking matching (exact-1 / 0 / multi)
  - No auto-reply (no `_send`/`_send_template` call)
  - No Gemini call
  - ActivityLog excludes body / full phone / raw payload
  - Admin inbox auth
  - Booking detail displays linked messages
  - Migration file shape (only creates whatsapp_messages)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

# Clean env BEFORE app import
for _v in ('DATABASE_URL',
           'AI_DRAFT_PROVIDER', 'AI_DRAFT_MODEL',
           'GEMINI_API_KEY', 'ANTHROPIC_API_KEY', 'ANTHROPIC_MODEL',
           'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN',
           'WHATSAPP_PHONE_NUMBER_ID', 'WHATSAPP_PHONE_ID',
           'WHATSAPP_WEBHOOK_VERIFY_TOKEN', 'WHATSAPP_APP_SECRET'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                            # noqa: E402
from app import create_app                                           # noqa: E402
from app.models import (                                             # noqa: E402
    db, User, Room, Guest, Booking, Invoice, ActivityLog,
    WhatsAppMessage,
)
from app.services import whatsapp as wa                              # noqa: E402
from app.services import whatsapp_inbound as inbound                 # noqa: E402


_VERIFY_TOKEN = 'test-verify-token-fake-do-not-use'
_APP_SECRET   = 'test-app-secret-fake-do-not-use'


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    SQLALCHEMY_ENGINE_OPTIONS = {'pool_pre_ping': True, 'pool_recycle': 300}
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _make_app():
    return create_app(_TestConfig)


def _set_webhook_env():
    os.environ['WHATSAPP_WEBHOOK_VERIFY_TOKEN'] = _VERIFY_TOKEN
    os.environ['WHATSAPP_APP_SECRET']           = _APP_SECRET


def _clear_webhook_env():
    os.environ.pop('WHATSAPP_WEBHOOK_VERIFY_TOKEN', None)
    os.environ.pop('WHATSAPP_APP_SECRET', None)


def _seed_guests(*phones):
    """Insert guests with the given phone numbers; returns list of Guest rows."""
    rows = []
    for i, p in enumerate(phones):
        g = Guest(first_name=f'Guest{i}', last_name='Test', phone=p)
        db.session.add(g)
        rows.append(g)
    db.session.commit()
    return rows


def _seed_room_and_booking(guest_id, status='confirmed', booking_ref=None):
    room = Room(number='99', name='Test', room_type='Test',
                floor=0, capacity=2, price_per_night=600.0)
    db.session.add(room)
    db.session.flush()
    b = Booking(
        booking_ref=booking_ref or f'BK{guest_id:06d}',
        room_id=room.id, guest_id=guest_id,
        check_in_date=date.today() + timedelta(days=3),
        check_out_date=date.today() + timedelta(days=5),
        num_guests=1, total_amount=1200.0,
        status=status,
    )
    db.session.add(b)
    db.session.commit()
    return b


def _make_signature(body: bytes, secret: str = _APP_SECRET) -> str:
    return 'sha256=' + hmac.new(
        secret.encode('utf-8'), body, hashlib.sha256
    ).hexdigest()


def _text_payload(*, wa_id='wamid.HBgL01', from_phone='9607001234',
                  body='Hello from guest', profile_name='Hassan',
                  ts='1730000000', msg_type='text'):
    msg = {
        'from': from_phone,
        'id': wa_id,
        'timestamp': ts,
        'type': msg_type,
    }
    if msg_type == 'text':
        msg['text'] = {'body': body}
    return {
        'object': 'whatsapp_business_account',
        'entry': [{
            'id': 'WABA-TEST',
            'changes': [{
                'field': 'messages',
                'value': {
                    'messaging_product': 'whatsapp',
                    'metadata': {
                        'display_phone_number': '+9607375797',
                        'phone_number_id': '123',
                    },
                    'contacts': [{
                        'profile': {'name': profile_name},
                        'wa_id': from_phone,
                    }] if profile_name else [],
                    'messages': [msg],
                },
            }],
        }],
    }


# ─────────────────────────────────────────────────────────────────────────
# Pure-function unit tests (no Flask context)
# ─────────────────────────────────────────────────────────────────────────

class SignatureVerificationTests(unittest.TestCase):

    def test_valid_signature(self):
        body = b'{"hello":"world"}'
        sig = _make_signature(body, 'mysecret')
        self.assertTrue(inbound.verify_signature(body, sig, 'mysecret'))

    def test_wrong_signature(self):
        body = b'{"hello":"world"}'
        sig = _make_signature(body, 'mysecret')
        self.assertFalse(inbound.verify_signature(body, sig, 'WRONG'))

    def test_missing_signature_header(self):
        self.assertFalse(inbound.verify_signature(b'x', None, 'mysecret'))
        self.assertFalse(inbound.verify_signature(b'x', '', 'mysecret'))

    def test_missing_app_secret(self):
        self.assertFalse(inbound.verify_signature(b'x',
                                                  'sha256=anything',
                                                  None))
        self.assertFalse(inbound.verify_signature(b'x',
                                                  'sha256=anything',
                                                  ''))

    def test_signature_without_sha256_prefix(self):
        self.assertFalse(inbound.verify_signature(b'x',
                                                  'rawhash',
                                                  'mysecret'))

    def test_token_match(self):
        self.assertTrue(inbound.verify_token_match('abc', 'abc'))
        self.assertFalse(inbound.verify_token_match('abc', 'xyz'))
        self.assertFalse(inbound.verify_token_match('', 'abc'))
        self.assertFalse(inbound.verify_token_match('abc', ''))
        self.assertFalse(inbound.verify_token_match(None, None))


class PhoneUtilsTests(unittest.TestCase):

    def test_normalize_phone(self):
        self.assertEqual(inbound.normalize_phone('+960 700-1234'), '9607001234')
        self.assertEqual(inbound.normalize_phone('9607001234'), '9607001234')
        self.assertEqual(inbound.normalize_phone(None), '')
        self.assertEqual(inbound.normalize_phone(''), '')

    def test_phone_last4(self):
        self.assertEqual(inbound.phone_last4('+960 700-1234'), '1234')
        self.assertEqual(inbound.phone_last4('9607001234'), '1234')
        self.assertEqual(inbound.phone_last4('123'), '')  # too short
        self.assertEqual(inbound.phone_last4(None), '')

    def test_hash_phone_deterministic(self):
        h1 = inbound.hash_phone('+960 700-1234', secret_key='same-key')
        h2 = inbound.hash_phone('9607001234',     secret_key='same-key')
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 16)
        # Hex chars only
        self.assertTrue(re.match(r'^[0-9a-f]{16}$', h1))

    def test_hash_phone_different_keys(self):
        h1 = inbound.hash_phone('9607001234', secret_key='key-a')
        h2 = inbound.hash_phone('9607001234', secret_key='key-b')
        self.assertNotEqual(h1, h2)

    def test_hash_phone_empty_input(self):
        self.assertEqual(inbound.hash_phone(None), '')
        self.assertEqual(inbound.hash_phone(''),   '')


class PayloadParserTests(unittest.TestCase):

    def test_parses_single_text_message(self):
        out = inbound.parse_webhook_payload(_text_payload())
        self.assertEqual(len(out), 1)
        m = out[0]
        self.assertEqual(m['wa_message_id'], 'wamid.HBgL01')
        self.assertEqual(m['from_phone'],    '9607001234')
        self.assertEqual(m['profile_name'],  'Hassan')
        self.assertEqual(m['message_type'],  'text')
        self.assertEqual(m['body_text'],     'Hello from guest')
        self.assertIsInstance(m['wa_timestamp'], datetime)

    def test_unsupported_type_marked_safely(self):
        out = inbound.parse_webhook_payload(
            _text_payload(msg_type='reaction'))
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]['message_type'].startswith('unsupported_'))
        self.assertIsNone(out[0]['body_text'])

    def test_image_type_keeps_clean_name_no_body(self):
        out = inbound.parse_webhook_payload(
            _text_payload(msg_type='image'))
        self.assertEqual(out[0]['message_type'], 'image')
        self.assertIsNone(out[0]['body_text'])

    def test_multiple_messages(self):
        # Two messages in a single change
        p = _text_payload(wa_id='wamid.001')
        p['entry'][0]['changes'][0]['value']['messages'].append({
            'from': '9601112222',
            'id': 'wamid.002',
            'timestamp': '1730001000',
            'type': 'text',
            'text': {'body': 'second'},
        })
        out = inbound.parse_webhook_payload(p)
        self.assertEqual(len(out), 2)
        ids = {m['wa_message_id'] for m in out}
        self.assertEqual(ids, {'wamid.001', 'wamid.002'})

    def test_malformed_payload_returns_empty(self):
        self.assertEqual(inbound.parse_webhook_payload(None), [])
        self.assertEqual(inbound.parse_webhook_payload('not a dict'), [])
        self.assertEqual(inbound.parse_webhook_payload({}), [])
        self.assertEqual(inbound.parse_webhook_payload(
            {'entry': 'not-a-list'}), [])

    def test_status_change_field_ignored(self):
        # Meta also sends status updates with field='statuses'; we ignore them.
        p = _text_payload()
        p['entry'][0]['changes'][0]['field'] = 'statuses'
        self.assertEqual(inbound.parse_webhook_payload(p), [])


# ─────────────────────────────────────────────────────────────────────────
# Route-level tests
# ─────────────────────────────────────────────────────────────────────────

class _RouteTestBase(unittest.TestCase):

    def setUp(self):
        _set_webhook_env()
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        _clear_webhook_env()


class GetVerificationTests(_RouteTestBase):

    def test_valid_handshake_returns_challenge(self):
        r = self.client.get('/webhooks/whatsapp', query_string={
            'hub.mode':         'subscribe',
            'hub.verify_token': _VERIFY_TOKEN,
            'hub.challenge':    'challenge-value-12345',
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data.decode(), 'challenge-value-12345')
        rows = ActivityLog.query.filter_by(
            action='whatsapp.verify.success').count()
        self.assertEqual(rows, 1)

    def test_wrong_token_returns_403(self):
        r = self.client.get('/webhooks/whatsapp', query_string={
            'hub.mode':         'subscribe',
            'hub.verify_token': 'WRONG-TOKEN',
            'hub.challenge':    'challenge',
        })
        self.assertEqual(r.status_code, 403)
        # Token must NOT appear in response body
        self.assertNotIn(b'WRONG-TOKEN', r.data)
        self.assertNotIn(_VERIFY_TOKEN.encode(), r.data)
        rows = ActivityLog.query.filter_by(
            action='whatsapp.verify.failed').count()
        self.assertEqual(rows, 1)

    def test_missing_token_returns_403(self):
        r = self.client.get('/webhooks/whatsapp', query_string={
            'hub.mode':      'subscribe',
            'hub.challenge': 'challenge',
        })
        self.assertEqual(r.status_code, 403)

    def test_env_unset_returns_403(self):
        _clear_webhook_env()
        r = self.client.get('/webhooks/whatsapp', query_string={
            'hub.mode':         'subscribe',
            'hub.verify_token': 'anything',
            'hub.challenge':    'x',
        })
        self.assertEqual(r.status_code, 403)
        # Restore for tearDown
        _set_webhook_env()


class PostSignatureTests(_RouteTestBase):

    def test_valid_signature_accepted(self):
        body = json.dumps(_text_payload()).encode()
        r = self.client.post(
            '/webhooks/whatsapp',
            data=body,
            content_type='application/json',
            headers={'X-Hub-Signature-256': _make_signature(body)},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WhatsAppMessage.query.count(), 1)

    def test_invalid_signature_rejected(self):
        body = json.dumps(_text_payload()).encode()
        r = self.client.post(
            '/webhooks/whatsapp',
            data=body,
            content_type='application/json',
            headers={'X-Hub-Signature-256': 'sha256=deadbeef'},
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(WhatsAppMessage.query.count(), 0)
        rows = ActivityLog.query.filter_by(
            action='whatsapp.signature_invalid').count()
        self.assertEqual(rows, 1)

    def test_missing_signature_header_rejected(self):
        body = json.dumps(_text_payload()).encode()
        r = self.client.post(
            '/webhooks/whatsapp',
            data=body,
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(WhatsAppMessage.query.count(), 0)

    def test_app_secret_unset_rejects_even_with_correct_signature(self):
        body = json.dumps(_text_payload()).encode()
        sig = _make_signature(body, _APP_SECRET)
        os.environ.pop('WHATSAPP_APP_SECRET', None)
        try:
            r = self.client.post(
                '/webhooks/whatsapp',
                data=body,
                content_type='application/json',
                headers={'X-Hub-Signature-256': sig},
            )
        finally:
            os.environ['WHATSAPP_APP_SECRET'] = _APP_SECRET
        self.assertEqual(r.status_code, 403)
        self.assertEqual(WhatsAppMessage.query.count(), 0)


class PostStorageTests(_RouteTestBase):

    def _post(self, payload):
        body = json.dumps(payload).encode()
        return self.client.post(
            '/webhooks/whatsapp',
            data=body,
            content_type='application/json',
            headers={'X-Hub-Signature-256': _make_signature(body)},
        )

    def test_text_message_stored_with_full_body(self):
        self._post(_text_payload())
        m = WhatsAppMessage.query.first()
        self.assertEqual(m.direction, 'inbound')
        self.assertEqual(m.message_type, 'text')
        self.assertEqual(m.wa_message_id, 'wamid.HBgL01')
        self.assertEqual(m.body_text, 'Hello from guest')
        self.assertEqual(m.from_phone_last4, '1234')
        self.assertEqual(len(m.from_phone_hash), 16)

    def test_full_phone_NOT_stored(self):
        self._post(_text_payload(from_phone='9607001234'))
        m = WhatsAppMessage.query.first()
        # Verify we cannot find the full phone in any text column on the row
        full = '9607001234'
        for v in (m.from_phone_hash or '', m.from_phone_last4 or '',
                  m.body_text or '', m.profile_name or ''):
            self.assertNotIn(full, v)

    def test_unsupported_type_stored_safely(self):
        self._post(_text_payload(msg_type='reaction'))
        self.assertEqual(WhatsAppMessage.query.count(), 1)
        m = WhatsAppMessage.query.first()
        self.assertTrue(m.message_type.startswith('unsupported_'))
        self.assertIsNone(m.body_text)

    def test_duplicate_wa_message_id_does_not_create_second_row(self):
        self._post(_text_payload(wa_id='wamid.dupe'))
        self._post(_text_payload(wa_id='wamid.dupe',
                                 body='different content'))
        # Only first one persisted
        self.assertEqual(WhatsAppMessage.query.count(), 1)
        # Duplicate audit row written
        rows = ActivityLog.query.filter_by(
            action='whatsapp.inbound.duplicate').count()
        self.assertEqual(rows, 1)

    def test_malformed_json_returns_200_and_logs_error(self):
        r = self.client.post(
            '/webhooks/whatsapp',
            data=b'{not json',
            content_type='application/json',
            headers={'X-Hub-Signature-256': _make_signature(b'{not json')},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WhatsAppMessage.query.count(), 0)
        rows = ActivityLog.query.filter_by(
            action='whatsapp.inbound.error').count()
        self.assertEqual(rows, 1)


class MatchingTests(_RouteTestBase):

    def _post(self, payload):
        body = json.dumps(payload).encode()
        return self.client.post(
            '/webhooks/whatsapp',
            data=body,
            content_type='application/json',
            headers={'X-Hub-Signature-256': _make_signature(body)},
        )

    def test_exact_one_guest_match_links_to_guest_and_booking(self):
        guests = _seed_guests('+960 700-1234')
        b = _seed_room_and_booking(guests[0].id, booking_ref='BKMATCH')
        self._post(_text_payload(from_phone='9607001234'))
        m = WhatsAppMessage.query.first()
        self.assertEqual(m.guest_id, guests[0].id)
        self.assertEqual(m.booking_id, b.id)

    def test_unknown_phone_unlinked(self):
        self._post(_text_payload(from_phone='9609999999'))
        m = WhatsAppMessage.query.first()
        self.assertIsNone(m.guest_id)
        self.assertIsNone(m.booking_id)

    def test_multiple_matching_guests_unlinked(self):
        # Two guests with the same phone (edge case, defensive)
        _seed_guests('9607001234', '+960-700-1234')
        self._post(_text_payload(from_phone='9607001234'))
        m = WhatsAppMessage.query.first()
        self.assertIsNone(m.guest_id, 'should NOT link when ambiguous')
        self.assertIsNone(m.booking_id)

    def test_matching_normalizes_format_differences(self):
        guests = _seed_guests('+960 700 1234')  # spaces + plus
        _seed_room_and_booking(guests[0].id)
        self._post(_text_payload(from_phone='9607001234'))  # digits only
        m = WhatsAppMessage.query.first()
        self.assertEqual(m.guest_id, guests[0].id)


class AuditPrivacyTests(_RouteTestBase):

    def _post(self, payload):
        body = json.dumps(payload).encode()
        return self.client.post(
            '/webhooks/whatsapp',
            data=body,
            content_type='application/json',
            headers={'X-Hub-Signature-256': _make_signature(body)},
        )

    def test_audit_row_excludes_body_text(self):
        body_phrase = 'Secret message body for guest verification 12345'
        self._post(_text_payload(body=body_phrase))
        rows = ActivityLog.query.filter_by(
            action='whatsapp.inbound.received').all()
        self.assertEqual(len(rows), 1)
        for col in (rows[0].description or '', rows[0].metadata_json or ''):
            self.assertNotIn(body_phrase, col,
                             'body text leaked into audit row')

    def test_audit_row_excludes_full_phone(self):
        full = '9607009876'
        self._post(_text_payload(from_phone=full,
                                 wa_id='wamid.privacy01'))
        rows = ActivityLog.query.filter_by(
            action='whatsapp.inbound.received').all()
        self.assertEqual(len(rows), 1)
        for col in (rows[0].description or '', rows[0].metadata_json or ''):
            self.assertNotIn(full, col,
                             f'full phone {full} leaked into audit row')
            # Full +-prefixed form
            self.assertNotIn('+' + full, col)

    def test_audit_metadata_keys_are_whitelist_only(self):
        self._post(_text_payload())
        row = ActivityLog.query.filter_by(
            action='whatsapp.inbound.received').first()
        meta = json.loads(row.metadata_json)
        expected = {
            'wa_message_id', 'message_type', 'from_phone_last4',
            'booking_id', 'guest_id', 'matched',
            'body_length', 'profile_name_present', 'direction',
        }
        self.assertEqual(set(meta.keys()), expected,
                         f'unexpected metadata keys: {set(meta.keys()) - expected}')

    def test_audit_row_excludes_secret_tokens(self):
        self._post(_text_payload())
        rows = ActivityLog.query.filter_by(
            action='whatsapp.inbound.received').all()
        for row in rows:
            blob = (row.metadata_json or '') + (row.description or '')
            for forbidden in (_VERIFY_TOKEN, _APP_SECRET,
                              'verify_token', 'app_secret', 'authorization',
                              'bearer'):
                self.assertNotIn(forbidden, blob,
                                 f'forbidden token {forbidden!r} in audit')


class NoSideEffectsTests(_RouteTestBase):
    """No auto-reply, no Gemini call, no booking mutation."""

    def _post(self, payload):
        body = json.dumps(payload).encode()
        return self.client.post(
            '/webhooks/whatsapp',
            data=body,
            content_type='application/json',
            headers={'X-Hub-Signature-256': _make_signature(body)},
        )

    def test_no_whatsapp_send_invoked(self):
        with mock.patch.object(wa, '_send') as send_text, \
             mock.patch.object(wa, '_send_template') as send_tpl, \
             mock.patch.object(wa, 'send_text_message') as send_admin:
            self._post(_text_payload())
        send_text.assert_not_called()
        send_tpl.assert_not_called()
        send_admin.assert_not_called()

    def test_no_gemini_call(self):
        from app.services import ai_drafts
        with mock.patch.object(ai_drafts, '_call_provider') as gem:
            self._post(_text_payload())
        gem.assert_not_called()

    def test_no_booking_mutation(self):
        guests = _seed_guests('+9607001234')
        b = _seed_room_and_booking(guests[0].id)
        before = (b.status, b.total_amount)
        self._post(_text_payload(from_phone='9607001234'))
        b2 = Booking.query.get(b.id)
        after = (b2.status, b2.total_amount)
        self.assertEqual(before, after)


class AdminInboxTests(_RouteTestBase):

    def setUp(self):
        super().setUp()
        admin = User(username='admin1', email='a@x', role='admin')
        admin.set_password('a-very-strong-password-1!')
        staff = User(username='staff1', email='s@x', role='staff')
        staff.set_password('a-very-strong-password-1!')
        db.session.add_all([admin, staff])
        db.session.commit()
        self.admin_id = admin.id
        self.staff_id = staff.id

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True

    def test_anonymous_redirected(self):
        r = self.client.get('/admin/whatsapp/inbox')
        self.assertIn(r.status_code, (301, 302))

    def test_staff_blocked(self):
        self._login(self.staff_id)
        r = self.client.get('/admin/whatsapp/inbox')
        self.assertNotEqual(r.status_code, 200)

    def test_admin_can_load_inbox(self):
        self._login(self.admin_id)
        r = self.client.get('/admin/whatsapp/inbox')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'WhatsApp Inbox', r.data)

    def test_admin_inbox_lists_inbound_messages(self):
        # Insert a message
        msg = WhatsAppMessage(
            direction='inbound',
            wa_message_id='wamid.inbox01',
            from_phone_hash='abc1234567890abc',
            from_phone_last4='1234',
            message_type='text',
            body_text='Visible to admin only',
            body_preview='Visible to admin only',
            status='received',
        )
        db.session.add(msg)
        db.session.commit()
        self._login(self.admin_id)
        r = self.client.get('/admin/whatsapp/inbox')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Visible to admin only', r.data)


class BookingDetailIntegrationTests(_RouteTestBase):

    def setUp(self):
        super().setUp()
        admin = User(username='admin1', email='a@x', role='admin')
        admin.set_password('a-very-strong-password-1!')
        db.session.add(admin)
        db.session.commit()
        self.admin_id = admin.id

    def _login(self):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.admin_id)
            sess['_fresh'] = True

    def test_booking_detail_shows_linked_messages(self):
        guests = _seed_guests('+9607008888')
        b = _seed_room_and_booking(guests[0].id, booking_ref='BKWALINK')

        msg = WhatsAppMessage(
            direction='inbound',
            wa_message_id='wamid.linked01',
            from_phone_hash='abcdef0123456789',
            from_phone_last4='8888',
            message_type='text',
            body_text='Hello, I have a question about my room.',
            body_preview='Hello, I have a question about my room.',
            booking_id=b.id, guest_id=guests[0].id,
            profile_name='Hassan',
            status='received',
        )
        db.session.add(msg)
        # Need an invoice for the detail template's payment-status panel
        inv = Invoice(invoice_number='INVTEST', booking_id=b.id,
                      subtotal=1200.0, total_amount=1200.0,
                      payment_status='paid', amount_paid=1200.0)
        db.session.add(inv)
        db.session.commit()

        self._login()
        r = self.client.get(f'/bookings/{b.id}')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'WhatsApp Messages', r.data)
        self.assertIn(b'I have a question about my room', r.data)


class StaticImportTests(unittest.TestCase):
    """AST static check: webhook route never imports send helpers
    or AI drafts."""

    @classmethod
    def setUpClass(cls):
        cls.repo = Path(__file__).resolve().parent.parent

    def test_webhook_route_does_not_import_send_helpers(self):
        import ast
        path = self.repo / 'app' / 'routes' / 'whatsapp_webhook.py'
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, 'module', '') or ''
                names = [n.name for n in node.names]
                blob = f'{module} :: {names}'
                # Allow the receive-side service module
                if 'whatsapp_inbound' in module:
                    continue
                self.assertNotIn(
                    'services.whatsapp', module,
                    f'webhook imports send-side service: {blob}',
                )
                self.assertNotIn(
                    'ai_drafts', module,
                    f'webhook imports ai_drafts: {blob}',
                )
                for n in names:
                    self.assertNotIn(n, {
                        '_send', '_send_template', 'send_text_message',
                        'send_booking_confirmation',
                        'send_booking_acknowledgment',
                        'send_staff_new_booking_notification',
                        'send_checkin_reminder',
                        'send_checkout_invoice_summary',
                        'generate_draft', 'build_prompt',
                    })


class MigrationFileTests(unittest.TestCase):
    """Confirm migration is correctly chained and only creates
    whatsapp_messages."""

    @classmethod
    def setUpClass(cls):
        cls.repo = Path(__file__).resolve().parent.parent
        cls.path = (cls.repo / 'migrations' / 'versions'
                    / 'c2b9f4d83a51_add_whatsapp_messages_table.py')

    def test_file_exists(self):
        self.assertTrue(self.path.exists(), self.path)

    def test_chain(self):
        text = self.path.read_text()
        self.assertIn("revision = 'c2b9f4d83a51'", text)
        self.assertIn("down_revision = 'f3a7c91b04e2'", text)

    def test_only_creates_whatsapp_messages(self):
        text = self.path.read_text()
        # Allowed
        self.assertIn("op.create_table(", text)
        self.assertIn("'whatsapp_messages'", text)
        # Forbidden
        for forbidden in ('op.alter_column', 'op.add_column',
                          'op.drop_column', 'op.rename_table',
                          'op.rename_column'):
            self.assertNotIn(forbidden, text)
        # All op.create_table / op.create_index / op.drop_index calls
        # must target whatsapp_messages
        for match in re.finditer(
                r"op\.(?:create|drop)_(?:table|index)\([^)]*", text):
            snippet = match.group(0)
            self.assertIn('whatsapp_messages', snippet,
                          f'unexpected target: {snippet}')

    def test_downgrade_drops_only_whatsapp_messages(self):
        text = self.path.read_text()
        m = re.search(r'def downgrade\(\):(.*)', text, re.DOTALL)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("op.drop_table('whatsapp_messages')", body)
        # No drop on existing tables
        for forbidden in ('rooms', 'bookings', 'invoices', 'users',
                          'expenses', 'guests', 'housekeeping_logs',
                          'activity_logs'):
            self.assertNotIn(f"drop_table('{forbidden}')", body)


if __name__ == '__main__':
    unittest.main()
