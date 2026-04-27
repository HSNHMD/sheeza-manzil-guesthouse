"""
Meta WhatsApp Cloud API service — template message edition.

Required environment variables (set on Railway):
    WHATSAPP_TOKEN            — permanent access token from Meta Business dashboard
    WHATSAPP_PHONE_NUMBER_ID  — the Phone Number ID (not the number itself)
    WHATSAPP_ENABLED          — set to 'true' to activate sending

Templates used (all language: en_US):
    booking_confirmed   — sent to guest when staff confirms   (APPROVED)
    booking_received    — sent to guest on portal submission  (PENDING)
    staff_new_booking   — sent to staff on portal submission  (PENDING)

Pending-approval templates fail gracefully: the booking flow is never blocked.
"""

import json
import logging
import os

try:
    import requests as _requests
except ImportError:
    _requests = None

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
# Read at call time (not module load time) so Railway env vars are always current.
_API_BASE = 'https://graph.facebook.com/v18.0'
_LANG     = 'en'

STAFF_PHONE = '9607375797'

# Meta error codes that mean "template not approved yet" — fail silently
_TEMPLATE_NOT_READY_CODES = {132000, 132001, 132005, 132007, 132008, 132015}


def _get_token()    -> str:  return os.environ.get('WHATSAPP_TOKEN', '')
def _get_phone_id() -> str:  return os.environ.get('WHATSAPP_PHONE_NUMBER_ID', '') or os.environ.get('WHATSAPP_PHONE_ID', '')
def _is_enabled()   -> bool: return os.environ.get('WHATSAPP_ENABLED', 'false').lower() == 'true'
def _api_url()      -> str:  return f'{_API_BASE}/{_get_phone_id()}/messages'


def _config_status() -> dict:
    """Return current config state — used by the test route."""
    token    = _get_token()
    phone_id = _get_phone_id()
    return {
        'enabled':       _is_enabled(),
        'has_token':     bool(token),
        'token_prefix':  (token[:8] + '…') if len(token) >= 8 else (token or '(empty)'),
        'phone_id':      phone_id or '(empty)',
        'api_url':       f'{_API_BASE}/{phone_id or "(missing)"}/messages',
        'language':      _LANG,
    }


# ── Credential / library guard ─────────────────────────────────────────────────
def _check_config():
    """Return an error string if config is incomplete, else None."""
    if not _is_enabled():
        return 'WHATSAPP_ENABLED is not true'
    if not _get_token():
        return 'WHATSAPP_TOKEN is not set'
    if not _get_phone_id():
        return 'WHATSAPP_PHONE_NUMBER_ID is not set'
    if _requests is None:
        return 'requests library not installed'
    return None


def _clean_phone(phone: str) -> str:
    """Strip + and whitespace; ensure Maldives local numbers get 960 prefix."""
    cleaned = phone.lstrip('+').replace(' ', '').replace('-', '')
    # If it looks like a bare 7-digit Maldives local number, prepend 960
    if len(cleaned) == 7 and cleaned[0] in '234567':
        cleaned = '960' + cleaned
    return cleaned


# ── Low-level: plain text (used by test route only) ────────────────────────────
def _send(to: str, text: str) -> dict:
    """
    Send a free-form text message.
    Note: Meta only allows free-form to numbers that have messaged you first
    (within 24 h window). Use for test / internal purposes only.
    Returns dict: {success, status_code, response_body, error}
    """
    result = {'success': False, 'status_code': None, 'response_body': None, 'error': None}
    err = _check_config()
    if err:
        result['error'] = err
        logger.info('[WhatsApp] %s', err)
        return result

    to_clean = _clean_phone(to)
    url = _api_url()
    payload = {
        'messaging_product': 'whatsapp',
        'to': to_clean,
        'type': 'text',
        'text': {'preview_url': False, 'body': text},
    }
    headers = {'Authorization': f'Bearer {_get_token()}', 'Content-Type': 'application/json'}

    try:
        resp = _requests.post(url, json=payload, headers=headers, timeout=10)
        result['status_code'] = resp.status_code
        result['response_body'] = resp.text
        if resp.status_code == 200:
            logger.info('[WhatsApp] text sent OK to %s', to_clean)
            result['success'] = True
        else:
            result['error'] = f'HTTP {resp.status_code}'
            logger.error('[WhatsApp] text error %s to %s: %s', resp.status_code, to_clean, resp.text)
    except Exception as exc:
        result['error'] = str(exc)
        logger.error('[WhatsApp] text exception to %s: %s', to_clean, exc)

    return result


