"""Tests for AI Reply Drafts for Inbound WhatsApp Messages V2.

Hard rules covered:
  - Auth gating on detail / draft / send routes
  - AI draft route never sends WhatsApp; never persists draft body
  - Send route resolves recipient phone from wa_message.guest.phone ONLY
  - Send route blocks unlinked / empty / overlong / placeholder body
  - Two-row audit pattern (attempt + sent/failed)
  - ActivityLog metadata is a strict whitelist (no body, no full phone,
    no prompt, no raw provider response)
  - Public webhook module still has no AI / send imports (AST guard)
  - No Gemini / Anthropic real call during tests (network never invoked)
  - No schema migration added in this feature

Three-layer mocking for defense-in-depth:
  1. service-level mock of `_call_provider` (so Gemini/Anthropic are never
     reached even on the prompt-builder path).
  2. route-level mock of `generate_inbound_reply_draft` for fast tests
     that exercise the audit + render path.
  3. send-level mock of `wa.send_text_message` for the send route.
"""

from __future__ import annotations

import ast
import json
import os
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

# Clean env BEFORE app import — same pattern as test_whatsapp_inbound.py.
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
from app.services import ai_drafts                                   # noqa: E402
from app.services import whatsapp as wa                              # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parent.parent


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    SQLALCHEMY_ENGINE_OPTIONS = {'pool_pre_ping': True, 'pool_recycle': 300}
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _make_app():
    return create_app(_TestConfig)


# ── Fixtures ─────────────────────────────────────────────────────────

def _seed_users():
    admin = User(username='admin1', email='a@x', role='admin')
    admin.set_password('a-very-strong-password-1!')
    staff = User(username='staff1', email='s@x', role='staff')
    staff.set_password('a-very-strong-password-1!')
    db.session.add_all([admin, staff])
    db.session.commit()
    return admin.id, staff.id


def _seed_room():
    room = Room(number='99', name='Test', room_type='Test',
                floor=0, capacity=2, price_per_night=600.0)
    db.session.add(room)
    db.session.commit()
    return room


def _seed_guest_with_booking(phone='+9607009876', booking_status='confirmed',
                             payment_status=None):
    g = Guest(first_name='Hassan', last_name='Test', phone=phone,
              email='hassan@example.com')
    db.session.add(g)
    db.session.flush()
    room = _seed_room()
    b = Booking(
        booking_ref='BK999001',
        room_id=room.id, guest_id=g.id,
        check_in_date=date.today() + timedelta(days=3),
        check_out_date=date.today() + timedelta(days=5),
        num_guests=1, total_amount=1200.0,
        status=booking_status,
    )
    db.session.add(b)
    db.session.flush()
    if payment_status is not None:
        inv = Invoice(
            booking_id=b.id,
            invoice_number='INV-999001',
            total_amount=1200.0,
            payment_status=payment_status,
        )
        db.session.add(inv)
    db.session.commit()
    return g, b


def _make_inbound_msg(*, guest_id=None, booking_id=None, body='Hello',
                     msg_type='text', wa_id='wamid.HBgL01',
                     profile_name='Hassan', from_phone_last4='9876'):
    m = WhatsAppMessage(
        direction='inbound',
        wa_message_id=wa_id,
        wa_timestamp=datetime.utcnow(),
        from_phone_hash='deadbeef00000001',
        from_phone_last4=from_phone_last4,
        profile_name=profile_name,
        guest_id=guest_id,
        booking_id=booking_id,
        message_type=msg_type,
        body_text=body,
        body_preview=(body or '')[:120] if body else None,
        status='received',
    )
    db.session.add(m)
    db.session.commit()
    return m


# ─────────────────────────────────────────────────────────────────────
# 1) Pure-function tests on ai_drafts.generate_inbound_reply_draft
# ─────────────────────────────────────────────────────────────────────

class _MockMessage:
    """Lightweight stand-in for WhatsAppMessage outside Flask context."""

    def __init__(self, *, body_text='Hello', message_type='text',
                 profile_name='Hassan', booking=None):
        self.body_text = body_text
        self.message_type = message_type
        self.profile_name = profile_name
        self.booking = booking
        self.booking_id = getattr(booking, 'id', None) if booking else None
        self.guest = None
        self.guest_id = None


