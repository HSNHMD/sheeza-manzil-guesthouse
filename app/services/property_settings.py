"""Property Settings / Branding Foundation V1 — service layer.

Single source of truth for property-wide settings: branding, contact,
currency, banking, tax, policies. Replaces the env-only branding
reader and the hard-coded payment-instructions module with one
DB-backed editable configuration.

Usage from anywhere in the app:

    from app.services.property_settings import (
        get_settings, get_branding,
        get_payment_instruction_block, get_contact_info,
    )

    s = get_settings()
    s.property_name           # "Sheeza Manzil Guesthouse"
    get_branding()['phone']   # "+960 737 5797"
    get_payment_instruction_block()   # multi-line bank-transfer block

Hard rules:

  - V1 is single-property. The model holds AT MOST one row. The
    migration seeds it. `get_settings()` returns it; if for some
    reason the row is missing (fresh test DB, accidental delete),
    `get_settings()` lazily creates a sensible default — the app
    must never crash because settings are missing.

  - The brand-context Jinja variable (`{{ brand.name }}` everywhere
    in the templates) is now sourced from these settings. The
    legacy `services.branding.get_brand()` reader is preserved as a
    thin wrapper so existing template references keep working.

  - Audit metadata for `update_settings()` records ONLY the names
    of changed fields, NEVER the values. Sensitive fields (bank
    account number, payment instructions text) are still loggable
    by reference but never exposed in plain text in audit rows.
"""

from __future__ import annotations

import os
from typing import Iterable, Optional


# Fields the admin form is allowed to write. Tied to the model
# columns; this is the whitelist.
_EDITABLE_FIELDS = (
    'property_name', 'short_name', 'tagline', 'logo_path',
    'primary_color', 'website_url',
    'email', 'phone', 'whatsapp_number',
    'address', 'city', 'country',
    'currency_code', 'timezone',
    'check_in_time', 'check_out_time',
    'invoice_display_name', 'payment_instructions_text',
    'bank_name', 'bank_account_name', 'bank_account_number',
    'tax_name', 'tax_rate', 'service_charge_rate',
    'booking_terms', 'cancellation_policy', 'wifi_info',
)


# ── Defaults (used only when DB row is somehow missing) ─────────────

def _env_default(name: str, fallback: str) -> str:
    return (os.environ.get(name) or fallback).strip()


def _bootstrap_defaults() -> dict:
    """Defaults used when the singleton row is missing.

    Mirrors the values the migration seeds so a half-migrated test DB
    still gets a sensible answer. Bank details mirror the legacy
    constants in services.payment_instructions so AI drafts render
    correctly even when the DB is fresh.
    """
    return {
        'property_name': _env_default('BRAND_NAME', 'Sheeza Manzil Guesthouse'),
        'short_name':    _env_default('BRAND_SHORT_NAME', 'Sheeza Manzil'),
        'tagline':       _env_default('BRAND_TAGLINE', ''),
        'logo_path':     _env_default('BRAND_LOGO_PATH', '/static/img/logo.png'),
        'primary_color': _env_default('BRAND_PRIMARY_COLOR', '#7B3F00'),
        'currency_code': _env_default('PROPERTY_CURRENCY', 'USD'),
        'timezone':      _env_default('PROPERTY_TIMEZONE', 'Indian/Maldives'),
        'check_in_time': '14:00',
        'check_out_time': '11:00',
        # Bank fallback — same values the migration bulk_inserts and
        # the legacy services.payment_instructions module hard-codes.
        'bank_name':           'Bank of Maldives (BML)',
        'bank_account_name':   'SHEEZA IMAD/MOHAMED S.R.',
        'bank_account_number': '7770000212622',
        'phone':               '+960 737 5797',
    }


# ── Read ────────────────────────────────────────────────────────────