# ── Low-level: template message ────────────────────────────────────────────────
def _send_template(to: str, template_name: str, params: list,
                   pending_approval: bool = False) -> dict:
    """
    Send a Meta-approved template message.

    params: list of string values for {{1}}, {{2}}, … body placeholders.
    pending_approval: if True, template-not-ready errors are logged as INFO
                      (not ERROR) and treated as non-fatal.

    Returns dict: {success, status_code, response_body, error, template_not_ready}
    """
    result = {
        'success': False, 'status_code': None,
        'response_body': None, 'error': None, 'template_not_ready': False,
    }
    err = _check_config()
    if err:
        result['error'] = err
        logger.info('[WhatsApp] %s — skipping template %s', err, template_name)
        return result

    to_clean = _clean_phone(to)
    url = _api_url()
    payload = {
        'messaging_product': 'whatsapp',
        'to': to_clean,
        'type': 'template',
        'template': {
            'name': template_name,
            'language': {'code': _LANG},
            'components': [{
                'type': 'body',
                'parameters': [{'type': 'text', 'text': str(p)} for p in params],
            }],
        },
    }
    headers = {'Authorization': f'Bearer {_get_token()}', 'Content-Type': 'application/json'}

    try:
        resp = _requests.post(url, json=payload, headers=headers, timeout=10)
        result['status_code'] = resp.status_code
        result['response_body'] = resp.text

        if resp.status_code == 200:
            logger.info('[WhatsApp] template "%s" sent OK to %s', template_name, to_clean)
            result['success'] = True
        else:
            # Check if it's a template-not-ready error
            try:
                body = resp.json()
                api_code = body.get('error', {}).get('code', 0)
            except Exception:
                api_code = 0

            if api_code in _TEMPLATE_NOT_READY_CODES:
                result['template_not_ready'] = True
                result['error'] = f'Template "{template_name}" not yet approved (code {api_code})'
                if pending_approval:
                    logger.info('[WhatsApp] %s — skipping silently', result['error'])
                else:
                    logger.warning('[WhatsApp] %s', result['error'])
            else:
                result['error'] = f'HTTP {resp.status_code}: {resp.text}'
                logger.error('[WhatsApp] template "%s" error %s to %s: %s',
                             template_name, resp.status_code, to_clean, resp.text)
    except Exception as exc:
        result['error'] = str(exc)
        logger.error('[WhatsApp] template "%s" exception to %s: %s', template_name, to_clean, exc)

    return result


# ── Public message functions ───────────────────────────────────────────────────

def send_booking_confirmation(booking) -> bool:
    """
    Template: booking_confirmed (APPROVED)
    Params: guest_name, booking_ref, room, check_in, check_out, total_mvr
    Sent when staff clicks Confirm.
    """
    phone = booking.guest.phone
    if not phone:
        return False

    params = [
        booking.guest.full_name,
        booking.booking_ref,
        f"Room {booking.room.number} — {booking.room.room_type}",
        booking.check_in_date.strftime('%d %B %Y'),
        booking.check_out_date.strftime('%d %B %Y'),
        f"{booking.total_amount:.0f}",
    ]
    return _send_template(phone, 'booking_confirmed', params, pending_approval=False)['success']


