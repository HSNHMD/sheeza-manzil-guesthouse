# Multi-Property — Migration Strategy (planning only)

> **Status:** PLANNING DOCUMENT.
> Companion to `docs/multi_property_foundation_plan.md` (the design)
> and `docs/multi_property_access_model.md` (the user/role design).
>
> **Hard rule:** every phase ships behind a feature flag and stays
> single-property-functional. We do not flip "multi-property is
> live" until every property-scoped query has been audited and
> every test passes against a two-property fixture.

The migration is **incremental and reversible**. At every phase
boundary, the running app must:

1. Pass every existing test.
2. Behave identically for end-users (single-property mode).
3. Be cleanly downgradable to the previous head.


## Phase 0 — Decisions & Pre-flight (no code)

### Goals
- Sign off on `docs/multi_property_foundation_plan.md`.
- Sign off on `docs/multi_property_access_model.md`.
- Decide naming: confirm `Property` (not `Tenant`).
- Decide URL strategy default: implicit (V1–V2) → optional
  `/p/<slug>/...` (V3+) → optional subdomain (V4+).

### Exit gate
- Plan + access model approved.
- No code touched.


## Phase 1 — Add `Property` table + seed (lowest-risk migration)

### Goals
Replace the `property_settings` singleton with a `properties` table
that has the same fields plus `slug`. Behaviour stays identical —
under the hood every read still hits row id=1.

### Migration steps
1. Create `properties` table mirroring `property_settings` schema +
   new `slug` column (`unique`, `not null`).
2. Bulk-insert one row from the existing `property_settings` row
   with `slug='default'` (or operator-supplied via env var).
3. Add `services.property_context.current_property()` helper that
   returns `Property.query.order_by(id).first()` for now.
4. Refactor `services.property_settings.get_settings()` so it returns
   a thin proxy / wrapper that always reads from the `properties`
   row. The legacy column names stay identical.
5. **Do NOT drop `property_settings` yet.** Leave it as an
   un-referenced legacy table (or rename it
   `property_settings_legacy`) for one full release. We can drop in
   a later cleanup migration once production has been on the new
   table for ≥ 30 days.
6. Tests:
   - Brand context still renders identical strings.
   - `/admin/property-settings/` still loads and saves (now writes
     through to the `properties` row).
   - Payment-instruction helper still returns the same block.

### Exit gate
- All existing tests pass.
- Staging + production single-property behaviour unchanged.
- Operator notices nothing.


## Phase 2 — Add `property_id` to top-of-graph models

### Goals
Add the FK column to the smallest-blast-radius set first. These are
the models whose `property_id` correctness drives all downstream
queries.

### Migration steps
1. Add `property_id` (FK `properties.id`, `nullable=False`,
   `server_default='1'`, `index=True`) to:
   - `rooms`
   - `room_types`
   - `bookings`
   - `booking_groups`
   - `expenses`
   - `bank_transactions`
   - `business_date_state`
2. Backfill — `UPDATE … SET property_id = 1 WHERE property_id IS NULL`
   inside the same migration (or a follow-up data migration if the
   row count is large; for our staging it's small enough to do
   inline).
3. Drop the `server_default='1'` once the column is populated. New
   rows will set `property_id` from the request context (Phase 4).
4. **Existing routes are unchanged** in this phase — they still
   compute results without filtering. Behaviour stays single-
   property because there's only one property row.
5. Tests:
   - Every model has `property_id` set on every existing row.
   - Adding a second property + creating a Booking on it does NOT
     surface in queries scoped to property #1 (regression test for
     the next phase).

