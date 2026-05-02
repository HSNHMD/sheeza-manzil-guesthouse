# Multi-Property Foundation — Plan (planning only)

> **Status:** PLANNING DOCUMENT.
> No code, no migrations, no production changes.
>
> **Purpose:** define the future `Property` entity, audit which existing
> models need to be scoped by property, and decide what stays global.
> Companion to `docs/multi_property_migration_strategy.md` (the
> phased rollout) and `docs/multi_property_access_model.md` (the
> user/role design).
>
> **Hard rule:** the platform must NEVER leak data across properties.
> Every multi-property design choice in this doc is evaluated against
> that rule first; ergonomics second.


## 1. The future `Property` model

`PropertySettings` (introduced in migration `0c5e7f3b842a`) is a
**singleton** — one row per environment, no `property_id` anywhere
else. The future `Property` entity is the **same shape**, but
becomes:

1. **Plural.** Many rows; one per real-world property.
2. **The owner of all property-scoped tables.** Every record below
   that needs property scoping carries a `property_id` FK pointing
   at this table.

### Field design

The migration that introduces `Property` should keep the V1 fields
of `PropertySettings` as-is, plus a few additions:

```
Property
├─ id                            (PK)
├─ created_at, updated_at
├─ slug                          (unique, lowercase, URL-safe — used
│                                 in /admin/property/<slug>/* later
│                                 and in subdomain routing if ever
│                                 done; keep simple chars only)
├─ legal_name                    (full legal entity name; can differ
│                                 from display name)
├─ is_active                     (soft-disable a property without
│                                 dropping its data)
│
├─ ── Branding (verbatim from PropertySettings) ─
├─ property_name, short_name, tagline, logo_path,
├─ primary_color, website_url
│
├─ ── Contact ─
├─ email, phone, whatsapp_number,
├─ address, city, country
│
├─ ── Operational ─
├─ currency_code, timezone,
├─ check_in_time, check_out_time
│
├─ ── Billing ─
├─ invoice_display_name, payment_instructions_text,
├─ bank_name, bank_account_name, bank_account_number
│
├─ ── Tax basics (will be replaced by structured TaxRule per
│      Channel Manager Phase 1) ─
├─ tax_name, tax_rate, service_charge_rate
│
├─ ── Policies ─
├─ booking_terms, cancellation_policy, wifi_info
│
└─ metadata_json
```

The migration that does the rename / promotion (`PropertySettings →
Property`, plus a unique `slug` column) is the cleanest place to
insert `slug`. **Do not** retroactively try to mint slugs from
existing booking data.

### Naming choice: `Property` vs `Tenant`

Use `Property`. Reasons:

- The platform serves one real-world property per row — not a tenant
  in the usual SaaS sense.
- Operators understand "property"; they don't think in tenants.
- "Tenant" carries SaaS-isolation connotations that we are NOT yet
  delivering at the platform level (one DB per environment, no
  schema-per-tenant, no row-level-security in PostgreSQL).
- If we ever add agency / chain features (one operator running 12
  properties), we extend with a `PropertyGroup` row sitting above
  `Property`.


## 2. Ownership Audit (current models, 2026-04-30)

For every existing model, this section answers four questions:

- **Needs `property_id`?** Will the row be meaningless without
  knowing which property it belongs to?
- **Stay global?** Is the row inherently cross-property (system
  config, audit log, …)?
- **Shared optionally?** Should one row appear in multiple properties
  with explicit consent (e.g. a guest staying at two of the chain's
  hotels)?
- **Migration risk** of adding the column + backfilling.