def send_booking_acknowledgment(booking) -> bool:
    """
    Template: booking_received (PENDING APPROVAL)
    Params: guest_name, booking_ref, room, check_in, check_out, total_mvr
    Sent to guest immediately after portal submission.
    Fails silently if template not yet approved.
    """
    phone = booking.guest.phone
    if not phone:
        return False

    params = [
        booking.guest.full_name,
        booking.booking_ref,
        f"Room {booking.room.number} — {booking.room.room_type}",
        booking.check_in_date.strftime('%d %B %Y'),
        booking.check_out_date.strftime('%d %B %Y'),
        f"{booking.total_amount:.0f}",
    ]
    return _send_template(phone, 'booking_received', params, pending_approval=True)['success']


def send_staff_new_booking_notification(booking) -> bool:
    """
    Template: staff_new_booking
    Params: booking_ref, guest_name, guest_phone, room_number, check_in, check_out, total_mvr
    Status is hardcoded in the template — not a parameter.
    Sent to STAFF_PHONE on portal submission.
    Fails silently if template not yet approved.
    """
    params = [
        booking.booking_ref,
        booking.guest.full_name,
        booking.guest.phone or 'N/A',
        booking.room.number,
        booking.check_in_date.strftime('%d %B %Y'),
        booking.check_out_date.strftime('%d %B %Y'),
        f"{booking.total_amount:.0f}",
    ]
    return _send_template(STAFF_PHONE, 'staff_new_booking', params, pending_approval=False)['success']


def send_checkin_reminder(booking) -> bool:
    """Free-form check-in reminder (within 24 h window after guest messages first)."""
    phone = booking.guest.phone
    if not phone:
        return False

    text = (
        f"Dear {booking.guest.full_name} ☀️\n\n"
        f"This is a reminder that your check-in is today!\n\n"
        f"Booking Ref: {booking.booking_ref}\n"
        f"Room: {booking.room.number}\n"
        f"Check-in: {booking.check_in_date.strftime('%d %B %Y')}\n\n"
        f"Please arrive at the front desk at your convenience. "
        f"Call us at +960 737 5797 for early check-in or special arrangements."
    )
    return _send(phone, text)['success']


def send_checkout_invoice_summary(booking, invoice) -> bool:
    """Free-form checkout invoice summary."""
    phone = booking.guest.phone
    if not phone:
        return False

    balance_line = (
        "Fully Paid ✅"
        if invoice.payment_status == 'paid'
        else f"Balance Due: MVR {invoice.balance_due:.0f} ⚠️"
    )
    text = (
        f"Thank you for staying with us, {booking.guest.full_name}! 🙏\n\n"
        f"Invoice: {invoice.invoice_number}\n"
        f"Room {booking.room.number} × {booking.nights} "
        f"night{'s' if booking.nights != 1 else ''}\n"
        f"Total: MVR {invoice.total_amount:.0f}\n"
        f"{balance_line}\n\n"
        f"We hope to see you again soon at Sheeza Manzil Guesthouse! 🌟"
    )
    return _send(phone, text)['success']


# ── V2: Admin-approved free-form text send ─────────────────────────────────
#
# Used by the AI Draft Approval workflow on the booking detail page. Wraps
# the existing `_send()` with three safety improvements over calling _send
# directly from a route handler:
#
#   1. Validates phone + body lengths BEFORE the HTTP call, so obviously
#      bad input never reaches Meta.
#   2. Returns a normalized result dict that strips the raw Meta response
#      and carries only a short symbolic error_class. The caller is
#      therefore safe to log the entire return value to ActivityLog
#      without any chance of leaking phone numbers or token-bearing
#      headers from Meta's echo.
#   3. Parses the wamid.… message_id out of the success body so the
#      audit log can correlate sent messages with Meta's records.
#
# WhatsApp 24-hour free-form window (binding):
#   Meta only allows free-form text to numbers that have messaged the
#   business within the last 24 hours. Outside that window, Meta returns
#   error 131047 ("re-engagement") and we map it to error_class
#   'meta_window_closed'. The caller's UI should suggest using the
#   existing wa.me deeplink as a fallback rather than inventing template
#   names — this module deliberately does NOT auto-fall-back to a
#   template, because that would silently change the message content.

