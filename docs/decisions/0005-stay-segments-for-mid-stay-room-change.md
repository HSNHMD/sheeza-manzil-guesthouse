# 0005 — Stay segments for mid-stay room change

**Status:** Accepted (2026-Q1)

## Context

A guest may need to move rooms partway through their stay (AC
broken, upgrade, family rejoined). The PMS needs to represent this
without breaking:
- the operator's mental model ("this is one guest, one bill"),
- the folio (one set of charges, one invoice),
- the reservation board (the bar should span both rooms),
- the WhatsApp thread / activity log (one history),
- reporting (revenue counts once).

## Decision

Model a stay as **one `Booking` + N `StaySegment` rows**.

- `Booking` remains the single source of truth for guest, total
  dates, folio, payments, history.
- `StaySegment` rows describe which physical room hosts the guest
  for which date sub-range. Half-open intervals matching the
  `Booking.check_in_date` / `check_out_date` convention.
- A booking with no segments renders by `booking.room_id`
  (everything works as before — additive change).
- A booking with segments renders by walking
  `booking.stay_segments` ordered by `start_date`.

The board renderer in `app/services/board.py` uses
`Booking.has_segments` to choose between the two paths.

## Consequences

**Easier:**
- One booking still means one folio, one invoice, one bill.
- Reporting "occupancy by room-night" is a join across segments;
  reporting "revenue by booking" is unchanged.
- The OTA modification handler (`apply_modification`) can swap
  rooms without creating a sibling booking — it just rewrites the
  `room_id` on the appropriate segment (or the whole booking if
  there are no segments).

**Harder:**
- Renderers must be segment-aware in the long run. V1 ships the
  foundation + the board renderer; many secondary screens
  (housekeeping board, guest detail, etc.) still render by
  `booking.room_id`. We deliberately leave those unchanged
  pending stress-testing.
- Mid-stay move UX (the actual operator gesture to split a stay
  in two) is deferred to a later sprint.
- Multi-segment renderers in mobile breakpoints are untested. See
  `docs/handoff/known_bugs.md` entry #2.

## Alternatives considered

- **Two sibling bookings (one per room).** Rejected — breaks the
  one-folio-one-bill model, doubles the activity log, and forces
  every consumer to know about the relationship.
- **A new `BookingMove` event row.** Rejected — captures the
  history but doesn't help renderers (they still need to compute
  current room from a sequence of events on every read).