def get_settings(*, autoseed: bool = True):
    """Return the singleton PropertySettings row.

    If the row is missing AND autoseed=True (default), creates one
    with bootstrap defaults and commits. Returns the (possibly newly
    seeded) row. With autoseed=False the function returns None when
    the row is missing — useful for tests that want to assert the
    pre-seed state.
    """
    from ..models import db, PropertySettings

    s = PropertySettings.query.order_by(PropertySettings.id.asc()).first()
    if s is not None:
        return s

    if not autoseed:
        return None

    defaults = _bootstrap_defaults()
    s = PropertySettings(
        property_name=defaults['property_name'],
        short_name=defaults['short_name'],
        tagline=defaults['tagline'] or None,
        logo_path=defaults['logo_path'],
        primary_color=defaults['primary_color'],
        currency_code=defaults['currency_code'],
        timezone=defaults['timezone'],
        check_in_time=defaults['check_in_time'],
        check_out_time=defaults['check_out_time'],
        # Carry the bank/contact fall-backs so AI drafts render
        # correctly even on a fresh DB.
        phone=defaults.get('phone'),
        bank_name=defaults.get('bank_name'),
        bank_account_name=defaults.get('bank_account_name'),
        bank_account_number=defaults.get('bank_account_number'),
        is_active=True,
    )
    db.session.add(s)
    db.session.commit()
    return s


def settings_to_dict(s) -> dict:
    """Return a plain dict of settings — handy for templates and JSON
    APIs. Excludes internal fields (id, created_at, updated_at)."""
    if s is None:
        return {}
    return {f: getattr(s, f, None) for f in _EDITABLE_FIELDS}


# ── Branding (replaces services.branding.get_brand()) ───────────────

def get_branding() -> dict:
    """Return the brand-context dict consumed by the Jinja
    `{{ brand.* }}` namespace. Keys mirror what existing templates
    expect (name, short_name, tagline, logo_path, primary_color)
    plus a few additions surfaced after V1:
        - phone, contact_phone (alias)
        - whatsapp_number
        - bank_name, bank_account_name, bank_account_number
        - bank_account (alias for bank_account_number)
        - currency_code
    """
    s = get_settings()
    color = (s.primary_color or '#0f172a').strip()
    if not color.startswith('#'):
        color = '#' + color
    return {
        'name':           s.property_name,
        'short_name':     s.short_name or s.property_name,
        'tagline':        s.tagline or '',
        'logo_path':      s.logo_path or '/static/img/logo.png',
        'primary_color':  color,

        'phone':          s.phone or '',
        'contact_phone':  s.phone or '',
        'whatsapp_number': s.whatsapp_number or s.phone or '',
        'email':          s.email or '',
        'website_url':    s.website_url or '',

        'address':        s.address or '',
        'city':           s.city or '',
        'country':        s.country or '',

        'currency_code':  s.currency_code or 'USD',
        'check_in_time':  s.check_in_time or '14:00',
        'check_out_time': s.check_out_time or '11:00',

        'bank_name':            s.bank_name or '',
        'bank_account_name':    s.bank_account_name or '',
        'bank_account_number':  s.bank_account_number or '',
        'bank_account':         s.bank_account_number or '',

        'invoice_display_name': s.invoice_display_name or s.property_name,
    }


def get_contact_info() -> dict:
    """Return ONLY the contact-relevant subset of branding."""
    s = get_settings()
    return {
        'phone':           s.phone or '',
        'whatsapp_number': s.whatsapp_number or s.phone or '',
        'email':           s.email or '',
        'website_url':     s.website_url or '',
        'address':         s.address or '',
        'city':            s.city or '',
        'country':         s.country or '',
    }


def get_payment_instruction_block() -> str:
    """Return the canonical payment-instruction text shown to guests
    in AI drafts and confirmation pages.

    Reads from the DB-backed `payment_instructions_text` first.
    Falls back to a synthesized block built from the bank fields if
    the long-form text is empty (so admins can edit just the bank
    fields without re-typing the whole paragraph).

    The returned string is multi-line and ready to be embedded
    verbatim into AI prompts or rendered as <pre> in templates.
    """
    s = get_settings()

    if s.payment_instructions_text and s.payment_instructions_text.strip():
        return s.payment_instructions_text.strip()

    # Build a minimal block from the bank fields.
    lines = ['Bank Transfer Details', '']
    if s.bank_name:
        lines.append(f'Bank: {s.bank_name}')
    if s.bank_account_name:
        lines.append(f'Account Name: {s.bank_account_name}')
    if s.bank_account_number:
        lines.append(f'Account Number: {s.bank_account_number}')
    if len(lines) > 2:
        lines.append('')
    lines.append(
        'Please send the payment slip after transfer so we can '
        'verify your booking.'
    )
    return '\n'.join(lines)


