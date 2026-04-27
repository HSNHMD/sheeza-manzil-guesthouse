"""AI Draft Assistant — V1.

Generates draft replies to guests using the Anthropic Claude API.

Design rules (binding):

1. **Draft-only.** This module produces TEXT, period. It does NOT call
   `_send_template`, `_send`, smtp, sendmail, or any auto-delivery mechanism.
   Sending is the admin's manual action via the existing wa.me deeplink.

2. **No fabrication.** The prompt builder passes ONLY explicit booking
   fields (booking_ref, room number, dates, total, status). It NEVER passes
   passport numbers, ID types, full address, or uploaded file contents.
   Every prompt instructs Claude to write 'admin: please verify <field>'
   when a fact is unclear, instead of guessing.

3. **No persistence of body.** This module never returns or stores the
   prompt text in the audit log. The route caller logs only metadata
   (draft_type, length, model, success boolean). The draft text itself
   is rendered to the admin's browser and discarded server-side.

4. **Graceful degradation.** Missing `ANTHROPIC_API_KEY` is not an error
   condition — `generate_draft()` returns
   `{'error': 'ai_not_configured', 'message': '...'}` and the booking
   detail page continues to render normally.

5. **Cost containment.** `max_tokens=400`, single attempt per call, no
   retry loop. The route gate (admin-only POST) keeps cost per booking
   bounded by admin clicks.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

try:
    import anthropic  # type: ignore
except ImportError:  # pragma: no cover - import error path
    anthropic = None  # noqa: N816

logger = logging.getLogger(__name__)

# ── Public constants ─────────────────────────────────────────────────────

# The 9 V1 draft types. Order is the user-facing display order.
DRAFT_TYPES = (
    'booking_received',
    'payment_instructions',
    'payment_received_pending_review',
    'booking_confirmed',
    'payment_mismatch',
    'missing_id',
    'missing_payment',
    'checkin_instructions',
    'thank_you_review',
)

# Human labels for the dropdown / log description. Internal use only —
# never sent to Claude as the source of truth.
DRAFT_LABELS = {
    'booking_received':                'Booking received — payment instructions',
    'payment_instructions':            'Payment instructions reminder',
    'payment_received_pending_review': 'Payment slip received — pending review',
    'booking_confirmed':               'Booking confirmed',
    'payment_mismatch':                'Payment amount mismatch',
    'missing_id':                      'Missing ID / passport reminder',
    'missing_payment':                 'Missing payment reminder',
    'checkin_instructions':            'Check-in instructions',
    'thank_you_review':                'Thank-you / review request',
}

# Universal banner the admin sees above every preview. Also embedded in
# the prompt as a header so Claude knows the output is for review-only.
DRAFT_DISCLAIMER = 'AI-generated draft — review before sending.'

# Anthropic call shape.
_DEFAULT_MODEL = 'claude-sonnet-4-6'
_MAX_TOKENS = 400
_TIMEOUT_SECONDS = 20


# ── Lazy client (module-level singleton) ─────────────────────────────────

_client: Optional['anthropic.Anthropic'] = None  # populated on first call


def _get_client():
    """Return a cached Anthropic client, or None if API key / SDK missing.

    Reads `ANTHROPIC_API_KEY` at first call (not module import) so env
    changes during a long-running process are picked up after restart.
    """
    global _client
    if _client is not None:
        return _client
    if anthropic is None:
        return None
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return None
    try:
        _client = anthropic.Anthropic(api_key=api_key)
        return _client
    except Exception as exc:  # pragma: no cover - SDK init error path
        logger.warning('AI drafts: anthropic.Anthropic init failed: %s', exc)
        return None


def _get_model() -> str:
    """Return the model name from env, falling back to a safe default."""
    return os.environ.get('ANTHROPIC_MODEL') or _DEFAULT_MODEL


# ── Prompt construction ──────────────────────────────────────────────────

# Universal system prompt. Same for every draft type.
_SYSTEM_PROMPT = (
    'You write short, polite WhatsApp drafts for the front desk of '
    'Sheeza Manzil Guesthouse, a small family guesthouse in Hanimaadhoo, '
    'Maldives. Your output will be reviewed by a human admin before being '
    'sent — never assume it will go out as-is.\n\n'
    'STRICT RULES:\n'
    '1. Use ONLY the booking facts provided below the divider. Do not invent '
    'prices, room numbers, dates, bank details, transfer references, '
    'policies, or anything not explicitly listed.\n'
    '2. If a fact is missing or marked unclear, write '
    '"[admin: please verify <field>]" instead of guessing.\n'
    '3. Never include passport numbers, ID document numbers, or full '
    'addresses, even if mentioned to you.\n'
    '4. Keep the message concise. Use a friendly, professional tone. '
    'No emoji spam — at most one greeting emoji is fine.\n'
    '5. Write in plain text suitable for WhatsApp. No markdown, no code '
    'fences, no HTML.\n'
    '6. Do not include sign-off phone numbers other than +960 737 5797 '
    '(the official guesthouse number).\n'
    '7. Sign off with "Sheeza Manzil Guesthouse" (no first names).\n'
    '8. Output ONLY the message body — no preamble, no commentary, '
    'no "Here is the draft:" prefix.\n'
)


def _missing(value) -> str:
    """Render a value for the prompt, or '[unknown]' if missing/falsy."""
    if value is None or value == '' or value == 0:
        return '[unknown]'
    return str(value)


def _booking_facts(booking) -> str:
    """Serialize the SAFE subset of booking fields for the prompt.

    EXPLICITLY EXCLUDED (privacy):
        guest.id_number, guest.id_type, guest.address,
        booking.id_card_filename, booking.payment_slip_filename,
        any drive_id / R2 object key.
    """
    inv = getattr(booking, 'invoice', None)
    payment_status = getattr(inv, 'payment_status', None) if inv else None
    amount_paid = getattr(inv, 'amount_paid', None) if inv else None
    invoice_number = getattr(inv, 'invoice_number', None) if inv else None
    has_phone = bool(getattr(booking.guest, 'phone', None))
    has_payment_slip = bool(getattr(booking, 'payment_slip_filename', None))
    has_id_card = bool(getattr(booking, 'id_card_filename', None))

    lines = [
        f'guest_first_name: {_missing(booking.guest.first_name)}',
        f'guest_last_name:  {_missing(booking.guest.last_name)}',
        f'booking_ref:      {_missing(booking.booking_ref)}',
        f'room_number:      {_missing(booking.room.number)}',
        f'room_type:        {_missing(booking.room.room_type)}',
        f'check_in_date:    {_missing(booking.check_in_date)}',
        f'check_out_date:   {_missing(booking.check_out_date)}',
        f'nights:           {_missing(booking.nights)}',
        f'num_guests:       {_missing(booking.num_guests)}',
        f'total_amount_mvr: {_missing(booking.total_amount)}',
        f'booking_status:   {_missing(booking.status)}',
        f'invoice_number:   {_missing(invoice_number)}',
        f'payment_status:   {_missing(payment_status)}',
        f'amount_paid_mvr:  {_missing(amount_paid)}',
        f'has_phone:        {has_phone}',
        f'has_id_card_uploaded:     {has_id_card}',
        f'has_payment_slip_uploaded:{has_payment_slip}',
    ]
    return '\n'.join(lines)


# Per-draft-type instruction snippets. Each one is appended to the system
# prompt before the booking facts.
_DRAFT_INSTRUCTIONS = {
    'booking_received': (
        'Draft a message acknowledging that the guest\'s booking has been '
        'received. Tell them payment instructions will follow. Mention the '
        'booking_ref, room, dates. Do NOT confirm the booking is accepted '
        '— it is awaiting payment review.'
    ),
    'payment_instructions': (
        'Draft a message providing bank-transfer payment instructions. '
        'Mention the booking_ref, total_amount_mvr, and ask the guest to '
        'send a payment slip when done. Do NOT include actual bank account '
        'numbers — write "[admin: please paste current bank details]" '
        'where the account info should go. The admin will fill it in.'
    ),
    'payment_received_pending_review': (
        'Draft a message acknowledging that the payment slip has been '
        'received and is being reviewed by the team. Tell them they will '
        'get a confirmation once the payment is verified. Mention booking_ref.'
    ),
    'booking_confirmed': (
        'Draft a message confirming the booking is fully confirmed. '
        'Include booking_ref, room, check_in_date, check_out_date, nights, '
        'and total_amount_mvr. Express looking forward to welcoming them.'
    ),
    'payment_mismatch': (
        'Draft a polite message explaining that the amount received does '
        'not match the expected total_amount_mvr. Ask the guest to check '
        'and either send the difference or contact us. Mention booking_ref. '
        'Do NOT specify the amount received — write "[admin: please confirm '
        'amount received]" so the admin can fill it in.'
    ),
    'missing_id': (
        'Draft a polite reminder that we still need a copy of the ID or '
        'passport for check-in. Mention booking_ref. Do NOT mention the '
        'specific ID type or number even if known.'
    ),
    'missing_payment': (
        'Draft a polite reminder that payment has not yet been received '
        'for booking_ref. Ask them to send the payment slip when done. '
        'Mention total_amount_mvr.'
    ),
    'checkin_instructions': (
        'Draft a brief check-in instructions message. Mention room_number, '
        'check_in_date, and that the front desk is at the official phone '
        '+960 737 5797 for early arrivals or special arrangements. '
        'Mention booking_ref.'
    ),
    'thank_you_review': (
        'Draft a brief thank-you message after check-out. Mention the '
        'guest\'s first name and politely invite a Google review. Do NOT '
        'invent a review URL — write "[admin: paste Google review link]" '
        'as a placeholder.'
    ),
}


def build_prompt(draft_type: str, booking) -> str:
    """Construct the full user-message prompt for Claude.

    Pure function — no API call. Safe to call in unit tests without
    network access. Raises ValueError on unknown draft_type.
    """
    if draft_type not in DRAFT_TYPES:
        raise ValueError(f'unknown draft_type: {draft_type!r}')

    instructions = _DRAFT_INSTRUCTIONS[draft_type]
    facts = _booking_facts(booking)

    return (
        f'{instructions}\n\n'
        '────────────────────────────────────────\n'
        'BOOKING FACTS (use ONLY these — guess nothing):\n'
        '────────────────────────────────────────\n'
        f'{facts}\n'
        '────────────────────────────────────────\n\n'
        'Output the message body now. No preamble. No commentary.'
    )


# ── State gating ─────────────────────────────────────────────────────────

def can_draft(booking, draft_type: str) -> bool:
    """Soft-gate: is this draft type appropriate for this booking state?

    Used to show / hide buttons in the UI. The route allow-list is the
    HARD gate — this is just UX. Returns True for unknown types so a
    future draft_type works without a code edit (route still validates).
    """
    if draft_type not in DRAFT_TYPES:
        return True
    bs = (booking.status or '').strip()
    inv = getattr(booking, 'invoice', None)
    ps = (getattr(inv, 'payment_status', '') if inv else '').strip()

    pre_confirmed = bs in ('new_request', 'pending_payment', 'payment_uploaded',
                           'payment_verified', 'unconfirmed', 'pending_verification')

    if draft_type == 'booking_received':
        return pre_confirmed
    if draft_type == 'payment_instructions':
        return pre_confirmed and ps not in ('verified', 'paid')
    if draft_type == 'payment_received_pending_review':
        return ps == 'pending_review'
    if draft_type == 'booking_confirmed':
        return bs == 'confirmed'
    if draft_type == 'payment_mismatch':
        return ps == 'mismatch'
    if draft_type == 'missing_id':
        return not getattr(booking, 'id_card_filename', None)
    if draft_type == 'missing_payment':
        return pre_confirmed and not getattr(booking, 'payment_slip_filename', None)
    if draft_type == 'checkin_instructions':
        return bs == 'confirmed'
    if draft_type == 'thank_you_review':
        return bs == 'checked_out'
    return True


# ── Generation ───────────────────────────────────────────────────────────

def generate_draft(draft_type: str, booking) -> dict:
    """Generate a draft via Claude. Never raises — returns a result dict.

    Success shape:
        {'success': True, 'draft': str, 'model': str, 'length_chars': int,
         'draft_type': str}

    Error shapes:
        {'success': False, 'error': 'invalid_draft_type'}
        {'success': False, 'error': 'ai_not_configured',
         'message': 'AI draft assistant is not configured.'}
        {'success': False, 'error': 'ai_unavailable',
         'message': 'AI draft service is temporarily unavailable.'}
    """
    if draft_type not in DRAFT_TYPES:
        return {'success': False, 'error': 'invalid_draft_type'}

    client = _get_client()
    if client is None:
        return {
            'success': False,
            'error': 'ai_not_configured',
            'message': 'AI draft assistant is not configured.',
        }

    try:
        prompt = build_prompt(draft_type, booking)
    except Exception as exc:
        # Programming error in prompt builder — log and surface generic.
        logger.warning('AI drafts: prompt build failed for %s: %s',
                       draft_type, exc)
        return {'success': False, 'error': 'prompt_build_failed'}

    model = _get_model()

    try:
        response = client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            timeout=_TIMEOUT_SECONDS,
            system=_SYSTEM_PROMPT,
            messages=[{'role': 'user', 'content': prompt}],
        )
    except Exception as exc:
        # NOTE: deliberately log only the exception class — never headers,
        # API key, or full response body.
        logger.warning('AI drafts: API call failed (%s)', type(exc).__name__)
        return {
            'success': False,
            'error': 'ai_unavailable',
            'message': 'AI draft service is temporarily unavailable.',
        }

    try:
        # Anthropic SDK returns response.content as a list of content blocks.
        body = ''
        for block in getattr(response, 'content', []):
            text = getattr(block, 'text', None)
            if text:
                body += text
        body = body.strip()
    except Exception as exc:  # pragma: no cover - shape error path
        logger.warning('AI drafts: response parsing failed (%s)',
                       type(exc).__name__)
        return {
            'success': False,
            'error': 'ai_unavailable',
            'message': 'AI draft service returned an unexpected response.',
        }

    if not body:
        return {
            'success': False,
            'error': 'ai_empty_response',
            'message': 'AI draft service returned an empty response.',
        }

    return {
        'success': True,
        'draft': body,
        'model': model,
        'length_chars': len(body),
        'draft_type': draft_type,
    }