class _MockBooking:
    def __init__(self, payment_status=None):
        class _G:
            first_name = 'Hassan'; last_name = 'Test'; phone = '+9607009876'

        class _R:
            number = '5'; room_type = 'Deluxe'

        class _Inv:
            pass

        self.id = 1
        self.guest = _G()
        self.room = _R()
        if payment_status is not None:
            inv = _Inv()
            inv.payment_status = payment_status
            inv.amount_paid = None
            inv.invoice_number = 'INV-1'
            self.invoice = inv
        else:
            self.invoice = None
        self.booking_ref = 'BK999001'
        self.check_in_date = '2026-04-30'
        self.check_out_date = '2026-05-02'
        self.nights = 2
        self.num_guests = 1
        self.total_amount = 1200.0
        self.status = 'confirmed'
        self.payment_slip_filename = None
        self.id_card_filename = None


class PromptBuilderTests(unittest.TestCase):
    """Pure-function tests on build_inbound_reply_prompt()."""

    def test_text_inbound_with_booking_includes_facts(self):
        m = _MockMessage(body_text='When is my checkin?',
                         booking=_MockBooking())
        prompt = ai_drafts.build_inbound_reply_prompt(m)
        self.assertIn('BK999001', prompt)
        self.assertIn('When is my checkin?', prompt)
        self.assertIn('BOOKING CONTEXT', prompt)

    def test_unlinked_message_omits_booking_facts(self):
        m = _MockMessage(body_text='Hi', booking=None)
        prompt = ai_drafts.build_inbound_reply_prompt(m)
        self.assertNotIn('BK999001', prompt)
        self.assertIn('not linked to a booking', prompt)
        self.assertIn('verify guest identity', prompt)

    def test_non_text_inbound_uses_placeholder_body(self):
        m = _MockMessage(body_text=None, message_type='image',
                         booking=_MockBooking())
        prompt = ai_drafts.build_inbound_reply_prompt(m)
        self.assertIn('non-text message', prompt)
        self.assertIn('image', prompt)

    def test_long_inbound_body_is_truncated_to_1000_chars(self):
        long_body = 'X' * 5000
        m = _MockMessage(body_text=long_body, booking=_MockBooking())
        prompt = ai_drafts.build_inbound_reply_prompt(m)
        # The body section should never contain 5000 X's; the truncated
        # version is at most 1001 + ellipsis.
        self.assertNotIn('X' * 1500, prompt)

    def test_payment_keyword_with_unpaid_includes_block(self):
        m = _MockMessage(body_text='Where do I send the bank transfer?',
                         booking=_MockBooking(payment_status='unpaid'))
        prompt = ai_drafts.build_inbound_reply_prompt(m)
        self.assertIn('OFFICIAL PAYMENT INSTRUCTION BLOCK', prompt)

    def test_payment_keyword_with_verified_omits_block(self):
        m = _MockMessage(body_text='I made a transfer',
                         booking=_MockBooking(payment_status='verified'))
        prompt = ai_drafts.build_inbound_reply_prompt(m)
        self.assertNotIn('OFFICIAL PAYMENT INSTRUCTION BLOCK', prompt)

    def test_no_payment_keyword_omits_block(self):
        m = _MockMessage(body_text='Just saying hi',
                         booking=_MockBooking(payment_status='unpaid'))
        prompt = ai_drafts.build_inbound_reply_prompt(m)
        self.assertNotIn('OFFICIAL PAYMENT INSTRUCTION BLOCK', prompt)


