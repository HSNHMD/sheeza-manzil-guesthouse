"""Audit / activity log helper.

Single sanctioned writer for `app.models.ActivityLog`. All logging in the
codebase MUST go through `log_activity()`; direct `ActivityLog(...)`
construction in routes is a code-review red flag.

Design contract (binding):

1. **Append-only.** Never UPDATE or DELETE an ActivityLog row. There is
   no helper for it and no route exposes one.

2. **Atomic with the caller's mutation.** This helper writes to the
   current `db.session` but does NOT commit. The caller's existing
   `db.session.commit()` flushes the mutation and the audit row in one
   transaction. If the caller rolls back, the audit row rolls back too.

3. **Best-effort.** A failure inside this helper must NEVER break the
   user-visible action. We catch every exception, log a safe warning to
   `app.logger`, and return None. Routes can call it without try/except.

4. **Privacy by design.** The helper:
     - rejects metadata keys whose name contains any banned substring
       (`password`, `token`, `secret`, `api_key`, `key`, `credential`)
     - allows scalar values only (str/int/float/bool/None) in metadata
     - truncates `description` to 500 chars
     - truncates each metadata value to 200 chars
     - truncates `old_value` / `new_value` to 64 chars
   Callers must not pass passport numbers, slip bytes, WhatsApp message
   bodies, or other personally-sensitive content. References to bookings
   and invoices should use `booking_ref` and `invoice_number`, not PII.

5. **Actor inference.** If `actor_type` is omitted the helper derives:
     - `current_user` is an authenticated admin/staff → 'admin' (with id)
     - `current_user` is anonymous (e.g. guest portal) → 'guest'
     - no Flask request context at all → 'system'

The helper does not import models at module load — it imports inside the
function so this module can be unit-tested in isolation.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional


# Substrings that disqualify a metadata key. Matched case-insensitively
# against the lowercased key name. If this list grows, consider compiling
# a precomputed regex once.
_BANNED_KEY_SUBSTRINGS = (
    'password',
    'token',
    'secret',
    'api_key',
    'apikey',
    'credential',
    # 'key' is intentionally last — broad catch-all that also covers
    # things like 'access_key', 'private_key'.
    'key',
)

_DESCRIPTION_MAX_LEN = 500
_METADATA_VALUE_MAX_LEN = 200
_OLD_NEW_MAX_LEN = 64
_METADATA_JSON_MAX_LEN = 4000   # hard cap on serialized metadata blob

# Whitelist of actor_type values — anything else is normalized to 'system'.
_VALID_ACTOR_TYPES = frozenset(('guest', 'admin', 'system', 'ai_agent'))

# Strict whitespace/identifier regex for the action string. We don't enforce
# this hard (would defeat the swallow-and-warn rule), but we use it to flag
# obviously-malformed action labels in the warning path.
_ACTION_SHAPE = re.compile(r'^[a-z][a-z0-9_.]{0,62}[a-z0-9]$')


def _is_banned_key(key: str) -> bool:
    """Return True if `key` (case-insensitively) contains a banned substring."""
    if not isinstance(key, str):
        return True
    lowered = key.lower()
    return any(banned in lowered for banned in _BANNED_KEY_SUBSTRINGS)


def _coerce_scalar(value: Any) -> Any:
    """Allow only JSON-safe scalars, truncating strings.

    Returns the cleaned value, or the literal string `'<dropped>'` for any
    non-scalar (lists, dicts, bytes, custom objects). We never try to
    recursively clean nested structures — flat metadata only.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > _METADATA_VALUE_MAX_LEN:
            return value[:_METADATA_VALUE_MAX_LEN] + '…'
        return value
    return '<dropped>'


