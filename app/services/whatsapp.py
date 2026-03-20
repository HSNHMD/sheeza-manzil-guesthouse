"""
Meta WhatsApp Cloud API service.

Required environment variables (set on Railway):
    WHATSAPP_TOKEN            — permanent access token from Meta Business dashboard
    WHATSAPP_PHONE_NUMBER_ID  — the Phone Number ID (not the number itself)
    WHATSAPP_ENABLED          — set to 'true' to activate sending

Usage:
    from app.services.whatsapp import send_booking_acknowledgment
    send_booking_acknowledgment(booking)
"""

import os
import logging

try:
    import requests as _requests
except ImportError:
    _requests = None

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
_TOKEN    = os.environ.get('WHATSAPP_TOKEN', '')
# Support both naming conventions (Railway uses WHATSAPP_PHONE_NUMBER_ID)
_PHONE_ID = os.environ.get('WHATSAPP_PHONE_NUMBER_ID') or os.environ.get('WHATSAPP_PHONE_ID', '')
_ENABLED  = os.environ.get('WHATSAPP_ENABLED', 'false').lower() == 'true'
_API_URL  = 'https://graph.facebook.com/v18.0/{phone_id}/messages'

STAFF_PHONE = '9607375797'   # front-desk notification recipient


# ── Low-level sender ──────────────────────────────────────────────────────────
def _send(to: str, text: str) -> bool:
    """
    Send a plain-text WhatsApp message via the Cloud API.
    Always non-blocking — logs errors but never raises.
    """
    if not _ENABLED:
        logger.info('[WhatsApp DISABLED] Would send to %s: %s', to, text[:80])
        return False

    if not _TOKEN or not _PHONE_ID:
        logger.error('WhatsApp credentials missing (WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID)')
        return False

    if _requests is None:
        logger.error('requests library not installed')
        return False

    to_clean = to.lstrip('+').replace(' ', '').replace('-', '')
    url = _API_URL.format(phone_id=_PHONE_ID)
    payload = {
        'messaging_product': 'whatsapp',
        'to': to_clean,
        'type': 'text',
        'text': {'preview_url': False, 'body': text},
    }
    headers = {
        'Authorization': f'Bearer {_TOKEN}',
        'Content-Type': 'application/json',
    }

    try:
        resp = _requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            logger.info('WhatsApp sent to %s', to_clean)
            return True
        logger.error('WhatsApp API error %s: %s', resp.status_code, resp.text)
        return False
    except Exception as exc:
        logger.error('WhatsApp send failed: %s', exc)
        return False


# ── Message functions ─────────────────────────────────────────────────────────

def send_booking_acknowledgment(booking) -> bool:
    """
    Sent to the guest immediately after they submit a booking through /book.
    Booking is unconfirmed or pending_verification at this point.
    """
    phone = booking.guest.phone
    if not phone:
        return False

    text = (
        f"Dear {booking.guest.full_name}, thank you for your booking at "
        f"Sheeza Manzil Guesthouse! 🏨\n\n"
        f"Booking Reference: {booking.booking_ref}\n"
        f"Room: {booking.room.number} — {booking.room.room_type}\n"
        f"Check-in: {booking.check_in_date.strftime('%A, %d %B %Y')}\n"
        f"Check-out: {booking.check_out_date.strftime('%A, %d %B %Y')}\n"
        f"Total: MVR {booking.total_amount:.0f}\n\n"
        f"Your booking will be confirmed once payment is verified.\n"
        f"For assistance: +960 737 5797"
    )
    return _send(phone, text)


def send_booking_confirmation(booking) -> bool:
    """
    Sent to the guest when staff clicks Confirm (booking status → confirmed).
    Also sent for bookings created directly by staff (already confirmed).
    """
    phone = booking.guest.phone
    if not phone:
        return False

    text = (
        f"Dear {booking.guest.full_name}, your booking at "
        f"Sheeza Manzil Guesthouse is CONFIRMED! ✅\n\n"
        f"Booking Reference: {booking.booking_ref}\n"
        f"Room: {booking.room.number} — {booking.room.room_type}\n"
        f"Check-in: {booking.check_in_date.strftime('%A, %d %B %Y')}\n"
        f"Check-out: {booking.check_out_date.strftime('%A, %d %B %Y')}\n\n"
        f"We look forward to welcoming you!\n"
        f"+960 737 5797"
    )
    return _send(phone, text)


def send_staff_new_booking_notification(booking) -> bool:
    """
    Sent to the staff phone whenever a guest submits a booking through /book.
    """
    status_label = booking.status.replace('_', ' ').title()
    text = (
        f"New booking received! 🔔\n\n"
        f"{booking.booking_ref}\n"
        f"Guest: {booking.guest.full_name}\n"
        f"Room: {booking.room.number} — {booking.room.room_type}\n"
        f"{booking.check_in_date.strftime('%d %b %Y')} to "
        f"{booking.check_out_date.strftime('%d %b %Y')}\n"
        f"Status: {status_label}\n"
        f"Total: MVR {booking.total_amount:.0f}"
    )
    return _send(STAFF_PHONE, text)


def send_checkin_reminder(booking) -> bool:
    """Sent to the guest on the day of check-in."""
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
    return _send(phone, text)


def send_checkout_invoice_summary(booking, invoice) -> bool:
    """Sent to the guest on check-out with invoice summary."""
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
    return _send(phone, text)
