# 0006 — Property-aware schema before multi-property UI

**Status:** Accepted (2026-Q1)

## Context

The roadmap includes a small chain of properties — currently one
guesthouse, with at least one more under consideration. Multi-tenant
PMS work is famous for two failure modes:
1. **Schema retrofit later.** You ship single-tenant, then "just add
   a property_id everywhere" turns into a months-long migration with
   data backfills, query audits, and broken assumptions.
2. **Premature abstraction.** You build a multi-property UI on day
   one, ship a half-finished property switcher, and confuse
   operators who only have one property.

We want neither.

## Decision

Land the **property-aware schema first**, in waves, **without
shipping a property switcher UI**.

Wave 1 (already shipped to staging): every model that owns business
data carries a `property_id` column with a server default that
points at the singleton property. New writes populate the column
correctly via `services.property.current_property_id()`. Models
covered include: `Room`, `Booking`, `Invoice`, `FolioItem`,
`Guest`, `WhatsAppMessage`, `WorkOrder` (via `room.property_id`),
`ChannelConnection`, `Property`.

Wave 2 (deferred): a per-route audit to scope queries by property,
plus a property switcher for admins. Estimated 1 sprint, gated on
the foundation depth audit (`docs/multi_property_foundation_plan.md`).

Wave 3 (deferred): per-property branding, per-property channel
connections (the schema already supports this — see
`ChannelConnection.property_id` UNIQUE constraint).

## Consequences

**Easier:**
- Adding a second property later is a data load + branding entry,
  not a schema migration with backfills.
- Channel connections are already scoped per property —
  Booking.com sandbox for property A doesn't collide with property B.
- The "we'll do multi-property someday" design pressure was
  resolved without paying the UI cost upfront.

**Harder:**
- Today, every route hard-codes
  `services.property.current_property_id()`. A reader could
  reasonably wonder "is this single-tenant or multi-tenant?" The
  answer is "schema-multi, UI-single."
- The `property_id` columns on wave-1 models have **no Python
  default** — they require explicit assignment. This is intentional
  (catches missing assignments at write time) but new code must
  remember to set them.
- Tests must seed a `Property` row before they can write any
  wave-1 row. Most tests do; some legacy tests rely on the
  server-default behavior.

## Alternatives considered

- **Stay single-tenant. Refactor when needed.** Rejected — see
  Context failure mode #1.
- **Build the full multi-property switcher in V1.** Rejected — see
  Context failure mode #2.
- **Use a row-level security (RLS) library.** Rejected — adds
  framework dependency for one tenant; revisit when wave 2 lands.
