"""Inbound WhatsApp webhook + admin inbox.

Three routes in one blueprint:

    GET  /webhooks/whatsapp        — Meta verification handshake (public)
    POST /webhooks/whatsapp        — Meta inbound delivery     (public, HMAC)
    GET  /admin/whatsapp/inbox     — Admin inbox view          (admin-only)

Hard rules (all enforced by the route + tests):
- The two public endpoints are gated by cryptographic verification only
  (verify-token for GET, HMAC-SHA256 of the raw body for POST). They
  never accept session-cookie auth.
- The route NEVER imports or calls any send helper from
  app.services.whatsapp. AST static test enforces this — see
  tests/test_whatsapp_inbound.py.
- The route NEVER imports app.services.ai_drafts. There is no AI draft
  generation triggered by inbound traffic in V1.
- The full message body is stored on WhatsAppMessage (admin needs it)
  but is NEVER passed to log_activity. Audit metadata is a strict
  whitelist: wa_message_id, message_type, from_phone_last4, booking_id,
  guest_id, matched, body_length.
- The full sender phone is NEVER stored. We persist only an HMAC hash
  (keyed by SECRET_KEY) and the last 4 digits.
- The route ALWAYS returns 200 OK on POST after signature verification
  succeeds, even if internal parsing/persisting fails. This prevents
  Meta from triggering retry-storms on transient errors. Failures are
  logged to ActivityLog as 'whatsapp.inbound.error'.
"""

from __future__ import annotations

import json
import os

from flask import Blueprint, request, render_template, abort

from flask_login import login_required

from ..models import db, WhatsAppMessage
from ..decorators import admin_required
from ..services.audit import log_activity
from ..services.whatsapp_inbound import (
    verify_signature,
    verify_token_match,
    parse_webhook_payload,
    normalize_phone,
    phone_last4,
    hash_phone,
    match_inbound_sender,
)


whatsapp_bp = Blueprint('whatsapp', __name__)


# ── Helpers ─────────────────────────────────────────────────────────

def _get_verify_token() -> str:
    return os.environ.get('WHATSAPP_WEBHOOK_VERIFY_TOKEN', '') or ''


def _get_app_secret() -> str:
    return os.environ.get('WHATSAPP_APP_SECRET', '') or ''


def _truncate(s, n):
    if s is None:
        return None
    s = str(s)
    return s if len(s) <= n else s[:n]


# ── GET /webhooks/whatsapp — Meta verification handshake ────────────

