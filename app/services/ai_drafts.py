"""AI Draft Assistant — V1.

Generates draft replies to guests using a configurable AI provider.

Currently supported providers:
    * ``gemini``   — Google Gemini (default; cheap Flash/Flash-Lite models).
                     Called via REST so no extra Python SDK is required —
                     just the existing ``requests`` library.
    * ``anthropic``— Anthropic Claude. Uses the ``anthropic`` SDK that is
                     already vendored for the receipt OCR feature.

Provider is selected at call-time via the ``AI_DRAFT_PROVIDER`` env var
(falls back to gemini). Model is selected via ``AI_DRAFT_MODEL`` (falls
back to the provider's default). Each provider has its own API-key env
var (``GEMINI_API_KEY`` / ``ANTHROPIC_API_KEY``) so they coexist without
collision.

Design rules (binding):

1. **Draft-only.** This module produces TEXT, period. It does NOT call
   any WhatsApp / SMTP / sendmail mechanism. Sending is the admin's
   manual action via the existing wa.me deeplink.

2. **No fabrication.** The prompt builder passes ONLY explicit booking
   fields (booking_ref, room number, dates, total, status). It NEVER
   passes passport numbers, ID types, full address, or uploaded file
   contents. Every prompt instructs the model to write
   '[admin: please verify <field>]' when a fact is unclear.

3. **No persistence of body.** This module never returns or stores the
   prompt text in the audit log. The route caller logs only metadata
   (draft_type, provider, model, length_chars, booking_ref). The draft
   text itself is rendered to the admin's browser and discarded
   server-side.

4. **Graceful degradation.** Missing API key for the active provider is
   not an error — ``generate_draft()`` returns
   ``{'error': 'ai_not_configured', 'message': '...'}`` and the booking
   detail page continues to render normally.

5. **Cost containment.** ``max_tokens=400``, single attempt per call,
   no retry loop. The route gate (admin-only POST) bounds cost per
   booking by admin clicks.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

# Anthropic SDK is optional — only required when provider == 'anthropic'.
try:
    import anthropic  # type: ignore
except ImportError:  # pragma: no cover - import error path
    anthropic = None  # noqa: N816

# requests is required for the Gemini REST call. It is in requirements.txt
# already (used by the WhatsApp service), so this should always be present.
try:
    import requests as _requests  # type: ignore
except ImportError:  # pragma: no cover
    _requests = None

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

DRAFT_DISCLAIMER = 'AI-generated draft — review before sending.'


# ── Provider config ──────────────────────────────────────────────────────

PROVIDERS = ('gemini', 'anthropic')
_DEFAULT_PROVIDER = 'gemini'

_DEFAULT_MODEL_BY_PROVIDER = {
    # Cheap, fast, current Gemini Flash-Lite. Operator can override via
    # AI_DRAFT_MODEL if they want a different price/quality point.
    'gemini':    'gemini-2.5-flash-lite',
    # Sonnet 4.6 is the current Anthropic mid-tier. Operator can downgrade
    # to a Haiku or upgrade to an Opus via AI_DRAFT_MODEL.
    'anthropic': 'claude-sonnet-4-6',
}

_MAX_TOKENS = 400
_TIMEOUT_SECONDS = 20

# Gemini REST endpoint (v1beta supports all current Flash/Flash-Lite models).
_GEMINI_API_BASE = 'https://generativelanguage.googleapis.com/v1beta/models'


def _get_provider() -> str:
    """Return the active provider, falling back to default if unset/invalid."""
    raw = (os.environ.get('AI_DRAFT_PROVIDER') or '').strip().lower()
    if raw in PROVIDERS:
        return raw
    if raw:
        # Unknown value — log once at INFO so operator sees the fallback
        # in the journal. The route surfaces the error separately.
        logger.info('AI drafts: unknown AI_DRAFT_PROVIDER=%r — '
                    'falling back to %s', raw, _DEFAULT_PROVIDER)
    return _DEFAULT_PROVIDER


def _resolve_model(provider: str) -> str:
    """Return the model name for ``provider``.

    Precedence:
      1. ``AI_DRAFT_MODEL`` env var (provider-agnostic override)
      2. ``ANTHROPIC_MODEL`` env var (legacy; only when provider=anthropic)
      3. Per-provider default in _DEFAULT_MODEL_BY_PROVIDER
    """
    unified = os.environ.get('AI_DRAFT_MODEL')
    if unified:
        return unified
    if provider == 'anthropic':
        legacy = os.environ.get('ANTHROPIC_MODEL')
        if legacy:
            return legacy
    return _DEFAULT_MODEL_BY_PROVIDER.get(provider, '')


def _is_provider_configured(provider: str) -> bool:
    """Return True if the API key for ``provider`` is set."""
    if provider == 'gemini':
        return bool(os.environ.get('GEMINI_API_KEY'))
    if provider == 'anthropic':
        return bool(os.environ.get('ANTHROPIC_API_KEY'))
    return False


# ── Anthropic client (lazy singleton) ────────────────────────────────────

_anthropic_client: Optional['anthropic.Anthropic'] = None


def _get_anthropic_client():
    """Return a cached Anthropic client, or None if SDK / key missing."""
    global _anthropic_client
    if _anthropic_client is not None:
        return _anthropic_client
    if anthropic is None:
        return None
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return None
    try:
        _anthropic_client = anthropic.Anthropic(api_key=api_key)
        return _anthropic_client
    except Exception as exc:  # pragma: no cover - SDK init error path
        logger.warning('AI drafts: anthropic.Anthropic init failed (%s)',
                       type(exc).__name__)
        return None


# ── Prompt construction (provider-agnostic) ──────────────────────────────

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
    if value is None or value == '' or value == 0:
        return '[unknown]'
    return str(value)


def _booking_facts(booking) -> str:
    """Serialize the SAFE subset of booking fields for the prompt."""
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
        'where the account info should go.'
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
    """Construct the user-message prompt. Pure function — no API call."""
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
    """Soft-gate: is this draft type appropriate for this booking state?"""
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


# ── Provider call dispatchers ────────────────────────────────────────────

def _call_anthropic(system: str, user: str, model: str) -> dict:
    """Call the Anthropic API. Returns {'success': True, 'text': str} or
    {'success': False, 'error': '<short>'}. Never raises."""
    client = _get_anthropic_client()
    if client is None:
        return {'success': False, 'error': 'ai_not_configured'}
    try:
        response = client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            timeout=_TIMEOUT_SECONDS,
            system=system,
            messages=[{'role': 'user', 'content': user}],
        )
    except Exception as exc:
        logger.warning('AI drafts: Anthropic call failed (%s)',
                       type(exc).__name__)
        return {'success': False, 'error': 'ai_unavailable'}
    try:
        body = ''
        for block in getattr(response, 'content', []):
            text = getattr(block, 'text', None)
            if text:
                body += text
        body = body.strip()
    except Exception as exc:  # pragma: no cover - shape error
        logger.warning('AI drafts: Anthropic parse failed (%s)',
                       type(exc).__name__)
        return {'success': False, 'error': 'ai_unavailable'}
    if not body:
        return {'success': False, 'error': 'ai_empty_response'}
    return {'success': True, 'text': body}


def _call_gemini(system: str, user: str, model: str) -> dict:
    """Call the Gemini REST API. Returns same shape as _call_anthropic.

    Uses ``requests`` (already in deps); no extra Python SDK required.
    """
    if _requests is None:
        return {'success': False, 'error': 'ai_not_configured'}
    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key:
        return {'success': False, 'error': 'ai_not_configured'}

    url = f'{_GEMINI_API_BASE}/{model}:generateContent'
    payload = {
        'system_instruction': {'parts': [{'text': system}]},
        'contents': [{'role': 'user',
                      'parts': [{'text': user}]}],
        'generationConfig': {
            'maxOutputTokens': _MAX_TOKENS,
            # Slightly conservative temperature for hospitality drafts.
            'temperature': 0.4,
        },
    }
    try:
        resp = _requests.post(
            url,
            params={'key': api_key},
            json=payload,
            timeout=_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        # NEVER log headers, payload, or the API key.
        logger.warning('AI drafts: Gemini call failed (%s)',
                       type(exc).__name__)
        return {'success': False, 'error': 'ai_unavailable'}

    if resp.status_code != 200:
        # Log the status code only — the response body may contain echoed
        # request metadata that we do not want in the journal.
        logger.warning('AI drafts: Gemini HTTP %s', resp.status_code)
        return {'success': False, 'error': 'ai_unavailable'}

    try:
        body = resp.json()
    except Exception as exc:
        logger.warning('AI drafts: Gemini JSON decode failed (%s)',
                       type(exc).__name__)
        return {'success': False, 'error': 'ai_unavailable'}

    candidates = (body or {}).get('candidates') or []
    if not candidates:
        return {'success': False, 'error': 'ai_empty_response'}

    parts = (candidates[0].get('content') or {}).get('parts') or []
    text = ''.join(p.get('text', '') for p in parts if isinstance(p, dict))
    text = text.strip()
    if not text:
        return {'success': False, 'error': 'ai_empty_response'}
    return {'success': True, 'text': text}


def _call_provider(provider: str, system: str, user: str, model: str) -> dict:
    """Dispatch to the right provider. Tests can patch this to bypass
    actual SDK / HTTP plumbing entirely."""
    if provider == 'anthropic':
        return _call_anthropic(system, user, model)
    if provider == 'gemini':
        return _call_gemini(system, user, model)
    return {'success': False, 'error': 'invalid_provider'}


# ── Public entry point ───────────────────────────────────────────────────

def generate_draft(draft_type: str, booking) -> dict:
    """Generate a draft. Never raises — returns a result dict.

    Success shape:
        {'success': True, 'draft': str, 'provider': str, 'model': str,
         'length_chars': int, 'draft_type': str}

    Error shapes:
        {'success': False, 'error': 'invalid_draft_type'}
        {'success': False, 'error': 'invalid_provider',
         'message': 'Configured AI provider is not supported.'}
        {'success': False, 'error': 'ai_not_configured',
         'message': 'AI draft assistant is not configured.',
         'provider': str}
        {'success': False, 'error': 'ai_unavailable',
         'message': 'AI draft service is temporarily unavailable.',
         'provider': str}
        {'success': False, 'error': 'ai_empty_response', ...}
        {'success': False, 'error': 'prompt_build_failed'}
    """
    if draft_type not in DRAFT_TYPES:
        return {'success': False, 'error': 'invalid_draft_type'}

    # Resolve provider FIRST so error responses can include it for UX.
    raw_provider = (os.environ.get('AI_DRAFT_PROVIDER') or '').strip().lower()
    if raw_provider and raw_provider not in PROVIDERS:
        return {
            'success': False,
            'error': 'invalid_provider',
            'message': (f'Configured AI provider {raw_provider!r} is not '
                        f'supported. Allowed: {", ".join(PROVIDERS)}.'),
        }

    provider = _get_provider()  # validated — falls back to default
    model = _resolve_model(provider)

    if not _is_provider_configured(provider):
        return {
            'success': False,
            'error': 'ai_not_configured',
            'provider': provider,
            'message': 'AI draft assistant is not configured.',
        }

    try:
        prompt = build_prompt(draft_type, booking)
    except Exception as exc:
        logger.warning('AI drafts: prompt build failed for %s (%s)',
                       draft_type, type(exc).__name__)
        return {
            'success': False,
            'error': 'prompt_build_failed',
            'provider': provider,
        }

    result = _call_provider(provider, _SYSTEM_PROMPT, prompt, model)

    if not result.get('success'):
        # Pass through whatever short error the provider returned, but add
        # a friendly user-facing message so the route can render cleanly.
        err = result.get('error', 'ai_unavailable')
        msg_by_err = {
            'ai_not_configured':
                'AI draft assistant is not configured.',
            'ai_unavailable':
                'AI draft service is temporarily unavailable.',
            'ai_empty_response':
                'AI draft service returned an empty response.',
        }
        return {
            'success': False,
            'error': err,
            'provider': provider,
            'message': msg_by_err.get(err, 'AI draft generation failed.'),
        }

    body = result['text']
    return {
        'success': True,
        'draft': body,
        'provider': provider,
        'model': model,
        'length_chars': len(body),
        'draft_type': draft_type,
    }