| Model | property_id? | Global? | Shareable? | Risk | Notes |
|---|---|---|---|---|---|
| `User` | optional FK + via `UserPropertyMembership` | YES (the row itself stays global) | YES | LOW | The user identity is global; their **access** is per-property. See `multi_property_access_model.md`. |
| `Room` | **YES (required)** | no | no | LOW | A room belongs to exactly one property. Backfill = "all current rooms → property #1". |
| `RoomType` | **YES (required)** | no | no | LOW | Same as Room — types are property-specific. |
| `RatePlan` | **YES (required)** | no | no | MEDIUM | `RatePlan.code` is currently globally unique. Must change to unique-within-property `(property_id, code)` composite. |
| `RateOverride` | **YES (required)** | no | no | LOW | Inherits from RoomType FK. |
| `RateRestriction` | **YES (required)** | no | no | LOW | Inherits from RoomType FK. |
| `RoomBlock` | **YES (required)** | no | no | LOW | Inherits from Room FK. |
| `Guest` | **YES (required for V1)** | no | YES later | MEDIUM | V1: guest is property-local. Later we may add a `global_guest_id` for multi-property guests with explicit re-confirmation. **Cross-property guest sharing is the single largest privacy risk** — treat as Phase 7+. |
| `Booking` | **YES (required)** | no | no | MEDIUM | Backfill straightforward. The booking_ref namespace currently looks global (e.g. `BK-12345`); we should keep that **global unique** for human reference, but store `property_id` for filtering. |
| `Invoice` | **YES (required)** | no | no | MEDIUM | Inherits from Booking; `invoice_number` should become unique within property. |
| `FolioItem` | **YES (required, denormalized)** | no | no | LOW | Inherits from `booking_id`, but **also** carry `property_id` directly so reports can scope without joining Booking. |
| `CashierTransaction` | **YES (required, denormalized)** | no | no | LOW | Same logic as FolioItem. Booking_id is nullable; property_id makes property scoping reliable. |
| `Expense` | **YES (required)** | no | no | LOW | Property-specific by definition. |
| `BankTransaction` | **YES (required)** | no | no | LOW | Bank statement reconciliation is per-property. |
| `HousekeepingLog` | **YES (required)** | no | no | LOW | Inherits from Room FK. |
| `WhatsAppMessage` | **YES (required, denormalized)** | no | no | MEDIUM | Inbound messages need to route to the correct property's inbox. The phone number that received the message identifies the property — see Phase 4 below. |
| `RoomType` / inventory tables | covered above | — | — | — | — |
| `PosCategory`, `PosItem` | **YES (required)** | no | no | LOW | Each property has its own menu. |
| `GuestOrder` | **YES (required, denormalized)** | no | no | LOW | Inherits from Booking when linked; standalone (unlinked) orders need a direct `property_id` so they can't leak between properties. |
| `GuestOrderItem` | inherits via `order_id` | no | no | LOW | No direct property_id needed; query through GuestOrder. |
| `BookingGroup` | **YES (required)** | no | no | LOW | Group must belong to one property — no cross-property group bookings in V1. |
| `RoomBlock` | covered above | — | — | — | — |
| `BusinessDateState` | **YES (one row per property)** | no | no | LOW | Today it's a singleton. Becomes "one BusinessDateState per Property" — i.e., a row per property. |
| `NightAuditRun` | **YES (required)** | no | no | LOW | Inherits from BusinessDateState scope. |
| `ActivityLog` | **YES (required, denormalized)** | semi-global | no | MEDIUM | Audit rows MUST carry `property_id` so per-property activity views work. Some rows (super-admin actions, settings changes) may have NULL property_id — represent that as "platform-level". |
| `PropertySettings` | **REPLACED** by Property | n/a | — | n/a | Phase 1 of the migration converts PropertySettings → Property and copies the singleton row to row #1. |
| **Communication templates** (future) | YES (with optional global default) | partially | YES | TBD | Each property may override the platform-default template, but operators on chain-wide deployments will want the base copy shared. Out of scope until CRM ships. |

### What stays global (forever)

| Model | Reason |
|---|---|
| **`User`** (the row) | A human is a human. Their access scope is per-property; the row stays global. |
| **`Property`** itself | Obviously. |
| **Future `PropertyGroup`** | Chain / brand level (deferred). |
| **Future `TaxRule` master catalog** | Maldives GST is the same across all Maldives properties; consider a global rule table with per-property opt-in flags. |
| **Future `CountryCurrency` / `Timezone` reference data** | Read-only, never property-specific. |
| **Future `OTAChannel` master catalog** | "Booking.com" is the same channel everywhere. The per-property mapping (`ChannelConnection`) belongs to the property. |


