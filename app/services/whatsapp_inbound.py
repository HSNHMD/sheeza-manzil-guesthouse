"""Inbound-WhatsApp helpers.

Pure-function utilities used by the webhook route handler. Splitting
these out of `app/routes/whatsapp_webhook.py` keeps the route thin and
makes signature verification, payload parsing, phone hashing, and
booking matching individually unit-testable without a Flask context.

Privacy contract (binding):
- The full sender phone number is NEVER persisted. Only an HMAC-SHA256
  hash (keyed by SECRET_KEY) and the last 4 digits.
- The full message body IS stored on the WhatsAppMessage row (admin
  needs to read it) but is NEVER passed to ActivityLog.
- The raw Meta payload is NEVER stored — only parsed fields.
- Verify token + app secret are NEVER logged. Log lines reference
  short symbolic outcomes only ('verify.success', 'sig.invalid', etc).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from datetime import datetime
from typing import Iterable, Optional


# ── Signature verification ──────────────────────────────────────────

def verify_signature(raw_body: bytes,
                     signature_header: Optional[str],
                     app_secret: Optional[str]) -> bool:
    """Constant-time verification of Meta's X-Hub-Signature-256 header.

    Returns True iff:
        - app_secret is set AND non-empty
        - signature_header starts with "sha256="
        - HMAC-SHA256(app_secret, raw_body) hex-equals the rest

    Returns False on any other input. Never raises. Never logs.
    """
    if not app_secret or not signature_header:
        return False
    if not signature_header.startswith('sha256='):
        return False
    received = signature_header[len('sha256='):]
    try:
        expected = hmac.new(
            app_secret.encode('utf-8'),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
    except Exception:
        return False
    return hmac.compare_digest(expected, received)


def verify_token_match(received: Optional[str],
                       configured: Optional[str]) -> bool:
    """Constant-time comparison of the GET-handshake hub.verify_token.

    Both arguments must be non-empty for the result to be True.
    """
    if not received or not configured:
        return False
    return hmac.compare_digest(received, configured)


# ── Phone normalization + privacy-preserving hashing ─────────────────

def normalize_phone(phone: Optional[str]) -> str:
    """Return the digit-only form of a phone number ('' on missing).

    No country-code mangling — Meta delivers `from` as digits-only
    in E.164 form (e.g. '9607001234'). Guest.phone may have +/spaces;
    `re.sub(r'\\D', '', ...)` normalizes both to the same form.
    """
    return re.sub(r'\D', '', phone or '')


def phone_last4(phone: Optional[str]) -> str:
    """Return the last 4 digits of a phone (or '' if shorter)."""
    norm = normalize_phone(phone)
    return norm[-4:] if len(norm) >= 4 else ''


def hash_phone(phone: Optional[str],
               secret_key: Optional[str] = None) -> str:
    """Return HMAC-SHA256(SECRET_KEY, normalized_phone) truncated to 16 hex.

    Returns '' for empty input. The truncation to 64 bits is more than
    enough for a small guesthouse (collision probability negligible
    under realistic guest counts) and keeps the column small.

    The hash is reversible only by an attacker who already has both the
    SECRET_KEY and the candidate phone number — i.e. it provides
    correlation without re-identification.
    """
    norm = normalize_phone(phone)
    if not norm:
        return ''
    key = (secret_key or os.environ.get('SECRET_KEY', '')).encode('utf-8')
    return hmac.new(key, norm.encode('utf-8'),
                    hashlib.sha256).hexdigest()[:16]


# ── Webhook payload parsing ──────────────────────────────────────────

# Message types where we know how to extract a body_text. Only 'text'
# carries plain user text; the others are media types that we record
# without body. Anything else → 'unsupported_<type>'.
_KNOWN_MESSAGE_TYPES = frozenset((
    'text', 'image', 'audio', 'video', 'document',
    'location', 'sticker', 'contacts', 'button', 'interactive',
))


def parse_webhook_payload(payload) -> list:
    """Extract a list of normalized message dicts from a Meta payload.

    Returns:
        list of dicts, each with keys:
            wa_message_id, from_phone, profile_name,
            message_type, body_text, wa_timestamp (datetime|None)

    Tolerant of malformed input: any structural problem in the payload
    skips that entry rather than raising. Returns [] for non-dict input.
    """
    if not isinstance(payload, dict):
        return []

    messages_out = []
    for entry in (payload.get('entry') or []):
        if not isinstance(entry, dict):
            continue
        for change in (entry.get('changes') or []):
            if not isinstance(change, dict):
                continue
            if change.get('field') != 'messages':
                continue
            value = change.get('value') or {}
            if not isinstance(value, dict):
                continue

            # Build a wa_id → profile_name map from the contacts list.
            profile_by_wa = {}
            for c in (value.get('contacts') or []):
                if not isinstance(c, dict):
                    continue
                wa_id = c.get('wa_id')
                pname = ((c.get('profile') or {}).get('name')
                         if isinstance(c.get('profile'), dict) else None)
                if wa_id:
                    profile_by_wa[wa_id] = pname

            for msg in (value.get('messages') or []):
                if not isinstance(msg, dict):
                    continue

                wa_id  = str(msg.get('id') or '')[:128]
                from_  = str(msg.get('from') or '')[:32]
                ts_str = str(msg.get('timestamp') or '')
                raw_type = str(msg.get('type') or 'unknown')

                # Body extraction (text only)
                body = None
                if raw_type == 'text':
                    text_block = msg.get('text') or {}
                    if isinstance(text_block, dict):
                        body = text_block.get('body')
                        if body is not None:
                            body = str(body)

                # Categorize message_type
                if raw_type == 'text':
                    stored_type = 'text'
                elif raw_type in _KNOWN_MESSAGE_TYPES:
                    stored_type = raw_type
                else:
                    stored_type = f'unsupported_{raw_type}'[:30]

                # Convert epoch string to datetime
                wa_ts = None
                try:
                    wa_ts = datetime.utcfromtimestamp(int(ts_str))
                except (ValueError, TypeError, OverflowError):
                    pass

                messages_out.append({
                    'wa_message_id': wa_id,
                    'from_phone':    from_,
                    'profile_name':  profile_by_wa.get(from_),
                    'message_type':  stored_type,
                    'body_text':     body,
                    'wa_timestamp':  wa_ts,
                })
    return messages_out


# ── Guest / booking matching ─────────────────────────────────────────

def match_inbound_sender(from_phone: str):
    """Return (guest_id, booking_id) for an inbound sender phone.

    Strategy:
      - Normalize the inbound phone to digits-only.
      - For each Guest with a non-empty phone, normalize and compare.
      - If exactly one Guest matches, return its (id, most-recent-active-
        booking-id).
      - If 0 or >1 matches, return (None, None).

    The "most recent active booking" is the booking with the latest
    check_in_date among non-terminal statuses. If the guest has no
    eligible booking, we still return their guest_id (so the inbox
    can group by guest).
    """
    from ..models import Guest, Booking  # lazy: avoid model-load races

    target = normalize_phone(from_phone)
    if not target:
        return (None, None)

    matches = []
    for g in Guest.query.all():
        if g.phone and normalize_phone(g.phone) == target:
            matches.append(g)

    if len(matches) != 1:
        return (None, None)

    guest = matches[0]
    # Pick most recent non-cancelled, non-rejected booking for this guest.
    booking = (
        Booking.query
        .filter(Booking.guest_id == guest.id,
                Booking.status.notin_(('cancelled', 'rejected')))
        .order_by(Booking.check_in_date.desc())
        .first()
    )
    return (guest.id, booking.id if booking else None)
