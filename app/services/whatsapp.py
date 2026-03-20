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

import os
import logging

try:
    import requests as _requests
except ImportError:
    _requests = None

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
_TOKEN    = os.environ.get('WHATSAPP_TOKEN', '')
_PHONE_ID = os.environ.get('WHATSAPP_PHONE_NUMBER_ID') or os.environ.get('WHATSAPP_PHONE_ID', '')
_ENABLED  = os.environ.get('WHATSAPP_ENABLED', 'false').lower() == 'true'
_API_URL  = 'https://graph.facebook.com/v18.0/{phone_id}/messages'
_LANG     = 'en'

STAFF_PHONE = '9607375797'

# Meta error codes that mean "template not approved yet" — fail silently
_TEMPLATE_NOT_READY_CODES = {132000, 132001, 132005, 132007, 132008, 132015}


def _config_status() -> dict:
    """Return current config state — used by the test route."""
    return {
        'enabled':       _ENABLED,
        'has_token':     bool(_TOKEN),
        'token_prefix':  (_TOKEN[:8] + '…') if len(_TOKEN) >= 8 else (_TOKEN or '(empty)'),
        'phone_id':      _PHONE_ID or '(empty)',
        'api_url':       _API_URL,
        'language':      _LANG,
    }


# ── Credential / library guard ─────────────────────────────────────────────────
def _check_config() -> str | None:
    """Return an error string if config is incomplete, else None."""
    if not _ENABLED:
        return 'WHATSAPP_ENABLED is not true'
    if not _TOKEN:
        return 'WHATSAPP_TOKEN is not set'
    if not _PHONE_ID:
        return 'WHATSAPP_PHONE_NUMBER_ID is not set'
    if _requests is None:
        return 'requests library not installed'
    return None


def _clean_phone(phone: str) -> str:
    return phone.lstrip('+').replace(' ', '').replace('-', '')


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
    url = _API_URL.format(phone_id=_PHONE_ID)
    payload = {
        'messaging_product': 'whatsapp',
        'to': to_clean,
        'type': 'text',
        'text': {'preview_url': False, 'body': text},
    }
    headers = {'Authorization': f'Bearer {_TOKEN}', 'Content-Type': 'application/json'}

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
    url = _API_URL.format(phone_id=_PHONE_ID)
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
    headers = {'Authorization': f'Bearer {_TOKEN}', 'Content-Type': 'application/json'}

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
    Template: staff_new_booking (PENDING APPROVAL)
    Params: booking_ref, guest_name, guest_phone, room, check_in, check_out, total_mvr
    Sent to STAFF_PHONE on portal submission.
    Fails silently if template not yet approved.
    """
    params = [
        booking.booking_ref,
        booking.guest.full_name,
        booking.guest.phone or 'N/A',
        f"Room {booking.room.number} — {booking.room.room_type}",
        booking.check_in_date.strftime('%d %B %Y'),
        booking.check_out_date.strftime('%d %B %Y'),
        f"{booking.total_amount:.0f}",
    ]
    return _send_template(STAFF_PHONE, 'staff_new_booking', params, pending_approval=True)['success']


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