_ADMIN_TEXT_MIN_LEN = 1
_ADMIN_TEXT_MAX_LEN = 1500


def _classify_send_error(raw: dict) -> str:
    """Map a raw `_send()` failure result to a short symbolic error class.

    The caller logs only the returned string, never `raw`, so this
    function fully controls what reaches the audit log.
    """
    err  = (raw.get('error') or '').lower()
    body = (raw.get('response_body') or '').lower()
    code = raw.get('status_code')

    # Config-side problems surface here as Python-side errors from
    # _check_config() — they predate any HTTP call.
    if 'is not true' in err and 'whatsapp_enabled' in err.lower() + body:
        return 'config_disabled'
    # _check_config returns one of these literal strings on failure:
    if err.startswith('whatsapp_enabled is not true'):
        return 'config_disabled'
    if err.startswith('whatsapp_token is not set') or err.startswith('whatsapp_phone'):
        return 'config_invalid'
    if err.startswith('requests library'):
        return 'config_invalid'

    # Meta API errors — match by code first, then known error-code substrings
    # in the body. Body is matched lowercased; values like '131047' are
    # never reflected to the audit log.
    if code == 401 or '132018' in body or 'invalid token' in body:
        return 'meta_token_invalid'
    if code == 400 and (
        '131047' in body
        or 're-engagement' in body
        or '24-hour' in body
        or '24 hour' in body
    ):
        return 'meta_window_closed'
    if code is not None and 400 <= code < 600:
        return 'meta_other'

    # No status code + an exception string usually means network failure.
    if raw.get('error') and code is None:
        return 'network_error'

    return 'unknown'


def send_text_message(to_phone, body: str) -> dict:
    """V2 admin-approved free-form text send.

    Args:
        to_phone: phone string (any format; will be normalized by `_clean_phone`).
        body:     message text. Whitespace is stripped before validation.

    Returns a normalized result dict that is SAFE TO LOG IN FULL:
        {'success': True,  'message_id': str | None, 'error_class': None}
        {'success': False, 'message_id': None,       'error_class': str}

    The return value never includes:
      * the raw Meta response body (may contain echoed phone metadata)
      * the full destination phone (caller decides what to log)
      * any header / token string

    Validation error_class values:
        'validation_phone'    — phone empty or missing
        'validation_body'     — body empty after strip
        'validation_too_long' — body > 1500 chars

    Send-path error_class values:
        'config_disabled'    — WHATSAPP_ENABLED is not true
        'config_invalid'     — token / phone-id / requests lib missing
        'meta_token_invalid' — Meta 401 / token rejected (rotate)
        'meta_window_closed' — Meta 131047, outside 24-hour reply window
        'meta_other'         — any other Meta 4xx/5xx
        'network_error'      — exception inside the HTTP call
        'unknown'            — defensive default
    """
    # ── Input validation (cheap; runs before any HTTP call) ──
    if not to_phone or not str(to_phone).strip():
        return {'success': False, 'message_id': None,
                'error_class': 'validation_phone'}

    text = (body or '').strip()
    if len(text) < _ADMIN_TEXT_MIN_LEN:
        return {'success': False, 'message_id': None,
                'error_class': 'validation_body'}
    if len(text) > _ADMIN_TEXT_MAX_LEN:
        return {'success': False, 'message_id': None,
                'error_class': 'validation_too_long'}

    # ── Delegate to internal _send (which handles config + HTTP) ──
    raw = _send(to_phone, text)

    if raw.get('success'):
        # Parse out the wamid.… message_id from the JSON envelope.
        # Anything unexpected leaves message_id as None — never raises.
        msg_id = None
        try:
            parsed = json.loads(raw.get('response_body') or '{}')
            messages = parsed.get('messages') or []
            if messages and isinstance(messages, list):
                first = messages[0] or {}
                msg_id = first.get('id') if isinstance(first, dict) else None
        except Exception:
            pass
        return {'success': True, 'message_id': msg_id, 'error_class': None}

    return {
        'success': False,
        'message_id': None,
        'error_class': _classify_send_error(raw),
    }
