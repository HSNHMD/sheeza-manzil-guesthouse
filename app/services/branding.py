"""Centralized branding configuration.

Branding values are read from environment variables with sensible defaults
that match the production identity ("Sheeza Manzil Guesthouse"). Staging or
demo deployments override the env vars to swap in a different brand without
any template edits or code changes.

Production-safety contract (binding):
- ALL defaults equal the historical hard-coded production values. A
  production deployment with NO new env vars must produce identical
  output to the pre-branding-refactor codebase.
- Reads happen at request time via a Jinja context processor. There is
  no module-level frozen value, so a service restart picks up env
  changes without code changes.
- This module is pure-config: no DB calls, no Flask context required
  (except the context processor wrapper).

Available env vars:
    BRAND_NAME          — full property name (e.g. "Sheeza Manzil Guesthouse")
    BRAND_SHORT_NAME    — short form (e.g. "Sheeza Manzil")
    BRAND_TAGLINE       — optional tagline
    BRAND_LOGO_PATH     — static URL path to the logo image
                          (default '/static/img/logo.png')
    BRAND_PRIMARY_COLOR — optional hex color for inline accents
                          (default '#7B3F00')

Templates access these via the ``brand`` context variable:

    {{ brand.name }}              {# full name #}
    {{ brand.short_name }}        {# short form #}
    {{ brand.tagline }}           {# may be empty #}
    {{ brand.logo_path }}         {# url path to logo #}
    {{ brand.primary_color }}     {# hex with leading # #}

The original Sheeza branding is captured here as the default tuple, so
any deployment that does NOT set env vars retains the existing look.
"""

from __future__ import annotations

import os


# Production defaults — DO NOT change without explicit prod approval.
_DEFAULT_NAME          = 'Sheeza Manzil Guesthouse'
_DEFAULT_SHORT_NAME    = 'Sheeza Manzil'
_DEFAULT_TAGLINE       = ''
_DEFAULT_LOGO_PATH     = '/static/img/logo.png'
_DEFAULT_PRIMARY_COLOR = '#7B3F00'


def get_brand() -> dict:
    """Return the active brand identity as a dict.

    V1 / V2 (Property Settings + Multi-Property Foundation): reads
    from the active Property's settings (via `services.property
    .current_property()`), falling back to PropertySettings then to
    env-var defaults if the DB is unavailable. The returned dict is
    a SUPERSET of the legacy keys — older templates referencing
    `{{ brand.name / short_name / tagline / logo_path / primary_color }}`
    keep working unchanged.

    Multi-property note: when a request resolves to a specific
    Property, the brand context follows automatically because
    services.property_settings.get_branding() reads from the
    PropertySettings row linked to that Property. In V2 with a
    single property the path collapses to a single PropertySettings
    lookup — same answer.

    Pure function. Safe to call from any request.
    """
    # Prefer the DB-backed settings, which themselves now respect the
    # active property.
    try:
        from .property_settings import get_branding as _db_branding
        return _db_branding()
    except Exception:
        # Falls through to env defaults — keeps the page rendering
        # if the DB is mid-migration or completely unavailable.
        pass

    name        = (os.environ.get('BRAND_NAME')          or _DEFAULT_NAME).strip()
    short_name  = (os.environ.get('BRAND_SHORT_NAME')    or _DEFAULT_SHORT_NAME).strip()
    tagline     = (os.environ.get('BRAND_TAGLINE')       or _DEFAULT_TAGLINE).strip()
    logo_path   = (os.environ.get('BRAND_LOGO_PATH')     or _DEFAULT_LOGO_PATH).strip()
    color       = (os.environ.get('BRAND_PRIMARY_COLOR') or _DEFAULT_PRIMARY_COLOR).strip()

    if not color.startswith('#'):
        color = '#' + color

    return {
        'name':           name,
        'short_name':     short_name,
        'tagline':        tagline,
        'logo_path':      logo_path,
        'primary_color':  color,
        # Sensible empty defaults for the new keys so old code that
        # falls through to env-vars still gets a stable shape.
        'phone':                '',
        'contact_phone':        '',
        'whatsapp_number':      '',
        'email':                '',
        'website_url':          '',
        'address':              '',
        'city':                 '',
        'country':              '',
        'currency_code':        'USD',
        'check_in_time':        '14:00',
        'check_out_time':       '11:00',
        'bank_name':            '',
        'bank_account_name':    '',
        'bank_account_number':  '',
        'bank_account':         '',
        'invoice_display_name': name,
    }


def register_context_processor(app) -> None:
    """Register the ``brand`` Jinja context variable on a Flask app.

    Call once during ``create_app()``. After this, every template can
    reference ``{{ brand.name }}`` etc. without an explicit pass-through.
    """
    @app.context_processor
    def _inject_brand():
        return {'brand': get_brand()}
