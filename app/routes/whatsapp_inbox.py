"""Admin-only WhatsApp inbox: per-message detail + AI reply drafts + send.

This module is the AI-AWARE half of the inbound WhatsApp UX. It is split
from ``app/routes/whatsapp_webhook.py`` (the public, receive-only webhook)
so the existing AST static-import guarantee on the webhook is preserved:
``whatsapp_webhook.py`` continues to import NEITHER ``ai_drafts`` NOR any
send helper, while THIS module owns all admin-side AI + send work.

Routes:
    GET  /admin/whatsapp/messages/<int:message_id>
        — Per-message detail page. Shows inbound body, linked booking
          (if any), and the AI draft assistant + send form.

    POST /admin/whatsapp/messages/<int:message_id>/ai-reply-draft
        — Generate an AI reply draft. Renders message_detail.html with
          ``ai_draft`` populated. NEVER persists the draft body.

    POST /admin/whatsapp/messages/<int:message_id>/send-reply
        — Admin-approved manual send of an EDITED draft. Recipient phone
          is resolved server-side from ``WhatsAppMessage.guest.phone`` —
          the form does NOT accept a phone field.

Hard rules enforced by code + tests:

  • Recipient phone is NEVER taken from form input. If the WhatsApp message
    is unlinked (no Guest), the send route refuses with a friendly error.
  • The full AI draft body is rendered to the admin's browser only. Nothing
    in this module writes the draft text to the database.
  • The full inbound body is passed to the AI prompt builder only. It is
    NEVER written to ActivityLog metadata.
  • The full sender phone is never logged. ActivityLog gets last4 only.
  • All three routes are gated by @login_required + @admin_required.
  • No status mutation: booking.status, invoice.payment_status, room.status
    are read-only here.
  • Failures map to friendly per-error_class flash messages; we never
    auto-substitute an approved template behind the admin's back.
"""

from __future__ import annotations

import re

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
)
from flask_login import login_required

from ..models import db, WhatsAppMessage
from ..decorators import admin_required
from ..services.audit import log_activity


whatsapp_inbox_bp = Blueprint('whatsapp_inbox', __name__)


# ── Helpers ──────────────────────────────────────────────────────────

def _phone_last4(phone) -> str:
    """Return the last 4 digits of a phone string, or '' if too short."""
    if not phone:
        return ''
    digits = re.sub(r'\D', '', str(phone))
    return digits[-4:] if len(digits) >= 4 else ''


def _safe_booking_ref(wa_message) -> str:
    """Return the linked booking_ref or None — never raises."""
    booking = getattr(wa_message, 'booking', None)
    return getattr(booking, 'booking_ref', None) if booking else None


# ── GET /admin/whatsapp/messages/<id> — detail view ──────────────────

@whatsapp_inbox_bp.route('/admin/whatsapp/messages/<int:message_id>',
                         methods=['GET'])
@login_required
@admin_required
def message_detail(message_id):
    """Per-inbound-message detail page.

    Loads the WhatsAppMessage and renders message_detail.html. No AI call
    happens on GET — the admin must click "Generate AI Reply Draft" to
    trigger that POST.
    """
    wa_message = WhatsAppMessage.query.get_or_404(message_id)
    return render_template(
        'whatsapp/message_detail.html',
        wa_message=wa_message,
        ai_draft=None,
        prefilled_body=None,
    )


# ── POST /admin/whatsapp/messages/<id>/ai-reply-draft ────────────────