@whatsapp_bp.route('/webhooks/whatsapp', methods=['GET'])
def webhook_verify():
    """Meta's hub-mode verification handshake.

    Meta sends:
        ?hub.mode=subscribe
        &hub.verify_token=<the token configured in Meta dashboard>
        &hub.challenge=<echo string>

    We respond with 200 + the challenge text iff:
        - configured WHATSAPP_WEBHOOK_VERIFY_TOKEN env var is non-empty
        - hub.mode == 'subscribe'
        - hub.verify_token matches our configured token (constant-time)

    Any failure → 403 with no body. Token value is NEVER logged.
    """
    mode      = request.args.get('hub.mode', '')
    token     = request.args.get('hub.verify_token', '')
    challenge = request.args.get('hub.challenge', '')

    configured = _get_verify_token()

    if not configured:
        # Fail-safe: refuse all verification attempts when the env var
        # is unset. Don't write an audit row — too noisy if Meta retries.
        return ('forbidden', 403)

    if mode != 'subscribe' or not verify_token_match(token, configured):
        # Constant-time mismatch. Log only the symbolic outcome.
        try:
            log_activity(
                'whatsapp.verify.failed',
                actor_type='system',
                description='WhatsApp webhook verification rejected.',
                metadata={
                    'mode_received': bool(mode),
                    'token_received': bool(token),
                },
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
        return ('forbidden', 403)

    # Verified.
    try:
        log_activity(
            'whatsapp.verify.success',
            actor_type='system',
            description='WhatsApp webhook verification handshake completed.',
            metadata={'challenge_length': len(challenge)},
        )
        db.session.commit()
    except Exception:
        db.session.rollback()

    return (challenge, 200, {'Content-Type': 'text/plain; charset=utf-8'})


# ── POST /webhooks/whatsapp — Meta inbound delivery ─────────────────

@whatsapp_bp.route('/webhooks/whatsapp', methods=['POST'])
def webhook_receive():
    """Receive an inbound WhatsApp event from Meta.

    Pipeline:
      1. Read raw request body (must happen before request.json reads
         the stream).
      2. Verify X-Hub-Signature-256 against WHATSAPP_APP_SECRET.
         Mismatch → 403 + audit row 'whatsapp.signature_invalid'.
      3. Parse JSON. Malformed → 200 + 'whatsapp.inbound.error' row.
      4. parse_webhook_payload() → list of message dicts.
      5. For each parsed message:
         - Skip duplicate wa_message_id (audit row 'whatsapp.inbound.duplicate').
         - Hash + last4 the sender phone.
         - Try match_inbound_sender() — link booking_id + guest_id if
           exactly one guest matches.
         - INSERT WhatsAppMessage row.
         - Log 'whatsapp.inbound.received' with metadata-only audit.
      6. Return 200 OK. ALWAYS. (Prevents Meta retry-storms.)
    """
    raw_body = request.get_data(cache=True) or b''
    sig_header = request.headers.get('X-Hub-Signature-256', '')
    app_secret = _get_app_secret()

    # ── (2) Signature verification — fail-safe on missing app_secret ──
    if not verify_signature(raw_body, sig_header, app_secret):
        try:
            log_activity(
                'whatsapp.signature_invalid',
                actor_type='system',
                description='Inbound WhatsApp POST rejected: signature mismatch.',
                metadata={
                    'has_signature_header': bool(sig_header),
                    'app_secret_configured': bool(app_secret),
                    'body_bytes': len(raw_body),
                },
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
        return ('forbidden', 403)

    # ── (3) JSON parse ──
    try:
        payload = json.loads(raw_body.decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError) as exc:
        try:
            log_activity(
                'whatsapp.inbound.error',
                actor_type='system',
                description='Inbound webhook JSON decode failed.',
                metadata={
                    'error_class': type(exc).__name__,
                    'body_bytes': len(raw_body),
                },
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
        return ('', 200)

    # ── (4) Parse + (5) persist ──
    try:
        parsed_messages = parse_webhook_payload(payload)
    except Exception as exc:
        try:
            log_activity(
                'whatsapp.inbound.error',
                actor_type='system',
                description='Inbound webhook parse failed.',
                metadata={'error_class': type(exc).__name__},
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
        return ('', 200)

    for parsed in parsed_messages:
        try:
            _persist_one_message(parsed)
        except Exception as exc:
            db.session.rollback()
            try:
                log_activity(
                    'whatsapp.inbound.error',
                    actor_type='system',
                    description='Inbound webhook persist failed.',
                    metadata={
                        'error_class': type(exc).__name__,
                        'wa_message_id_present': bool(
                            parsed.get('wa_message_id')),
                    },
                )
                db.session.commit()
            except Exception:
                db.session.rollback()

    return ('', 200)


def _persist_one_message(parsed: dict):
    """Persist a single parsed message dict.

    Idempotent on wa_message_id: if a row already exists, log a duplicate
    audit event and return without raising.
    """
    wa_id = parsed.get('wa_message_id') or None

    # Idempotency check
    if wa_id:
        existing = (WhatsAppMessage.query
                    .filter_by(wa_message_id=wa_id).first())
        if existing is not None:
            log_activity(
                'whatsapp.inbound.duplicate',
                actor_type='system',
                description='Duplicate WhatsApp inbound (already stored).',
                metadata={
                    'wa_message_id': wa_id,
                    'message_type': existing.message_type,
                },
            )
            db.session.commit()
            return

    from_phone = parsed.get('from_phone') or ''
    body_text = parsed.get('body_text')
    profile_name = parsed.get('profile_name')

    # Match to guest/booking (best-effort)
    guest_id, booking_id = match_inbound_sender(from_phone)

    # Build the row
    msg = WhatsAppMessage(
        direction='inbound',
        wa_message_id=wa_id,
        wa_timestamp=parsed.get('wa_timestamp'),
        from_phone_hash=hash_phone(from_phone),
        from_phone_last4=phone_last4(from_phone),
        to_phone_last4=None,  # to_phone is our number; not relevant inbound
        profile_name=_truncate(profile_name, 100),
        booking_id=booking_id,
        guest_id=guest_id,
        message_type=parsed.get('message_type', 'text'),
        body_text=body_text,
        body_preview=_truncate(body_text, 120),
        status='received',
        metadata_json=None,
    )
    db.session.add(msg)

    # ── Audit row: metadata WHITELIST ONLY, never the body ──
    booking_ref = None
    if booking_id is not None:
        from ..models import Booking
        b = Booking.query.get(booking_id)
        if b:
            booking_ref = b.booking_ref

    log_activity(
        'whatsapp.inbound.received',
        actor_type='system',
        booking_id=booking_id,
        description=(
            'Inbound WhatsApp message received'
            + (f' (linked to {booking_ref}).' if booking_ref else ' (unlinked).')
        ),
        metadata={
            'wa_message_id':     wa_id,
            'message_type':      msg.message_type,
            'from_phone_last4':  msg.from_phone_last4,
            'booking_id':        booking_id,
            'guest_id':          guest_id,
            'matched':           bool(guest_id),
            'body_length':       len(body_text) if body_text else 0,
            'profile_name_present': bool(profile_name),
            'direction':         'inbound',
        },
    )
    db.session.commit()


# ── GET /admin/whatsapp/inbox — admin inbox ─────────────────────────

@whatsapp_bp.route('/admin/whatsapp/inbox', methods=['GET'])
@login_required
@admin_required
def inbox():
    """Admin-only inbox view of recent inbound WhatsApp messages.

    Query-string filters (all optional):
        ?linked=1      — only messages with a booking link
        ?unlinked=1    — only messages without a booking link
        ?message_type= — exact match on stored message_type
    """
    show_linked   = request.args.get('linked')   == '1'
    show_unlinked = request.args.get('unlinked') == '1'
    msg_type      = (request.args.get('message_type') or '').strip()

    query = (WhatsAppMessage.query
             .filter(WhatsAppMessage.direction == 'inbound'))
    if show_linked and not show_unlinked:
        query = query.filter(WhatsAppMessage.booking_id.isnot(None))
    elif show_unlinked and not show_linked:
        query = query.filter(WhatsAppMessage.booking_id.is_(None))
    if msg_type:
        query = query.filter(WhatsAppMessage.message_type == msg_type)

    messages = (query
                .order_by(WhatsAppMessage.created_at.desc())
                .limit(100)
                .all())

    return render_template(
        'whatsapp/inbox.html',
        messages=messages,
        show_linked=show_linked,
        show_unlinked=show_unlinked,
        msg_type=msg_type,
    )
