"""Pepper transactional outbox — emit helper (Phase 0).

`emit()` adds a PepperOutbox row to the CURRENT db.session WITHOUT committing, so
it flushes atomically with the caller's booking/slip insert (same idiom as
services.audit.log_activity). Never let the bot open its own transaction around a
service call that already self-commits — pin the emit to that service's commit.

Best-effort: a failure to serialise the payload must never break the booking, so
emit swallows its own errors (the booking is the product; a missing alert row is
recoverable, a failed booking is not).
"""

from __future__ import annotations

import json


def emit(event_type, *, booking_id=None, reference=None, payload=None):
    """Queue an outbox event on the current transaction. Returns the (unflushed)
    PepperOutbox instance, or None if it could not be built."""
    from ..models import db, PepperOutbox
    try:
        row = PepperOutbox(
            event_type=event_type,
            booking_id=booking_id,
            reference=reference,
            payload_json=json.dumps(payload, default=str) if payload else None,
        )
        db.session.add(row)
        return row
    except Exception:  # noqa: BLE001 — never break the booking for an alert row
        return None