@whatsapp_inbox_bp.route(
    '/admin/whatsapp/messages/<int:message_id>/ai-reply-draft',
    methods=['POST'],
)
@login_required
@admin_required
def ai_reply_draft(message_id):
    """Generate an AI reply draft for an inbound WhatsApp message.

    Pipeline:
        1. Load WhatsAppMessage (404 if missing).
        2. If guest_id is None → render with a friendly "unlinked" notice;
           do NOT call the AI (saves cost + avoids hallucination).
        3. Lazy-import ai_drafts (so the module never appears at top-level
           — preserves test isolation).
        4. Call generate_inbound_reply_draft(wa_message).
        5. Audit ``whatsapp.inbound.ai_reply_draft_created`` with metadata
           whitelist only — no body, no prompt, no raw response.
        6. Render message_detail.html with ai_draft=result.
    """
    wa_message = WhatsAppMessage.query.get_or_404(message_id)

    # Gate: cannot draft a reply for an unlinked message — no recipient
    # phone is recoverable safely without weakening privacy. Render the
    # detail page with a friendly message and skip the AI call entirely.
    if wa_message.guest_id is None or wa_message.guest is None:
        flash(
            'This message is not linked to a known guest, so we cannot '
            'safely auto-fill the recipient phone. Reply manually via the '
            'inbound sender if needed.',
            'warning',
        )
        return render_template(
            'whatsapp/message_detail.html',
            wa_message=wa_message,
            ai_draft={
                'success': False,
                'error': 'unlinked_message',
                'message': (
                    'Cannot generate AI reply: message is not linked '
                    'to a guest. Verify guest identity manually.'
                ),
                'has_booking_context': False,
                'payment_instructions_used': False,
                'inbound_body_length': len(wa_message.body_text or ''),
            },
            prefilled_body=None,
        )

    # Lazy import keeps top-level imports free of ai_drafts → simpler
    # test isolation + smaller blast radius for module-load failures.
    from ..services.ai_drafts import generate_inbound_reply_draft

    result = generate_inbound_reply_draft(wa_message)

    # ── Audit (success or failure) — metadata whitelist only ──
    booking_ref = _safe_booking_ref(wa_message)
    try:
        log_activity(
            'whatsapp.inbound.ai_reply_draft_created',
            description=(
                'Admin generated AI reply draft for inbound WhatsApp.'
                if result.get('success')
                else f'AI reply draft generation failed '
                     f'({result.get("error", "unknown")}).'
            ),
            booking_id=wa_message.booking_id,
            metadata={
                'whatsapp_message_id': wa_message.wa_message_id,
                'booking_id':          wa_message.booking_id,
                'booking_ref':         booking_ref,
                'guest_id':            wa_message.guest_id,
                'draft_type':          'inbound_reply',
                'matched_booking':     wa_message.booking_id is not None,
                'provider':            result.get('provider'),
                'model':               result.get('model'),
                'message_length':      result.get('length_chars'),
                'inbound_body_length': result.get('inbound_body_length'),
                'has_booking_context': result.get('has_booking_context'),
                'payment_instructions_used': result.get(
                    'payment_instructions_used'),
                'success':             bool(result.get('success')),
                'error':               result.get('error'),
            },
        )
        db.session.commit()
    except Exception:
        db.session.rollback()

    # The draft text (if any) is rendered into the page response and
    # then discarded server-side. It is never written to the DB.
    return render_template(
        'whatsapp/message_detail.html',
        wa_message=wa_message,
        ai_draft=result,
        prefilled_body=result.get('draft') if result.get('success') else None,
    )


# ── POST /admin/whatsapp/messages/<id>/send-reply ────────────────────