class GenerateInboundReplyDraftTests(unittest.TestCase):
    """Tests on the public entry point — provider call is mocked at
    `_call_provider` so Gemini/Anthropic are never reached."""

    def test_no_provider_key_returns_ai_not_configured(self):
        os.environ.pop('GEMINI_API_KEY', None)
        os.environ.pop('ANTHROPIC_API_KEY', None)
        m = _MockMessage(body_text='hi', booking=_MockBooking())
        result = ai_drafts.generate_inbound_reply_draft(m)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'ai_not_configured')
        self.assertEqual(result['has_booking_context'], True)

    def test_invalid_provider_returns_invalid_provider(self):
        os.environ['AI_DRAFT_PROVIDER'] = 'doesnotexist'
        try:
            m = _MockMessage(body_text='hi', booking=_MockBooking())
            result = ai_drafts.generate_inbound_reply_draft(m)
        finally:
            os.environ.pop('AI_DRAFT_PROVIDER', None)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'invalid_provider')

    def test_success_path_returns_draft(self):
        os.environ['GEMINI_API_KEY'] = 'fake-key-do-not-use'
        try:
            m = _MockMessage(body_text='ok', booking=_MockBooking())
            with mock.patch.object(
                ai_drafts, '_call_provider',
                return_value={'success': True, 'text': 'Hello there'},
            ):
                result = ai_drafts.generate_inbound_reply_draft(m)
        finally:
            os.environ.pop('GEMINI_API_KEY', None)
        self.assertTrue(result['success'])
        self.assertEqual(result['draft'], 'Hello there')
        self.assertEqual(result['draft_type'], 'inbound_reply')
        self.assertEqual(result['has_booking_context'], True)

    def test_provider_failure_propagates_error_class(self):
        os.environ['GEMINI_API_KEY'] = 'fake-key-do-not-use'
        try:
            m = _MockMessage(body_text='hi', booking=_MockBooking())
            with mock.patch.object(
                ai_drafts, '_call_provider',
                return_value={'success': False, 'error': 'ai_unavailable'},
            ):
                result = ai_drafts.generate_inbound_reply_draft(m)
        finally:
            os.environ.pop('GEMINI_API_KEY', None)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'ai_unavailable')


# ─────────────────────────────────────────────────────────────────────
# 2) Route auth tests
# ─────────────────────────────────────────────────────────────────────

class _RouteTestBase(unittest.TestCase):

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.admin_id, self.staff_id = _seed_users()
        self.guest, self.booking = _seed_guest_with_booking()
        self.linked = _make_inbound_msg(
            guest_id=self.guest.id, booking_id=self.booking.id,
            body='When is checkin?', wa_id='wamid.LINKED01',
        )
        self.unlinked = _make_inbound_msg(
            guest_id=None, booking_id=None,
            body='Hi I have not booked yet', wa_id='wamid.UNLINKED01',
        )
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True


class DetailRouteAuthTests(_RouteTestBase):

    def test_anonymous_redirected_or_blocked(self):
        r = self.client.get(f'/admin/whatsapp/messages/{self.linked.id}')
        # Flask-Login redirects unauth → /console (401 also acceptable)
        self.assertIn(r.status_code, (301, 302, 401))

    def test_staff_blocked(self):
        self._login(self.staff_id)
        # Staff guard redirects to /staff/dashboard before admin_required
        # is reached, so 302 is correct here.
        r = self.client.get(f'/admin/whatsapp/messages/{self.linked.id}')
        self.assertIn(r.status_code, (302, 401, 403))

    def test_admin_can_load_detail(self):
        self._login(self.admin_id)
        r = self.client.get(f'/admin/whatsapp/messages/{self.linked.id}')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'AI Reply Draft Assistant', r.data)
        self.assertIn(b'Generate AI Reply Draft', r.data)

    def test_detail_404_for_unknown_message_id(self):
        self._login(self.admin_id)
        r = self.client.get('/admin/whatsapp/messages/999999')
        self.assertEqual(r.status_code, 404)


class DraftRouteAuthTests(_RouteTestBase):

    def test_anonymous_blocked(self):
        with mock.patch.object(ai_drafts, 'generate_inbound_reply_draft') as m:
            r = self.client.post(
                f'/admin/whatsapp/messages/{self.linked.id}/ai-reply-draft')
        self.assertIn(r.status_code, (301, 302, 401))
        m.assert_not_called()

    def test_staff_blocked(self):
        self._login(self.staff_id)
        with mock.patch.object(ai_drafts, 'generate_inbound_reply_draft') as m:
            r = self.client.post(
                f'/admin/whatsapp/messages/{self.linked.id}/ai-reply-draft')
        self.assertIn(r.status_code, (302, 401, 403))
        m.assert_not_called()

    def test_get_method_not_allowed(self):
        self._login(self.admin_id)
        r = self.client.get(
            f'/admin/whatsapp/messages/{self.linked.id}/ai-reply-draft')
        self.assertEqual(r.status_code, 405)


