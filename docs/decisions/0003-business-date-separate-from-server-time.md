# 0003 — Business date separate from server time

**Status:** Accepted (2026-Q1)

## Context

Hotel operations don't run on the calendar day. A walk-in arriving
at 02:00 is still part of yesterday's shift. Night audit closes the
books for "yesterday" between 23:00 and 03:00 the next morning.
Reports for "today" mean the operations day, not the wall-clock day.

Using `date.today()` everywhere meant:
- Reports flickered between "yesterday" and "today" depending on
  when the operator ran them.
- Walk-ins after midnight got the wrong check-in date.
- Night Audit had no way to say "I am closing day X" — every query
  computed today on the fly.

## Decision

Introduce **`BusinessDateState`** as a singleton DB row. The
**business date** is the authoritative "operations today" — it
advances only when Night Audit explicitly says so.

Three helpers:
- `current_business_date()` (`app/services/night_audit.py`) returns
  the date. Falls back to `date.today()` with a logger warning if
  the singleton row is missing — surfaces as a Night Audit blocker.
- `hotel_date()` (`app/utils.py`) returns the same date with a 3 AM
  MVT rollover for surfaces that haven't migrated to
  `current_business_date()` yet. Convenience wrapper for legacy code.
- A Jinja context processor injects `{{ business_date }}` into every
  template.

## Consequences

**Easier:**
- "Today's arrivals" / "today's departures" / "today's revenue" all
  agree, regardless of when the operator runs them.
- Night Audit becomes a real workflow — close the day, advance the
  date, generate the close-of-day report.
- Multi-property is unblocked: each property can have its own
  business date if they're in different time zones (foundation only;
  current code uses a single row).

**Harder:**
- Test fixtures need to either (a) seed `BusinessDateState` or (b)
  use `hotel_date()` instead of `date.today()`. Today, 4 tests in
  `tests/test_front_office.py` use `date.today()` and flake when
  local OS calendar day is ahead of MVT calendar day. See
  `docs/handoff/known_bugs.md`.
- New code MUST consciously choose between `current_business_date()`
  (for reports / posting / billing) and `date.today()` (for
  audit-trail timestamps).

## Alternatives considered

- **Use `date.today()` everywhere.** Rejected — see Context.
- **Compute "operations today" per-request from configurable
  rollover hour.** Rejected — Night Audit becomes a UI-only
  ceremony with no persistence; can't represent "the books are
  closed" as state.