@whatsapp_inbox_bp.route(
    '/admin/whatsapp/messages/<int:message_id>/send-reply',
    methods=['POST'],
)
@login_required
@admin_required
def send_reply(message_id):
    """Admin-approved manual send of an edited reply draft.

    Form input:
        message_body : the EDITED draft text. NEVER empty. NEVER contains
                       '[admin: …]'. Length ≤ 1500.

    Sender phone is taken from ``wa_message.guest.phone`` — NEVER from
    form input. If the message is unlinked, we refuse with a flash.

    Two-row audit pattern:
        1. ``whatsapp.inbound.reply_send_attempt`` — BEFORE the API call.
        2. ``whatsapp.inbound.reply_sent`` (success) OR
           ``whatsapp.inbound.reply_failed`` (failure) — AFTER.
    """
    wa_message = WhatsAppMessage.query.get_or_404(message_id)

    message_body = (request.form.get('message_body') or '').strip()
    booking_ref = _safe_booking_ref(wa_message)

    # ── Gate: must be linked to a known guest with a phone ──
    guest = wa_message.guest
    if guest is None or not (guest.phone or '').strip():
        flash(
            'Cannot send reply: this message is not linked to a guest with '
            'a phone number on file.',
            'error',
        )
        return redirect(url_for(
            'whatsapp_inbox.message_detail', message_id=message_id))

    phone = guest.phone.strip()
    recipient_last4 = _phone_last4(phone)

    # ── Validation ──
    if not message_body:
        flash('Cannot send: message is empty.', 'error')
        return redirect(url_for(
            'whatsapp_inbox.message_detail', message_id=message_id))

    # Reject leftover '[admin: ...]' placeholder substrings — same guard
    # as ai_draft_send_whatsapp uses on outbound drafts.
    if '[admin:' in message_body.lower():
        flash(
            'Cannot send: draft still contains "[admin: …]" placeholder '
            'text. Please replace placeholders with real values before '
            'sending.',
            'error',
        )
        return redirect(url_for(
            'whatsapp_inbox.message_detail', message_id=message_id))

    if len(message_body) > 1500:
        flash(
            f'Cannot send: message is {len(message_body)} characters; '
            f'the limit is 1500. Trim the message and try again.',
            'error',
        )
        return redirect(url_for(
            'whatsapp_inbox.message_detail', message_id=message_id))

    # ── Audit attempt BEFORE the API call ──
    try:
        log_activity(
            'whatsapp.inbound.reply_send_attempt',
            description=(
                f'Admin attempting WhatsApp reply send '
                f'(booking_ref: {booking_ref or "—"}).'
            ),
            booking_id=wa_message.booking_id,
            metadata={
                'whatsapp_message_id':   wa_message.wa_message_id,
                'booking_id':            wa_message.booking_id,
                'booking_ref':           booking_ref,
                'guest_id':              wa_message.guest_id,
                'draft_type':            'inbound_reply',
                'matched_booking':       wa_message.booking_id is not None,
                'provider':              'whatsapp',
                'recipient_phone_last4': recipient_last4,
                'message_length':        len(message_body),
            },
        )
        db.session.commit()
    except Exception:
        db.session.rollback()

    # ── Lazy import the send wrapper so this module's top-level imports
    #    do not declare a hard dependency on the send service. (Optional
    #    hygiene — does not change behaviour.)
    from ..services.whatsapp import send_text_message

    result = send_text_message(phone, message_body)

    # ── Audit outcome ──
    if result.get('success'):
        try:
            log_activity(
                'whatsapp.inbound.reply_sent',
                description=(
                    f'WhatsApp reply delivered to guest '
                    f'(booking_ref: {booking_ref or "—"}).'
                ),
                booking_id=wa_message.booking_id,
                metadata={
                    'whatsapp_message_id':   wa_message.wa_message_id,
                    'booking_id':            wa_message.booking_id,
                    'booking_ref':           booking_ref,
                    'guest_id':              wa_message.guest_id,
                    'draft_type':            'inbound_reply',
                    'matched_booking':       wa_message.booking_id is not None,
                    'provider':              'whatsapp',
                    'recipient_phone_last4': recipient_last4,
                    'message_length':        len(message_body),
                    'outbound_message_id':   result.get('message_id'),
                    'success':               True,
                },
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
        flash(
            f'Reply sent to guest WhatsApp ending in {recipient_last4}.',
            'success',
        )
        return redirect(url_for(
            'whatsapp_inbox.message_detail', message_id=message_id))

    # Failure path
    error_class = result.get('error_class') or 'unknown'
    try:
        log_activity(
            'whatsapp.inbound.reply_failed',
            description=(
                f'WhatsApp reply send failed (error_class: {error_class}; '
                f'booking_ref: {booking_ref or "—"}).'
            ),
            booking_id=wa_message.booking_id,
            metadata={
                'whatsapp_message_id':   wa_message.wa_message_id,
                'booking_id':            wa_message.booking_id,
                'booking_ref':           booking_ref,
                'guest_id':              wa_message.guest_id,
                'draft_type':            'inbound_reply',
                'matched_booking':       wa_message.booking_id is not None,
                'provider':              'whatsapp',
                'recipient_phone_last4': recipient_last4,
                'message_length':        len(message_body),
                'error_class':           error_class,
                'success':               False,
            },
        )
        db.session.commit()
    except Exception:
        db.session.rollback()

    flash_msg_by_error = {
        'meta_window_closed': (
            "WhatsApp's 24-hour reply window has closed for this conversation. "
            'Open the wa.me link in the inbox and message the guest manually.'
        ),
        'meta_token_invalid': (
            'WhatsApp send failed: the access token is invalid. '
            'Rotate WHATSAPP_TOKEN in production and try again.'
        ),
        'config_disabled': (
            'WhatsApp send failed: WHATSAPP_ENABLED is not true. '
            'Enable WhatsApp sending in the production environment.'
        ),
        'config_invalid': (
            'WhatsApp send failed: WhatsApp configuration is incomplete.'
        ),
        'meta_other': (
            'WhatsApp send failed: Meta API returned an error. Try again, '
            'or message the guest manually.'
        ),
        'network_error': (
            'WhatsApp send failed: network error talking to Meta. Try again.'
        ),
        'validation_phone': (
            'Cannot send: guest phone number is invalid.'
        ),
        'validation_body': (
            'Cannot send: message is empty after trimming.'
        ),
        'validation_too_long': (
            'Cannot send: message is too long.'
        ),
    }
    flash(
        flash_msg_by_error.get(
            error_class,
            f'WhatsApp send failed (error_class: {error_class}).',
        ),
        'error',
    )
    return redirect(url_for(
        'whatsapp_inbox.message_detail', message_id=message_id))