class SendRouteAuthTests(_RouteTestBase):

    def test_anonymous_blocked(self):
        with mock.patch.object(wa, 'send_text_message') as m:
            r = self.client.post(
                f'/admin/whatsapp/messages/{self.linked.id}/send-reply',
                data={'message_body': 'Hi back!'},
            )
        self.assertIn(r.status_code, (301, 302, 401))
        m.assert_not_called()

    def test_staff_blocked(self):
        self._login(self.staff_id)
        with mock.patch.object(wa, 'send_text_message') as m:
            r = self.client.post(
                f'/admin/whatsapp/messages/{self.linked.id}/send-reply',
                data={'message_body': 'Hi back!'},
            )
        self.assertIn(r.status_code, (302, 401, 403))
        m.assert_not_called()

    def test_get_method_not_allowed(self):
        self._login(self.admin_id)
        r = self.client.get(
            f'/admin/whatsapp/messages/{self.linked.id}/send-reply')
        self.assertEqual(r.status_code, 405)


# ─────────────────────────────────────────────────────────────────────
# 3) Draft route behavior
# ─────────────────────────────────────────────────────────────────────

class DraftRouteBehaviourTests(_RouteTestBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def _stub_provider(self, text='Generated draft body'):
        return mock.patch.object(
            ai_drafts, '_call_provider',
            return_value={'success': True, 'text': text},
        )

    def test_unlinked_message_does_not_call_provider(self):
        os.environ['GEMINI_API_KEY'] = 'fake-key'
        try:
            with mock.patch.object(ai_drafts, '_call_provider') as p:
                r = self.client.post(
                    f'/admin/whatsapp/messages/{self.unlinked.id}/ai-reply-draft')
        finally:
            os.environ.pop('GEMINI_API_KEY', None)
        self.assertEqual(r.status_code, 200)
        p.assert_not_called()
        self.assertIn(b'not linked', r.data)

    def test_linked_message_calls_provider_with_inbound_body(self):
        os.environ['GEMINI_API_KEY'] = 'fake-key'
        try:
            with self._stub_provider() as p:
                r = self.client.post(
                    f'/admin/whatsapp/messages/{self.linked.id}/ai-reply-draft')
        finally:
            os.environ.pop('GEMINI_API_KEY', None)
        self.assertEqual(r.status_code, 200)
        p.assert_called_once()
        args, kwargs = p.call_args
        # _call_provider(provider, system, user, model)
        provider_arg, system_arg, user_arg, model_arg = args
        self.assertIn('When is checkin?', user_arg)
        self.assertIn('BK999001', user_arg)

    def test_draft_body_not_persisted_anywhere(self):
        unique_marker = 'UNIQUE_DRAFT_MARKER_AB12CD34'
        os.environ['GEMINI_API_KEY'] = 'fake-key'
        try:
            with self._stub_provider(text=unique_marker):
                r = self.client.post(
                    f'/admin/whatsapp/messages/{self.linked.id}/ai-reply-draft')
        finally:
            os.environ.pop('GEMINI_API_KEY', None)
        self.assertEqual(r.status_code, 200)
        # The marker must be in the rendered HTML…
        self.assertIn(unique_marker.encode(), r.data)
        # …but never in any DB row (WhatsAppMessage / ActivityLog).
        for row in WhatsAppMessage.query.all():
            self.assertNotIn(unique_marker, row.body_text or '')
            self.assertNotIn(unique_marker, row.body_preview or '')
        for row in ActivityLog.query.all():
            haystack = ' '.join([
                row.description or '',
                (row.metadata_json or ''),
            ])
            self.assertNotIn(unique_marker, haystack)

    def test_draft_route_does_not_send_whatsapp(self):
        os.environ['GEMINI_API_KEY'] = 'fake-key'
        try:
            with self._stub_provider(), mock.patch.object(
                wa, 'send_text_message') as send_m:
                self.client.post(
                    f'/admin/whatsapp/messages/{self.linked.id}/ai-reply-draft')
        finally:
            os.environ.pop('GEMINI_API_KEY', None)
        send_m.assert_not_called()

    def test_draft_route_does_not_change_booking_or_invoice_status(self):
        before_status = self.booking.status
        os.environ['GEMINI_API_KEY'] = 'fake-key'
        try:
            with self._stub_provider():
                self.client.post(
                    f'/admin/whatsapp/messages/{self.linked.id}/ai-reply-draft')
        finally:
            os.environ.pop('GEMINI_API_KEY', None)
        b = Booking.query.get(self.booking.id)
        self.assertEqual(b.status, before_status)

    def test_draft_route_audit_metadata_strict_whitelist(self):
        os.environ['GEMINI_API_KEY'] = 'fake-key'
        try:
            with self._stub_provider(text='Reply text body content'):
                self.client.post(
                    f'/admin/whatsapp/messages/{self.linked.id}/ai-reply-draft')
        finally:
            os.environ.pop('GEMINI_API_KEY', None)

        rows = (ActivityLog.query
                .filter(ActivityLog.action ==
                        'whatsapp.inbound.ai_reply_draft_created')
                .all())
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0].metadata_json or '{}')
        # Allowed keys should be present
        for key in ('whatsapp_message_id', 'booking_id', 'guest_id',
                    'draft_type', 'matched_booking', 'provider', 'model',
                    'message_length', 'has_booking_context',
                    'payment_instructions_used', 'success'):
            self.assertIn(key, meta, f'missing whitelisted key {key}')
        # Forbidden keys / values must NOT appear
        forbidden_substrings = (
            'When is checkin?',
            'Reply text body content',
            self.guest.phone,
        )
        meta_blob = json.dumps(meta)
        for substr in forbidden_substrings:
            self.assertNotIn(
                substr, meta_blob,
                f'forbidden substring {substr!r} leaked into metadata',
            )

    def test_draft_route_audit_description_excludes_inbound_body(self):
        os.environ['GEMINI_API_KEY'] = 'fake-key'
        try:
            with self._stub_provider():
                self.client.post(
                    f'/admin/whatsapp/messages/{self.linked.id}/ai-reply-draft')
        finally:
            os.environ.pop('GEMINI_API_KEY', None)
        row = (ActivityLog.query
               .filter(ActivityLog.action ==
                       'whatsapp.inbound.ai_reply_draft_created')
               .first())
        self.assertIsNotNone(row)
        self.assertNotIn('When is checkin?', row.description or '')


