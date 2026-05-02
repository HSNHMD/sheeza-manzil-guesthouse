# 0001 — Reservation Board as front-office spine

**Status:** Accepted (2026-Q1)

## Context

Front-office operators were juggling three views to do their daily
work:
- a list of **bookings** (filterable, but you couldn't see the room
  layout);
- a **calendar** (good for forward planning, painful for tactical
  moves);
- a **rooms list** (good for housekeeping, blind to bookings).

Three views meant operators kept two browser tabs open and
mentally joined the data. Mistakes (double-booking, missed-clean,
walk-in-dropped-on-OOO-room) were easy.

Other PMS products solve this with a single "tape chart" view —
horizontal axis = days, vertical axis = rooms, bookings as bars.
The operator drags a booking onto a room and the system enforces
the constraints.

## Decision

The **Reservation Board** at `/board/` is the primary front-office
work surface. Bookings, rooms, room blocks, stay segments, and
maintenance state all render in one fluid grid. Operators do moves
by drag-and-drop; the board calls `services/board_actions.py` which
runs conflict checks before persisting.

The Reservation Board is the operational calendar. The legacy
calendar route still exists (`/calendar/`) but is hidden from the
sidebar by the IA-cleanup sprint — it stays around so old bookmarks
keep resolving.

## Consequences

**Easier:**
- One canvas for all front-office work — drag, drop, refresh, done.
- Conflict checks happen in one service (`check_booking_room_move_conflict`)
  — RoomBlocks, OOO state, stay segments all factor in.
- Mid-stay room change rendering has a single consumer to update.
- Staff training is one screen instead of three.

**Harder:**
- The board's JS file (~1500 lines, IIFE-wrapped vanilla JS) is now
  a high-leverage surface — every visual change has to ship without
  regressing drag/drop. We added `tests/test_board_js_syntax.py` as
  a static guard after a duplicate-`const` bug bricked drag/drop
  silently in commit `d7bb40e`'s parent.
- Long-range planning (3+ months out) is still better in the
  calendar view; the board is week-scale.

## Alternatives considered

- **Keep the three views, improve each.** Rejected — the cognitive
  load of joining them mentally was the actual problem.
- **Use a third-party widget (FullCalendar / Bryntum).** Rejected —
  rate-tier costs, vendor lock-in, accessibility unknown, and we'd
  still own the conflict-check logic.