## 3. Shared vs Isolated Data

### Property-isolated (default for V1 → V3)

Everything operational. Every booking, every folio item, every cash
transaction, every menu item, every report number. **The default
mindset must be "isolated unless proven otherwise"** — leakage is the
risk, sharing is the convenience.

### Platform-global

| Item | Why |
|---|---|
| `User` rows | One identity, many access scopes. |
| `Property` table | Defines the universe. |
| `ActivityLog` super-admin actions | Property-NULL rows for cross-cutting events (e.g. "platform admin enabled feature X"). |
| Reference data (currencies, timezones, country codes) | Static. |
| Future channel manager master catalogue | The OTA itself is global; the connection is per-property. |

### Shared optionally (Phase 7+, deliberately deferred)

| Item | Constraint |
|---|---|
| `Guest` records | Cross-property guest profiles need explicit consent, GDPR-aware re-confirmation, and a clear UX for "I have stayed at two of your hotels." V1 keeps guest property-local. |
| Communication templates | Property may override; default may be platform-level. |
| Document templates (T&Cs, cancellation policy boilerplate) | Same logic — global default, per-property override. |
| OTA credentials | Should NEVER be shared. Each property has its own OTA contracts. |

The single highest-risk sharing-conversation is **guests**. Don't
over-engineer it. V1 design: `Guest.property_id` is required.
A future "chain customer" feature can add a `GlobalGuestProfile` row
that points at the per-property `Guest` rows by consent.


## 4. Property Resolution: How Every Request Knows the Active Property

For V1 / V2 of multi-property foundation, **the property is implicit**:
exactly one active property exists per environment; every request
operates on it. This stays compatible with the current behaviour.

For V3+, when multiple properties exist, the request resolves the
active property in this priority:

1. **Explicit URL prefix** — `/p/<slug>/...`. The cleanest answer for
   multi-property admins.
2. **Subdomain / hostname** — `sheeza.husn.cloud` vs
   `paradise.husn.cloud`. Keeps URLs short for staff who only ever
   touch one property.
3. **Session-stored "active property"** for users with access to
   multiple properties. Settable via a top-bar property picker.
4. **User's primary property** (one of their `UserPropertyMembership`
   rows flagged `is_primary=True`).

The resolver lives in a single function — proposed name
`services.property_context.current_property()` — and is the **only**
way request handlers learn which property they are operating in.
Direct `Property.query.filter_by(...)` calls are forbidden in route
handlers.


## 5. Anti-Leak Architecture (the most important section)

### 5.1 Query scoping rule

Every query that reads property-scoped data MUST filter by
`property_id`. Three enforcement mechanisms layered together:

1. **Service-layer wrappers**. New `app/services/scoped_query.py`
   provides `for_property(model)` that returns a query already
   filtered. Routes call those wrappers, not raw `Model.query`.
2. **SQLAlchemy event listeners**. A `before_compile_select` hook
   inspects every emitted SELECT statement; if the target table has
   a `property_id` column AND the query lacks a `property_id =
   :pid` predicate, it raises in DEBUG / staging.
3. **Test enforcement**. A test suite that loads two seeded
   properties and runs every read endpoint, asserting the response
   contains zero rows from the wrong property.

The first mechanism is the developer ergonomic. The second is the
runtime safety net. The third is the proof.

### 5.2 Write scoping rule

Every INSERT into a property-scoped table MUST set `property_id`
to the current request's property. Enforced via:

1. Default value bound to `current_property().id` on the model when
   the column is added (lambda default).
2. `nullable=False` constraint on `property_id` for every
   property-scoped table — the DB itself rejects unscoped writes.
3. A second event listener on `before_insert` that double-checks the
   row's `property_id` matches the request context.

### 5.3 What about background jobs?

Night Audit, channel sync workers, scheduled reports — they don't
have a request context. Solution: every job carries an explicit
`property_id` parameter. Jobs that span multiple properties (e.g.
"close all properties at midnight") are explicitly per-property
loops, not "scope-less" queries.