# ── Update ──────────────────────────────────────────────────────────

def update_settings(form_data, *, user=None) -> dict:
    """Apply form input to the singleton row. Caller commits.

    Returns:
        {ok: bool, error: str|None, changed_fields: [str]}.

    Audit metadata records ONLY the list of changed field names,
    never the values. Sensitive numeric / text values (bank account,
    payment instructions, policies) are NOT exposed in audit rows.
    """
    from ..models import db
    from .audit import log_activity

    s = get_settings()

    errors = []
    changed = []

    def _str(name, max_len, *, allow_empty=True):
        if name not in form_data:
            return
        new_val = (form_data.get(name) or '').strip() or None
        if new_val and len(new_val) > max_len:
            errors.append(f'{name}: max {max_len} chars.')
            return
        if not allow_empty and new_val is None:
            errors.append(f'{name}: required.')
            return
        if new_val != getattr(s, name):
            setattr(s, name, new_val)
            changed.append(name)

    def _float(name, *, min_val=None, max_val=None):
        if name not in form_data:
            return
        raw = (form_data.get(name) or '').strip()
        if raw == '':
            new_val = None
        else:
            try:
                new_val = float(raw)
            except (TypeError, ValueError):
                errors.append(f'{name}: must be a number.')
                return
            if min_val is not None and new_val < min_val:
                errors.append(f'{name}: must be ≥ {min_val}.')
                return
            if max_val is not None and new_val > max_val:
                errors.append(f'{name}: must be ≤ {max_val}.')
                return
        if new_val != getattr(s, name):
            setattr(s, name, new_val)
            changed.append(name)

    # Branding
    _str('property_name', 160, allow_empty=False)
    _str('short_name',    80)
    _str('tagline',       255)
    _str('logo_path',     255)
    _str('primary_color', 16)
    _str('website_url',   255)

    # Contact
    _str('email',           120)
    _str('phone',           40)
    _str('whatsapp_number', 40)
    _str('address',         255)
    _str('city',            80)
    _str('country',         80)

    # Operational
    _str('currency_code', 8, allow_empty=False)
    _str('timezone',      64, allow_empty=False)
    _str('check_in_time',  8)
    _str('check_out_time', 8)

    # Billing
    _str('invoice_display_name',     160)
    _str('payment_instructions_text', 4000)
    _str('bank_name',                120)
    _str('bank_account_name',        120)
    _str('bank_account_number',      60)

    # Tax
    _str('tax_name', 40)
    _float('tax_rate',            min_val=0, max_val=100)
    _float('service_charge_rate', min_val=0, max_val=100)

    # Policies
    _str('booking_terms',       4000)
    _str('cancellation_policy', 4000)
    _str('wifi_info',           1000)

    # Primary color normalization
    if s.primary_color and not s.primary_color.startswith('#'):
        s.primary_color = '#' + s.primary_color
        if 'primary_color' not in changed:
            changed.append('primary_color')

    if errors:
        return {'ok': False, 'error': '; '.join(errors),
                'changed_fields': []}

    if changed:
        log_activity(
            'property_settings.updated',
            actor_user_id=getattr(user, 'id', None),
            description=(
                f'Property settings updated '
                f'({len(changed)} field'
                f'{"s" if len(changed) != 1 else ""} changed).'
            ),
            metadata={
                'changed_fields': ','.join(sorted(changed))[:240],
                'changed_count':  len(changed),
                'property_name':  s.property_name,
            },
        )

    return {'ok': True, 'error': None, 'changed_fields': changed}