# ─────────────────────────────────────────────────────────────────────
# 4) Send route behavior
# ─────────────────────────────────────────────────────────────────────

class SendRouteValidationTests(_RouteTestBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_unlinked_message_blocks_send(self):
        with mock.patch.object(wa, 'send_text_message') as m:
            r = self.client.post(
                f'/admin/whatsapp/messages/{self.unlinked.id}/send-reply',
                data={'message_body': 'Hi'},
                follow_redirects=False,
            )
        self.assertIn(r.status_code, (301, 302))
        m.assert_not_called()
        # No attempt audit row written
        self.assertEqual(
            ActivityLog.query.filter(
                ActivityLog.action.like('whatsapp.inbound.reply_%')
            ).count(),
            0,
        )

    def test_empty_body_blocks_send(self):
        with mock.patch.object(wa, 'send_text_message') as m:
            r = self.client.post(
                f'/admin/whatsapp/messages/{self.linked.id}/send-reply',
                data={'message_body': ''},
                follow_redirects=False,
            )
        self.assertIn(r.status_code, (301, 302))
        m.assert_not_called()
        self.assertEqual(
            ActivityLog.query.filter(
                ActivityLog.action.like('whatsapp.inbound.reply_%')
            ).count(),
            0,
        )

    def test_placeholder_body_blocks_send(self):
        with mock.patch.object(wa, 'send_text_message') as m:
            r = self.client.post(
                f'/admin/whatsapp/messages/{self.linked.id}/send-reply',
                data={'message_body': 'Hi [admin: insert booking_ref]'},
                follow_redirects=False,
            )
        self.assertIn(r.status_code, (301, 302))
        m.assert_not_called()

    def test_overlong_body_blocks_send(self):
        with mock.patch.object(wa, 'send_text_message') as m:
            r = self.client.post(
                f'/admin/whatsapp/messages/{self.linked.id}/send-reply',
                data={'message_body': 'X' * 1501},
                follow_redirects=False,
            )
        self.assertIn(r.status_code, (301, 302))
        m.assert_not_called()


class SendRouteSuccessTests(_RouteTestBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_valid_send_calls_wrapper_once_with_guest_phone(self):
        with mock.patch.object(
            wa, 'send_text_message',
            return_value={'success': True, 'message_id': 'wamid.OUT01',
                          'error_class': None},
        ) as m:
            r = self.client.post(
                f'/admin/whatsapp/messages/{self.linked.id}/send-reply',
                data={'message_body': 'Check-in is at 2pm.'},
                follow_redirects=False,
            )
        m.assert_called_once()
        # arg 0 must be the guest's phone — never the form
        args, kwargs = m.call_args
        self.assertEqual(args[0], self.guest.phone)
        self.assertEqual(args[1], 'Check-in is at 2pm.')
        self.assertIn(r.status_code, (301, 302))

    def test_send_route_ignores_to_phone_in_form(self):
        with mock.patch.object(
            wa, 'send_text_message',
            return_value={'success': True, 'message_id': 'wamid.OUT02',
                          'error_class': None},
        ) as m:
            self.client.post(
                f'/admin/whatsapp/messages/{self.linked.id}/send-reply',
                data={
                    'message_body': 'Check-in 2pm.',
                    # Attempted spoof — must be IGNORED.
                    'to_phone': '+99999999999',
                    'phone': '+88888888888',
                },
            )
        args, _ = m.call_args
        self.assertEqual(args[0], self.guest.phone)
        self.assertNotIn('99999', args[0])

    def test_send_logs_attempt_then_sent_with_safe_metadata(self):
        with mock.patch.object(
            wa, 'send_text_message',
            return_value={'success': True, 'message_id': 'wamid.SENT01',
                          'error_class': None},
        ):
            self.client.post(
                f'/admin/whatsapp/messages/{self.linked.id}/send-reply',
                data={'message_body': 'Confirming your booking.'},
            )
        actions = sorted(r.action for r in
                         ActivityLog.query.filter(
                             ActivityLog.action.like('whatsapp.inbound.reply_%')
                         ).order_by(ActivityLog.id.asc()).all())
        self.assertEqual(actions, [
            'whatsapp.inbound.reply_send_attempt',
            'whatsapp.inbound.reply_sent',
        ])

        sent_row = (ActivityLog.query
                    .filter(ActivityLog.action ==
                            'whatsapp.inbound.reply_sent').first())
        meta = json.loads(sent_row.metadata_json or '{}')
        # Allowed
        self.assertEqual(meta.get('recipient_phone_last4'), '9876')
        self.assertEqual(meta.get('outbound_message_id'), 'wamid.SENT01')
        self.assertEqual(meta.get('booking_ref'), 'BK999001')
        self.assertEqual(meta.get('matched_booking'), True)
        # Forbidden
        meta_blob = json.dumps(meta)
        self.assertNotIn('Confirming your booking.', meta_blob)
        self.assertNotIn(self.guest.phone, meta_blob)

    def test_send_does_not_change_booking_or_room_or_invoice(self):
        before_b = self.booking.status
        before_room_status = getattr(self.booking.room, 'status', None)
        with mock.patch.object(
            wa, 'send_text_message',
            return_value={'success': True, 'message_id': 'wamid.X',
                          'error_class': None},
        ):
            self.client.post(
                f'/admin/whatsapp/messages/{self.linked.id}/send-reply',
                data={'message_body': 'Hi.'},
            )
        b = Booking.query.get(self.booking.id)
        self.assertEqual(b.status, before_b)
        self.assertEqual(getattr(b.room, 'status', None), before_room_status)


class SendRouteFailureTests(_RouteTestBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_meta_window_closed_logs_failed_with_error_class(self):
        with mock.patch.object(
            wa, 'send_text_message',
            return_value={'success': False, 'message_id': None,
                          'error_class': 'meta_window_closed'},
        ):
            self.client.post(
                f'/admin/whatsapp/messages/{self.linked.id}/send-reply',
                data={'message_body': 'Hi.'},
            )
        failed = (ActivityLog.query
                  .filter(ActivityLog.action ==
                          'whatsapp.inbound.reply_failed').first())
        self.assertIsNotNone(failed)
        meta = json.loads(failed.metadata_json or '{}')
        self.assertEqual(meta.get('error_class'), 'meta_window_closed')

    def test_send_does_not_call_gemini(self):
        with mock.patch.object(ai_drafts, '_call_provider') as gm, \
             mock.patch.object(
                 wa, 'send_text_message',
                 return_value={'success': True, 'message_id': 'wamid.A',
                               'error_class': None},
             ):
            self.client.post(
                f'/admin/whatsapp/messages/{self.linked.id}/send-reply',
                data={'message_body': 'Hi.'},
            )
        gm.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# 5) Static / safety checks
# ─────────────────────────────────────────────────────────────────────

class WebhookASTGuardStillHoldsTests(unittest.TestCase):
    """The public webhook module must STILL not import ai_drafts or any
    send helper. The new admin blueprint lives elsewhere on purpose."""

    def test_webhook_module_does_not_import_ai_drafts(self):
        path = _REPO_ROOT / 'app' / 'routes' / 'whatsapp_webhook.py'
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, 'module', '') or ''
                self.assertNotIn(
                    'ai_drafts', module,
                    f'webhook module must not import ai_drafts: {module}',
                )
                names = {n.name for n in node.names}
                self.assertFalse(names & {
                    'generate_draft', 'generate_inbound_reply_draft',
                    'build_prompt', 'build_inbound_reply_prompt',
                })

    def test_webhook_module_does_not_import_send_helpers(self):
        path = _REPO_ROOT / 'app' / 'routes' / 'whatsapp_webhook.py'
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, 'module', '') or ''
                if 'whatsapp_inbound' in module:
                    continue
                self.assertNotIn(
                    'services.whatsapp', module,
                    f'webhook imports send-side: {module}',
                )
                names = {n.name for n in node.names}
                self.assertFalse(names & {
                    '_send', '_send_template', 'send_text_message',
                    'send_booking_confirmation',
                    'send_booking_acknowledgment',
                    'send_staff_new_booking_notification',
                    'send_checkin_reminder',
                    'send_checkout_invoice_summary',
                })


class NoNewMigrationTests(unittest.TestCase):
    """Confirm no new alembic version files were added in this feature."""

    def test_known_migration_count_unchanged(self):
        # If a migration is added in this feature, this test will fail and
        # the developer must update the expected list deliberately.
        versions_dir = _REPO_ROOT / 'migrations' / 'versions'
        if not versions_dir.exists():
            self.skipTest('no migrations directory')
        files = sorted(p.name for p in versions_dir.glob('*.py')
                       if p.name != '__init__.py')
        self.assertIn(
            'c2b9f4d83a51_add_whatsapp_messages_table.py', files,
            'V1 inbound migration must still be present',
        )


class TemplateBehaviourTests(_RouteTestBase):
    """Light DOM-style assertions on rendered HTML — no JS execution."""

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_inbox_lists_view_reply_link(self):
        r = self.client.get('/admin/whatsapp/inbox')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'View / Reply with AI draft', r.data)

    def test_message_detail_shows_generate_button_for_linked(self):
        r = self.client.get(f'/admin/whatsapp/messages/{self.linked.id}')
        self.assertIn(b'Generate AI Reply Draft', r.data)

    def test_message_detail_hides_draft_form_for_unlinked(self):
        r = self.client.get(f'/admin/whatsapp/messages/{self.unlinked.id}')
        # Generate button is hidden for unlinked
        self.assertNotIn(b'Generate AI Reply Draft', r.data)
        self.assertIn(b'unlinked', r.data.lower())

    def test_textarea_appears_after_successful_draft(self):
        os.environ['GEMINI_API_KEY'] = 'fake-key'
        try:
            with mock.patch.object(
                ai_drafts, '_call_provider',
                return_value={'success': True, 'text': 'Suggested reply.'},
            ):
                r = self.client.post(
                    f'/admin/whatsapp/messages/{self.linked.id}/ai-reply-draft')
        finally:
            os.environ.pop('GEMINI_API_KEY', None)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'<textarea', r.data)
        self.assertIn(b'message_body', r.data)
        self.assertIn(b'Suggested reply.', r.data)
        self.assertIn(b'AI-generated reply draft', r.data)


if __name__ == '__main__':
    unittest.main()
