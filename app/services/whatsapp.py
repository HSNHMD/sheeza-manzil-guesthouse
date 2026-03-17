"""
Meta WhatsApp Cloud API service.

Required environment variables:
    WHATSAPP_TOKEN    — permanent access token from Meta Business dashboard
    WHATSAPP_PHONE_ID — the Phone Number ID (not the number itself)

Currently wired up but NOT activated. To enable, set WHATSAPP_ENABLED=true
in your environment or .env file.

Usage:
    from app.services.whatsapp import send_booking_confirmation
    send_booking_confirmation(booking)
"""

import os
import logging

try:
    import requests as _requests
except ImportError:
    _requests = None

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
_TOKEN = os.environ.get('WHATSAPP_TOKEN', '')
_PHONE_ID = os.environ.get('WHATSAPP_PHONE_ID', '')
_ENABLED = os.environ.get('WHATSAPP_ENABLED', 'false').lower() == 'true'
_API_URL = 'https://graph.facebook.com/v19.0/{phone_id}/messages'


# ── Low-level sender ──────────────────────────────────────────────────────────
def _send(to: str, text: str) -> bool:
    """
    Send a plain-text WhatsApp message via the Cloud API.

    Args:
        to:   Recipient phone number in E.164 format (e.g. '9607123456').
              Leading '+' is stripped automatically.
        text: Message body (max ~4096 chars).

    Returns:
        True on success, False on any failure.
    """
    if not _ENABLED:
        logger.info('[WhatsApp DISABLED] Would send to %s: %s', to, text[:80])
        return False

    if not _TOKEN or not _PHONE_ID:
        logger.error('WhatsApp credentials not configured (WHATSAPP_TOKEN / WHATSAPP_PHONE_ID)')
        return False

    if _requests is None:
        logger.error('requests library not installed — cannot send WhatsApp message')
        return False

    to = to.lstrip('+').replace(' ', '').replace('-', '')
    url = _API_URL.format(phone_id=_PHONE_ID)
    payload = {
        'messaging_product': 'whatsapp',
        'to': to,
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
            logger.info('WhatsApp sent to %s', to)
            return True
        logger.error('WhatsApp API error %s: %s', resp.status_code, resp.text)
        return False
    except Exception as exc:
        logger.error('WhatsApp send failed: %s', exc)
        return False


# ── Message builders ──────────────────────────────────────────────────────────
def send_booking_confirmation(booking) -> bool:
    """
    Send a booking confirmation to the guest's WhatsApp number.
    Triggered after a new booking is created.

    booking: app.models.Booking instance (with .guest and .room loaded).
    """
    phone = booking.guest.phone
    if not phone:
        logger.warning('No phone for guest %s — skipping WhatsApp confirmation', booking.guest.full_name)
        return False

    text = (
        f"Hi {booking.guest.first_name} 👋\n\n"
        f"Your booking at *Sheeza Manzil Guesthouse* is confirmed!\n\n"
        f"📋 *Booking Ref:* {booking.booking_ref}\n"
        f"🏠 *Room:* {booking.room.number} ({booking.room.room_type})\n"
        f"📅 *Check-in:* {booking.check_in_date.strftime('%A, %d %B %Y')}\n"
        f"📅 *Check-out:* {booking.check_out_date.strftime('%A, %d %B %Y')}\n"
        f"🌙 *Nights:* {booking.nights}\n"
        f"💰 *Total:* MVR {booking.total_amount:.0f}\n\n"
        f"We look forward to welcoming you. Reply to this message if you have any questions."
    )
    return _send(phone, text)


def send_checkin_reminder(booking) -> bool:
    """
    Send a check-in day reminder to the guest.
    Intended to be triggered on the morning of check-in date.

    booking: app.models.Booking instance.
    """
    phone = booking.guest.phone
    if not phone:
        return False

    text = (
        f"Good morning {booking.guest.first_name} ☀️\n\n"
        f"This is a reminder that your check-in is *today*!\n\n"
        f"📋 *Ref:* {booking.booking_ref}\n"
        f"🏠 *Room:* {booking.room.number}\n"
        f"📅 *Check-in:* {booking.check_in_date.strftime('%d %B %Y')}\n\n"
        f"Please arrive at the front desk at your convenience. "
        f"Let us know if you need an early check-in or any special arrangements."
    )
    return _send(phone, text)


def send_checkout_invoice_summary(booking, invoice) -> bool:
    """
    Send an invoice summary to the guest on check-out.
    Triggered automatically when a guest is checked out.

    booking: app.models.Booking instance.
    invoice: app.models.Invoice instance.
    """
    phone = booking.guest.phone
    if not phone:
        return False

    balance_line = (
        f"✅ *Fully Paid*"
        if invoice.payment_status == 'paid'
        else f"⚠️ *Balance Due:* MVR {invoice.balance_due:.0f}"
    )

    text = (
        f"Thank you for staying with us, {booking.guest.first_name}! 🙏\n\n"
        f"*Invoice Summary*\n"
        f"─────────────────\n"
        f"📄 *Invoice:* {invoice.invoice_number}\n"
        f"🏠 *Room:* {booking.room.number} × {booking.nights} night{'s' if booking.nights != 1 else ''}\n"
        f"💰 *Subtotal:* MVR {invoice.subtotal:.0f}\n"
        f"🧾 *Tax ({invoice.tax_rate:.0f}%):* MVR {invoice.tax_amount:.0f}\n"
        f"💳 *Total:* MVR {invoice.total_amount:.0f}\n"
        f"{balance_line}\n\n"
        f"We hope to see you again soon! 🌟"
    )
    return _send(phone, text)


def send_staff_notification(message: str, staff_phone: str) -> bool:
    """
    Send an internal notification to a staff member's WhatsApp.
    Use for alerts like new bookings, maintenance requests, etc.

    message:     Plain text to send.
    staff_phone: Staff member's phone in E.164 format.
    """
    if not staff_phone:
        return False
    text = f"[Sheeza Manzil Staff Alert]\n\n{message}"
    return _send(staff_phone, text)