def sanitize_metadata(meta: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """Return a sanitized shallow copy of `meta`, or None if empty.

    Drops banned keys, coerces values to JSON-safe scalars, truncates strings.
    Does not recurse — nested dicts/lists are replaced with the literal
    `'<dropped>'`. This is intentional: structured payloads belong in their
    own typed columns, not in audit metadata.
    """
    if not meta:
        return None
    cleaned: dict = {}
    for raw_key, raw_value in meta.items():
        if _is_banned_key(raw_key):
            continue
        # Stringify keys for safety; SQLAlchemy/JSON both want str keys.
        key = str(raw_key)[:64]
        cleaned[key] = _coerce_scalar(raw_value)
    return cleaned or None


def _truncate(value: Optional[str], max_len: int) -> Optional[str]:
    if value is None:
        return None
    s = str(value)
    if len(s) <= max_len:
        return s
    return s[:max_len]


def _infer_actor() -> tuple:
    """Return (actor_type, actor_user_id) from the current request context.

    Uses lazy imports because this module is also imported in unit tests
    that have no app context.
    """
    try:
        from flask_login import current_user  # type: ignore
        if current_user and getattr(current_user, 'is_authenticated', False):
            return ('admin', int(getattr(current_user, 'id', 0)) or None)
        return ('guest', None)
    except Exception:
        return ('system', None)


def _request_metadata():
    """Return (ip_address, user_agent) from the current Flask request, if any."""
    try:
        from flask import request  # type: ignore
        if request is None:
            return (None, None)
        # request.remote_addr respects ProxyFix if it's been installed; if
        # not, we fall back to the first X-Forwarded-For entry.
        ip = (
            request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
            or request.remote_addr
        )
        ua = request.headers.get('User-Agent', '')
        return (
            _truncate(ip, 45),
            _truncate(ua, 255),
        )
    except Exception:
        return (None, None)


def log_activity(
    action: str,
    *,
    actor_type: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    booking=None,
    invoice=None,
    booking_id: Optional[int] = None,
    invoice_id: Optional[int] = None,
    old_value: Optional[str] = None,
    new_value: Optional[str] = None,
    description: str = '',
    metadata: Optional[Mapping[str, Any]] = None,
    capture_request: bool = True,
):
    """Append one row to the activity_logs table within the current session.

    Returns the created `ActivityLog` instance on success, or `None` on
    failure (failure is logged but never re-raised).

    Args:
        action: short canonical event identifier, e.g. 'booking.created'.
        actor_type: 'guest' | 'admin' | 'system' | 'ai_agent'. If omitted,
            inferred from the current Flask-Login session.
        actor_user_id: explicit override (otherwise inferred).
        booking / invoice: pass the ORM object — the helper will resolve
            `.id` (saving the caller from booking.id when booking might
            be None).
        booking_id / invoice_id: explicit overrides if no ORM object is
            handy (e.g. after a DELETE).
        old_value / new_value: short before/after labels (≤64 chars each).
            Use for status transitions like ('pending_payment' → 'confirmed').
        description: human-readable sentence (≤500 chars). NEVER include
            secrets, full passport numbers, slip bytes, or message bodies.
        metadata: small flat dict of scalar values; banned keys are
            stripped (see `_BANNED_KEY_SUBSTRINGS`).
        capture_request: if True (default) the helper will pull
            ip_address + user_agent from the current Flask request, when
            one exists. Pass False from CLI/system code paths.

    The helper never commits. The caller is responsible for committing.
    """
    # Lazy imports so this module is import-safe in test code that does
    # not have the Flask app context wired up yet.
    try:
        from flask import current_app
        from ..models import db, ActivityLog
    except Exception:  # pragma: no cover - import error path
        return None

    try:
        # Resolve actor.
        if actor_type is None:
            actor_type, inferred_id = _infer_actor()
            if actor_user_id is None:
                actor_user_id = inferred_id
        if actor_type not in _VALID_ACTOR_TYPES:
            actor_type = 'system'

        # Resolve booking_id / invoice_id from ORM objects when given.
        if booking is not None and booking_id is None:
            booking_id = getattr(booking, 'id', None)
        if invoice is not None and invoice_id is None:
            invoice_id = getattr(invoice, 'id', None)

        # Privacy + length scrubbing.
        cleaned_meta = sanitize_metadata(metadata)
        if cleaned_meta is not None:
            try:
                meta_blob = json.dumps(cleaned_meta, default=str,
                                       ensure_ascii=False, sort_keys=True)
            except Exception:
                meta_blob = None
            if meta_blob and len(meta_blob) > _METADATA_JSON_MAX_LEN:
                meta_blob = meta_blob[:_METADATA_JSON_MAX_LEN]
        else:
            meta_blob = None

        clean_description = _truncate(description or '', _DESCRIPTION_MAX_LEN) or ''
        clean_old = _truncate(old_value, _OLD_NEW_MAX_LEN)
        clean_new = _truncate(new_value, _OLD_NEW_MAX_LEN)
        clean_action = _truncate(action, 64) or 'unknown'

        if not _ACTION_SHAPE.match(clean_action):
            # Don't reject — just warn. Some actions are programmatic.
            try:
                current_app.logger.debug(
                    'audit: non-canonical action label %r', clean_action,
                )
            except Exception:
                pass

        ip = ua = None
        if capture_request:
            ip, ua = _request_metadata()

        row = ActivityLog(
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            booking_id=booking_id,
            invoice_id=invoice_id,
            action=clean_action,
            old_value=clean_old,
            new_value=clean_new,
            description=clean_description,
            metadata_json=meta_blob,
            ip_address=ip,
            user_agent=ua,
        )
        # Use the caller's session — DO NOT commit here.
        db.session.add(row)
        return row
    except Exception as exc:  # pragma: no cover - swallow path
        # Never break the user-visible action. Log and move on.
        try:
            current_app.logger.warning(
                'audit: log_activity(%r) suppressed exception: %s',
                action, exc,
            )
        except Exception:
            pass
        return None