### 5.4 What about admin reports?

A super-admin viewing a "fleet rollup" report SELECTs across
properties on purpose. That endpoint is the **only** place where
unscoped reads are allowed, and it's gated:

- `@super_admin_required` decorator.
- A specific keyword argument `properties=` listing exactly which
  property IDs are aggregated.
- The query never hits raw `Booking.query.all()` — it goes through
  `services.fleet_reports.aggregate_for(properties)`.

If `properties=None`, the call raises immediately. No "default to
all" behaviour anywhere.


## 6. Recommended Minimal Implementation After This Plan

The smallest safe step that moves us toward multi-property without
breaking single-property behaviour:

### Step 1 (next sprint, code allowed): "Property table + slug"

1. Migration that:
   - Creates a `properties` table identical to `property_settings`
     plus `slug` (unique, indexed).
   - Copies the single `property_settings` row into `properties` as
     id=1, slug='default'.
   - Leaves `property_settings` table in place for one release as a
     **read-only legacy view** of properties[id=1].
2. `services.property_context.current_property()` helper that
   returns `Property.query.get(1)` for now (single-property).
3. Update `services.property_settings.get_settings()` to call
   `current_property()` under the hood. Same return shape.
4. Tests that prove the brand context, payment instructions, and
   admin form all still work unchanged.
5. **No `property_id` columns added anywhere else yet.**

This step is **invisible to the operator** — every page still works
identically. It just gives us the table to point at next.

### Step 2 (sprint after): "property_id on top-of-graph models"

1. Migration adds `property_id` (NOT NULL, FK, default=1) to:
   - `Room`
   - `RoomType`
   - `Booking`
   - `BookingGroup`
   - `Expense`
   - `BankTransaction`
   - `BusinessDateState`
2. Backfill all existing rows with `property_id = 1`.
3. Wire `current_property()` into routes — every read filters by
   `property_id`, every write defaults to the current property.

Models lower in the graph (FolioItem, CashierTransaction, Invoice,
GuestOrder, ActivityLog, etc.) get their own column **denormalized
for query speed** in a later step. They don't strictly need it for
correctness — they inherit through joins — but reports / inboxes /
audit pages benefit hugely from a direct column.

### Step 3+: see migration strategy doc.


## 7. What This Plan Does NOT Decide

Out of scope; deliberately deferred to later planning rounds:

- **Cross-property guest profile sharing.** GDPR-sensitive; needs
  legal sign-off before a single line of code is written.
- **Channel manager OTA mappings per property.** Already covered in
  `docs/channel_manager_architecture.md`. Multi-property compounds
  the complexity; treat as a Channel Manager Phase 6+ topic.
- **AI agent context.** When an LLM-driven assistant fetches "today's
  arrivals" — which property does it run for? Phase 5 of the access
  model doc covers this; the short answer is "the user's currently-
  selected property; super-admin must specify."
- **Per-property storage in R2.** Right now uploads are flat. A
  future migration to per-property prefixes (`uploads/<slug>/...`) is
  desirable; not blocking.
- **Per-property domain / SSL.** Could be done via DNS + nginx +
  separate certs; orthogonal to the data layer.
- **Pricing.** A multi-property platform usually means platform-level
  billing for the SaaS itself. Out of scope here.


## 8. Summary Table: What Changes vs Stays the Same

| Area | Single-property today | Multi-property foundation (after migration phases) |
|---|---|---|
| Settings access | `property_settings` singleton | `Property` table, current row resolved per request |
| Models scope | flat | every operational model gains `property_id` |
| Routes | global | property-scoped via `current_property()` |
| User auth | role-based (admin / staff) | role + `UserPropertyMembership` |
| URL shape | flat | unchanged for V1; later optional `/p/<slug>/...` |
| Reports | property-implicit | property-explicit, super-admin can roll up |
| Audit trail | global | mostly property-scoped, with NULL property_id for platform events |
| Background jobs | global | per-property loops; explicit `property_id` parameter |

---

*Document owner: PMS architecture. Last updated: 2026-04-30.*
