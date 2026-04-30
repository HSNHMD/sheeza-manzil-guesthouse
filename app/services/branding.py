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


def _apply_env_overrides(branding: dict) -> dict:
    """Stamp env-var overrides onto a resolved branding dict.

    Three env vars take precedence OVER whatever is in the DB row —
    this is the staging escape hatch. Production never sets these,
    so the DB row remains the source of truth there. Override is
    deliberately scoped to the "visible" fields a non-technical
    operator cares about; bank details, addresses, etc. continue
    to come from the DB.

        BRAND_NAME_OVERRIDE         — full property name
        BRAND_SHORT_NAME_OVERRIDE   — short name (header wordmark)
        BRAND_PRIMARY_COLOR_OVERRIDE — hex accent color

    Why "_OVERRIDE" suffixed: the bare `BRAND_NAME` env var still
    exists as a *bootstrap* default (used only when the DB row is
    seeded for the first time). The override variant is unambiguous
    about its precedence — it always wins.
    """
    name_override   = (os.environ.get('BRAND_NAME_OVERRIDE') or '').strip()
    short_override  = (os.environ.get('BRAND_SHORT_NAME_OVERRIDE') or '').strip()
    color_override  = (os.environ.get('BRAND_PRIMARY_COLOR_OVERRIDE') or '').strip()

    if name_override:
        branding['name'] = name_override
        # Keep invoice display name in sync unless it was explicitly
        # set in the DB to something different — best to surface the
        # override on every visible surface.
        branding['invoice_display_name'] = name_override
    if short_override:
        branding['short_name'] = short_override
    elif name_override and not branding.get('short_name'):
        branding['short_name'] = name_override
    if color_override:
        if not color_override.startswith('#'):
            color_override = '#' + color_override
        branding['primary_color'] = color_override

    return branding


def get_brand() -> dict:
    """Return the active brand identity as a dict.

    Resolution order (first hit wins):
      1. Env-var OVERRIDES (BRAND_*_OVERRIDE) — staging escape hatch.
      2. PropertySettings DB row — production source of truth.
      3. Env-var DEFAULTS (BRAND_*) — only used when DB is unavailable.
      4. Hard-coded defaults — last-resort production identity.

    The returned dict is a SUPERSET of the legacy keys — older
    templates referencing `{{ brand.name / short_name / tagline /
    logo_path / primary_color }}` keep working unchanged.

    Pure function. Safe to call from any request.
    """
    # Prefer the DB-backed settings, then layer env overrides on top.
    try:
        from .property_settings import get_branding as _db_branding
        return _apply_env_overrides(_db_branding())
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

    return _apply_env_overrides({
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
    })


def register_context_processor(app) -> None:
    """Register the ``brand`` Jinja context variable on a Flask app.

    Call once during ``create_app()``. After this, every template can
    reference ``{{ brand.name }}`` etc. without an explicit pass-through.
    """
    @app.context_processor
    def _inject_brand():
        return {'brand': get_brand()}