### Exit gate
- All migrations green.
- Two-property fixture exists in tests, and existing tests still
  pass against it (because everything is still seeded under
  property #1).


## Phase 3 — Add `property_id` to derived / denormalized models

### Goals
Add `property_id` to models that *could* be reached via joins, but
where having a direct column makes scoping fast and reliable.

### Migration steps
1. Add `property_id` (FK, `not null`, `index`) to:
   - `folio_items`
   - `cashier_transactions`
   - `invoices`
   - `guest_orders`
   - `housekeeping_logs`
   - `room_blocks`
   - `whatsapp_messages`
   - `activity_logs` (NULL allowed for platform-level events; see
     Phase 5)
   - `night_audit_runs` (NULL allowed for platform-level events
     never apply here in practice — but we keep the option open)
2. Backfill via the natural join — for each row, look up the
   `property_id` of its parent (booking_id, room_id, etc.) and
   write it.
3. Add `nullable=False` AFTER the backfill is verified to be
   complete (one migration that backfills + a follow-up that
   tightens the constraint).
4. **`Guest` is deliberately excluded from Phase 3.** Guest
   property scoping has privacy implications (cross-property guest
   sharing) and gets its own dedicated phase later.

### Exit gate
- Every property-scoped row has a non-null `property_id`.
- Reports queries (`/reports/*`) updated to filter by
  `current_property().id`.
- Tests prove zero leakage between two seeded properties.


## Phase 4 — Wire `current_property()` into the request path

### Goals
Make every read and write property-aware. This is the phase where
single-property behaviour and multi-property behaviour become
identical at the data layer — they only differ in *which* property
`current_property()` returns.

### Migration steps
1. Implement `services.property_context`:
   - `current_property()` → returns the Property row to use for the
     active request.
   - Reads, in order:
     a. `g._property` (set by middleware below).
     b. Session key `active_property_id` if user has multi-property
        access.
     c. User's primary property membership.
     d. Default to the only Property row (single-property fallback).
2. Add a `before_request` middleware that resolves the active
   property from URL prefix → subdomain → session → user-default.
   Stashes the row on `flask.g`.
3. Refactor read-paths in batches (one blueprint at a time):
   - `routes/bookings.py` filters every list by `property_id`.
   - `routes/rooms.py`, `routes/calendar.py`, `routes/board.py`,
     `routes/reports.py`, `routes/folios.py`, `routes/cashiering.py`,
     `routes/inventory.py`, `routes/pos.py`, `routes/menu_orders.py`,
     `routes/groups.py`, `routes/housekeeping.py`,
     `routes/front_office.py`, `routes/night_audit.py` all do the
     same.
4. Refactor write-paths in batches:
   - Every `Model(...)` instantiation that creates a property-scoped
     row gets `property_id=current_property().id`.
   - Every form-validation / service helper inserts the property
     stamp before flush.
5. Add the SQLAlchemy `before_compile_select` event listener that
   raises in DEBUG when a query against a property-scoped table has
   no `property_id` predicate — this is the safety net.
6. Add an integration test suite that loads a two-property fixture
   and walks every read endpoint, asserting zero cross-property
   contamination.

### What NOT to migrate in this phase
- **Do not** add a property switcher to the UI yet. The active
  property is still implicit.
- **Do not** allow super-admin to switch contexts yet — keep that
  for Phase 6.
- **Do not** open the second property to real traffic yet — staging
  uses fixtures only.
- **Do not** touch `Guest` scoping yet.

### Exit gate
- Every existing route in the two-property fixture returns the
  correct property's data.
- The leak-detection event listener fires on zero queries during
  the full test suite.
- All tests pass.


## Phase 5 — Add `Guest.property_id` (carefully)

### Goals
Guests are sensitive. Adding `property_id` to `guests` is straight-
forward technically but has GDPR + UX implications when properties
later want to share guest profiles.

### Migration steps
1. Add `property_id` to `guests` (FK, NOT NULL, default=1, index).
2. Backfill all existing guests to property #1.
3. Update guest-related routes to scope by property.
4. Update guest-creation paths (admin form, public booking submit,
   booking engine, online menu) to set `property_id` from the
   request context.
5. **Document explicitly** in the data dictionary that
   `Guest.property_id` is V1 design and that cross-property guest
   profile sharing is a SEPARATE Phase 7+ feature with its own
   `GlobalGuestProfile` table.
6. Tests:
   - Same person booking at property A and property B creates two
     separate Guest rows.
   - Per-property `/guests/` endpoint never surfaces the other
     property's guests.

### Exit gate
- Two-property test fixtures with overlapping guest names cleanly
  isolated.
- No automatic deduplication across properties.


## Phase 6 — Access model + property switcher

### Goals
Multi-property access becomes user-visible.

### Migration steps
1. Implement `UserPropertyMembership` model (see
   `multi_property_access_model.md` for shape).
2. Implement `services.access` — `user_can_access(property)`,
   `user_role_in(property)`, `user_primary_property(user)`.
3. Add the property switcher to the admin top bar:
   - Visible only when the current user has access to ≥ 2
     properties.
   - Clicking sets `session['active_property_id']`.
   - Resolver uses session value if present.
4. Add `@super_admin_required` decorator (super-admin = a User row
   with `is_platform_admin=True`).
5. Add a fleet-rollup page at `/admin/fleet/` (super-admin only)
   that aggregates Reports KPIs across explicitly-listed properties.
6. Migration backfills `UserPropertyMembership` so every existing
   admin user has a membership in property #1 with role='admin'.

### Exit gate
- A non-super-admin user can have access to property A only and
  cannot see property B's data.
- A super-admin can view fleet rollup.
- Audit log records `actor_user_id` + `property_id` on every action.


## Phase 7 — Cross-property guest profile (Phase 7+, deliberately deferred)

This phase is mentioned only to document that it is OUT of scope for
the foundation work. Building it requires:

- Legal sign-off on cross-property data sharing under GDPR / local
  privacy law.
- A `GlobalGuestProfile` table with explicit consent flags.
- A merge / unmerge admin UI.
- Re-running every Reports query through the dedup logic.

Until that work is scoped and approved, every property has its own
`Guest` rows.


## What MUST NOT Be Migrated All At Once

These are the seven biggest "do not be tempted" items. Doing any one
of them in a single migration **will** cause a production incident.

1. **Don't migrate every model's `property_id` in one giant
   migration.** Top-of-graph first, derived second.
2. **Don't make the property switcher visible before the
   leak-detection event listener has run cleanly for ≥ 7 days on
   staging.**
3. **Don't add `property_id` AND change query patterns in the same
   migration.** The migration adds the column + backfills only.
   Routes change in a follow-up code release.
4. **Don't drop the `property_settings` table in the same release
   that introduces `properties`.** Keep it for ≥ 30 days as a
   readable legacy.
5. **Don't backfill `Guest.property_id` to anything other than #1
   in the first round.** Cross-property guest sharing is its own
   project.
6. **Don't ship the multi-property switcher without a kill switch.**
   `MULTI_PROPERTY_ENABLED=False` in env should fall back to
   single-property mode.
7. **Don't combine multi-property + channel manager + new payment
   gateway in the same release.** Each one is its own multi-week
   effort with its own incident risk.


## Risk Matrix per Phase

| Phase | Top risks | Mitigations |
|---|---|---|
| 1 | Breaking the brand-context Jinja namespace | All-keys backward-compatible; `services.branding.get_brand()` is unchanged |
| 2 | Backfill misses a row → NULL property_id with NOT NULL constraint → migration fails | Backfill BEFORE constraint; safety net constraint added in follow-up migration |
| 3 | Denormalized property_id drifts from joined property_id | Trigger or post-write check that asserts `folio_item.property_id == folio_item.booking.property_id` |
| 4 | A query missed by the audit returns cross-property data | Event listener raises in DEBUG; integration test fixture with 2 properties |
| 4 | Background job runs without property context | Every job takes `property_id` as required arg; no scope-less jobs |
| 5 | Guest dedup expectations confused | Explicit doc + UX copy: "guest profiles are per property in V1" |
| 6 | Super-admin accidentally writes to wrong property | Property switcher requires confirm-by-typed-slug for super-admin |
| 6 | UserPropertyMembership backfill misses an admin | Fail-loud: every admin user without a membership row blocks login until fixed |
| 7 (deferred) | GDPR violation through silent guest merge | Phase locked behind explicit consent UI |


## Operational Notes

- **Staging gets a second property fixture** as soon as Phase 1
  ships. Test doubles are not optional; they are the only way to
  catch leakage.
- **Production stays single-property** through Phase 5. Real-world
  promotion to multi-property is a separate decision after Phase 6
  is stable on staging for ≥ 30 days.
- **Every phase has a single owner.** Splitting phase ownership
  across multiple engineers in flight is the most common cause of
  data-isolation bugs in this kind of work.
- **Read-only / observer mode for super-admin first.** The fleet
  rollup page (Phase 6) is read-only — no super-admin can post a
  charge or cancel a booking from cross-property context. Writes
  happen in the property's own context after switching.

---

*Document owner: PMS architecture. Last updated: 2026-04-30.*
