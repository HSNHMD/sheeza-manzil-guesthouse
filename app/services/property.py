"""Multi-Property Foundation V1 — current-property service.

Single source of truth for "which property is this request operating
on?" V1 is single-property: there is exactly one row in `properties`
and `current_property()` always returns it.

Future multi-property work will replace the body of `current_property()`
with the URL-prefix → subdomain → session → user-default chain
described in docs/multi_property_foundation_plan.md §4. Routes that
read from the active property today will continue to work unchanged
when that resolver evolves — they only see the return value, not the
selection logic.

Hard rule:

  - `current_property_id()` is the only function model `default=`
    lambdas should call to resolve their `property_id`. It auto-seeds
    the singleton if the row is missing, so existing tests keep
    working without explicit setup.

  - This module DOES NOT mutate any other property-scoped model. Its
    sole responsibility is keeping the Property row addressable.

  - The `services.property_settings` module is unchanged. Its
    `get_settings()` continues to return the PropertySettings
    singleton; eventually it will read through `current_property()`'s
    `settings_id`, but for V1 the singleton-per-row pattern is
    sufficient.
"""

from __future__ import annotations

from typing import Optional


# ── Read ────────────────────────────────────────────────────────────

def current_property(*, autoseed: bool = True):
    """Return the active Property row.

    V1: returns the only row in `properties` (the singleton). With
    `autoseed=True` (default), creates the row if missing — useful in
    tests that bypass migrations.
    """
    from ..models import db, Property
    from .property_settings import get_settings

    p = Property.query.order_by(Property.id.asc()).first()
    if p is not None:
        return p
    if not autoseed:
        return None

    # Seed: create the singleton from the existing PropertySettings
    # row (which itself auto-seeds if absent).
    settings = get_settings()
    p = Property(
        code='default',
        name=settings.property_name or 'Default Property',
        short_name=settings.short_name,
        timezone=settings.timezone or 'Indian/Maldives',
        currency_code=settings.currency_code or 'USD',
        is_active=True,
        settings_id=settings.id,
    )
    db.session.add(p)
    # flush() — never commit() inside an auto-seed. Committing here
    # would close the caller's open transaction (e.g. a test that's
    # mid-INSERT). flush() gives us p.id without altering caller state.
    db.session.flush()
    return p


def current_property_id() -> Optional[int]:
    """Return the active Property.id (auto-seeds when missing).

    Used by model `default=` lambdas so existing tests + migration
    backfill paths get a sensible value without setup ceremony.
    """
    p = current_property()
    return p.id if p is not None else None


# ── Query scoping helper ────────────────────────────────────────────

def for_current_property(query):
    """Filter a SQLAlchemy query by the active property.

    Convenience wrapper used by V1 read paths to add a property scope
    without each route having to import `current_property_id` and
    handle the None-safety. The model the query targets must have a
    `property_id` column; otherwise this is a no-op (returns the
    query unchanged so legacy un-scoped models keep working).
    """
    pid = current_property_id()
    if pid is None:
        return query

    # Inspect the target model — if it doesn't have a property_id
    # column this is a no-op (single-property mode means an unscoped
    # query is still correct).
    try:
        entity = query.column_descriptions[0]['entity']
        if not hasattr(entity, 'property_id'):
            return query
        return query.filter(entity.property_id == pid)
    except (AttributeError, IndexError, KeyError):
        return query


# ── Write helper ────────────────────────────────────────────────────

def stamp_property_id(model_obj, *, force: bool = False) -> None:
    """Set `model_obj.property_id` to the current property if unset.

    Routes that build new model instances can call this just before
    `db.session.add(...)` to ensure the row carries the active
    property. `force=True` overwrites an already-set value (rare;
    only when a route deliberately changes the property scope of a
    row, which we don't do in V1).
    """
    if not hasattr(model_obj, 'property_id'):
        return
    if force or getattr(model_obj, 'property_id', None) is None:
        model_obj.property_id = current_property_id()


# ── Reverse-resolution helpers (used by reports / inspect page) ─────

def property_member_count(prop) -> dict:
    """Return a small dict of model counts for an inspect view."""
    from ..models import (
        Room, Booking, Invoice, FolioItem, CashierTransaction,
        WhatsAppMessage, RoomType, RatePlan, BookingGroup,
    )

    pid = prop.id
    out = {}
    for label, model in [
        ('rooms',                  Room),
        ('bookings',               Booking),
        ('invoices',               Invoice),
        ('folio_items',            FolioItem),
        ('cashier_transactions',   CashierTransaction),
        ('whatsapp_messages',      WhatsAppMessage),
        ('room_types',             RoomType),
        ('rate_plans',             RatePlan),
        ('booking_groups',         BookingGroup),
    ]:
        try:
            out[label] = model.query.filter_by(property_id=pid).count()
        except Exception:
            # Column not yet present — partial migration state.
            out[label] = None
    return out
