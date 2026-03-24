import base64
import json
import logging
import os

import anthropic

from ..models import EXPENSE_CATEGORIES

_SUPPORTED_MEDIA = {'image/jpeg', 'image/png'}
_client: anthropic.Anthropic | None = None
logger = logging.getLogger(__name__)


def _get_client() -> anthropic.Anthropic | None:
    global _client
    if _client is None:
        api_key = os.environ.get('ANTHROPIC_API_KEY')
        if not api_key:
            return None
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def scan_receipt(image_bytes: bytes, media_type: str) -> dict:
    """Scan a receipt image via Claude vision. Returns extracted fields dict or {'error': str}."""
    if media_type == 'application/pdf':
        return {'error': 'PDF scanning not supported'}

    client = _get_client()
    if client is None:
        return {'error': 'API key not configured'}

    if media_type == 'image/jpg':
        media_type = 'image/jpeg'
    if media_type not in _SUPPORTED_MEDIA:
        return {'error': 'Unsupported image format'}

    categories_str = ', '.join(EXPENSE_CATEGORIES)
    image_b64 = base64.standard_b64encode(image_bytes).decode('utf-8')

    try:
        response = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=512,
            messages=[{
                'role': 'user',
                'content': [
                    {
                        'type': 'image',
                        'source': {
                            'type': 'base64',
                            'media_type': media_type,
                            'data': image_b64,
                        },
                    },
                    {
                        'type': 'text',
                        'text': (
                            'You are a receipt data extractor for a hotel in the Maldives. '
                            'Extract data from this receipt and return ONLY a valid JSON object '
                            'with NO markdown formatting or code fences.\n\n'
                            'Fields to extract:\n'
                            '- date: expense date as YYYY-MM-DD (omit if unclear)\n'
                            '- vendor: supplier name, max 50 chars\n'
                            '- amount: total as a plain number in MVR. '
                            'Convert from other currencies if needed (1 USD = 15.4 MVR, 1 EUR = 17 MVR). '
                            'No currency symbol.\n'
                            f'- category: one of: {categories_str}\n'
                            '- description: brief description, max 80 chars\n\n'
                            'If you cannot read the receipt, return exactly: '
                            '{"error": "Could not read receipt clearly"}\n\n'
                            'Return ONLY the JSON object, nothing else.'
                        ),
                    },
                ],
            }],
        )
    except Exception as exc:
        logger.error('Receipt scan API error: %s', exc)
        return {'error': 'Scan service unavailable'}

    raw = response.content[0].text.strip()

    # Strip markdown code fences if Claude wrapped the JSON
    if raw.startswith('```'):
        lines = raw.splitlines()
        inner = []
        for line in lines[1:]:
            if line.strip() == '```':
                break
            inner.append(line)
        raw = '\n'.join(inner)

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {'error': 'Could not extract data from receipt'}

    if 'error' in data:
        return data

    # Normalise category — exact match first, then 4-char prefix, then 'Other'
    category = str(data.get('category', ''))
    if category not in EXPENSE_CATEGORIES:
        category = next(
            (c for c in EXPENSE_CATEGORIES if c.lower().startswith(category.lower()[:4])),
            'Other',
        )

    # Normalise amount — strip commas, convert to float
    raw_amount = data.get('amount', 0)
    try:
        if isinstance(raw_amount, str):
            raw_amount = raw_amount.replace(',', '').strip()
        amount = round(float(raw_amount), 2)
    except (ValueError, TypeError):
        amount = 0.0

    return {
        'date': str(data.get('date', '')),
        'vendor': str(data.get('vendor', ''))[:50],
        'amount': amount,
        'category': category,
        'description': str(data.get('description', ''))[:80],
    }
