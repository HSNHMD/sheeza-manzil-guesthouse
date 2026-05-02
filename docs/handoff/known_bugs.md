# Known bugs / open issues

> **Last updated:** 2026-05-03.
> Each entry is honest about scope. If something here is fixed, edit
> the entry — do not delete it (the audit trail matters more than
> tidiness).

## Open UI bugs

*(none currently tracked — the most recent UI bug, drag-and-drop
silently broken by a duplicate `const grid` declaration, was fixed in
commit `d7bb40e` and is now guarded by `tests/test_board_js_syntax.py`
to prevent regression)*

## Open workflow bugs

### 1. Front-office tests are flaky around midnight (local time)
- **File:** `tests/test_front_office.py`
- **Symptom:** 4 tests fail (`test_arrivals_renders_today`,
  `test_arrivals_search`, `test_departures_renders_today`,
  `test_index_shows_correct_counts`) when the local developer's OS
  calendar day has rolled over but MVT (UTC+5) hasn't.
- **Root cause:** test seeds use `date.today()` (OS-local), but the
  routes use `hotel_date()` from `app/utils.py` which is MVT-locked
  with a 3 AM rollover. The two helpers can disagree on the calendar
  day for ~3 hours per night when the developer is in GMT+8.
- **Last seen:** 2026-05-02 ~00:30 GMT+8.
- **Severity:** test-only. App runs correctly on the VPS (which is
  closer to UTC). The 4 failures self-resolve when the developer's
  local clock catches MVT.
- **Fix:** swap `today = date.today()` for `today = hotel_date()` in
  the test fixtures. One-line change, scoped to the test file.

### 2. Stay-segment rendering for multi-segment bookings hasn't been
   stress-tested in mobile breakpoints
- **File:** `app/templates/board/index.html`
- **Symptom:** None observed yet, but only single-segment stays have
  been exercised.
- **Severity:** unknown. Document the hypothesis and verify before
  promoting StaySegment usage to production.
- **Fix:** test a 3-segment stay in 1280px / 768px / 414px viewports.

## Staging-only issues

### 3. Probe scripts have left occasional orphan rows in the channel
   tables
- **Symptom:** previous probe scripts created `PROBE-*` rows that
  weren't cleaned up because the script crashed mid-run. Subsequent
  probes hit unique-constraint violations.
- **Mitigation:** the latest probes use a `cleanup` dict + `finally`
  block. Manual cleanup query if needed:
  ```sql
  DELETE FROM channel_inbound_events
   WHERE external_reservation_ref LIKE 'PROBE-%';
  DELETE FROM channel_import_exceptions
   WHERE external_reservation_ref LIKE 'PROBE-%';
  DELETE FROM bookings
   WHERE external_reservation_ref LIKE 'PROBE-%'
      OR booking_ref LIKE 'PB%';
  ```
- **Risk:** none — staging only.

## Anything partially working

### 4. Channel manager: full schema, no real network
- **State:** every model + admin page + sandbox form for booking_com
  exists. Outbound HTTP is **deliberately absent** in V1.
- **What works:** sandbox import, sandbox modify, sandbox cancel,
  exception queue, idempotency via `channel_inbound_events`.
- **What doesn't:** real OTA calls. The "test sync" button writes a
  `test_noop` ChannelSyncJob row and emits zero HTTP.
- **Next:** see `docs/handoff/next_steps.md` (sprint 2).

### 5. Multi-property: schema-deep, UI-shallow
- **State:** `property_id` columns exist on `Room`, `Booking`,
  `Invoice`, `FolioItem`, `WhatsAppMessage`, etc. (see
  `app/models.py` lines marked `Multi-Property V1`). Routes hard-code
  `services.property.current_property_id()` which always returns the
  Sheeza Manzil property id.
- **What works:** all wave-1 writes carry property_id correctly.
- **What doesn't:** there's no property switcher UI; queries don't
  scope by property; no property-bounded permissions.
- **Next:** see `docs/multi_property_foundation_plan.md` and
  `docs/multi_property_access_model.md`.

## Anything visually inconsistent

### 6. Two design vocabularies coexist
- The reservation board + premium UI uses `app/static/css/design-system.css`
  (CSS variables `--ds-*`).
- The original screens (booking detail, guest detail, invoice list)
  still use Tailwind utility classes.
- **Severity:** cosmetic. Both render correctly; the look differs
  card-to-card.
- **Fix policy:** when modifying an old screen, port to design-system
  classes; never half-port (rename ALL classes in a card or none).

### 7. Some sidebar icons use Heroicons-v1 stroke paths
- Most sidebar icons are inline `<svg>` with `viewBox="0 0 24 24"`,
  but a few rely on older Heroicons-v1 stroke paths that look slightly
  thicker than the rest.
- **Severity:** cosmetic.
- **Fix:** unified Heroicons-v2 sweep is a future polish sprint.

## Anything deferred intentionally

| Deferral | Why | Where to revisit |
|---|---|---|
| Per-department permissions on `/maintenance` | V1 keeps maintenance admin-only; per-department permissions need a model change | `docs/decisions/` (TBD ADR when planned) |
| Reservation Board open-WO badge from `svc.open_count_by_room()` | Helper exists; rail consumer doesn't | `app/services/maintenance.py:open_count_by_room` |
| Date-ranged `RoomBlock` auto-creation when marking room OOO | V1 only flips the indefinite housekeeping_status flag | `app/services/maintenance.mark_room_out_of_order` |
| Photo / attachment upload on work orders | R2 wiring needs to land on staging first | `project_sheeza_r2_gap.md` |
| Email / Slack notifications when a work order is assigned | Out of scope for V1; channels are operator-only | n/a |
| Scheduled inbound OTA poll worker | No real OTA client yet | `docs/handoff/next_steps.md` sprint 2 |
| Auto-refund on cancel_unsafe_state | PCI review required | n/a |
| No-show event handling in apply_cancellation | Out of scope for V1 | `app/services/channel_import.py` |

## How to add a bug to this file

1. Describe the symptom in 1-2 sentences.
2. Pin the file path or commit hash where the issue lives.
3. State severity honestly (UI-only, workflow, data-loss).
4. Suggest a fix or link to the next sprint that addresses it.
5. Never delete an entry — strike it through and add a `**Fixed:**`
   line below.
