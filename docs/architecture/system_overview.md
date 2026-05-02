# System overview

> **Last updated:** 2026-05-03.
> Honest snapshot of what the platform is today, with the boundaries
> of what's deployed where called out explicitly.

## What this is

A single-repo Flask 2.x property-management system (PMS) for a small
guesthouse, evolving toward a small chain. One Python codebase, one
SQLAlchemy schema, deployed twice: production (small subset of
features) and staging (everything in flight).

## High-level shape

```
                                    ┌──────────────────────────┐
                                    │   Internet / operators   │
                                    └────────────┬─────────────┘
                                                 │
                                ┌────────────────┴─────────────────┐
                                │                                  │
                       https://sheezamanzil.com         https://staging.husn.cloud
                                │                                  │
                          ┌─────▼─────┐                       ┌────▼─────┐
                          │  nginx    │                       │  nginx   │
                          │ + Certbot │                       │+ Certbot │
                          └─────┬─────┘                       └────┬─────┘
                                │                                  │
                          gunicorn :prod                     gunicorn :staging
                                │                                  │
                       sheeza.service                  guesthouse-staging.service
                                │                                  │
                          /var/www/sheeza-manzil       /var/www/guesthouse-staging
                          (branch: main)               (branch: feature/reservation-board)
                                │                                  │
                                └──────► Flask app ◄──────────────┘
                                              │
                                              ▼
                                ┌─────────────────────────┐
                                │   Postgres (per-env)    │
                                │   uploads/ (per-env)    │
                                │   Cloudflare R2 (uploads)
                                │   WhatsApp Cloud API    │
                                │   Gemini (AI drafts)    │
                                └─────────────────────────┘
```

The Flask app is monolithic-by-design. Subsystems are organized as
**blueprints** (one per domain area) talking through **services**
(stateless functions) to a single **SQLAlchemy schema**. There are
no microservices, no message queues, no separate background workers
in V1.

## Subsystems (high-level)

### Front Office
**Operator-facing daily workflow.** Handles arrivals, departures,
in-house guests, walk-ins. The Reservation Board is the spine — drag
a booking into a clean room, the rest of the system catches up.

- Routes: `app/routes/front_office.py`, `app/routes/reservation_board.py`,
  `app/routes/bookings.py`, `app/routes/guests.py`,
  `app/routes/calendar.py`
- Templates: `app/templates/board/`, `app/templates/front_office/`,
  `app/templates/bookings/`
- Decision: `docs/decisions/0001-reservation-board-as-front-office-spine.md`

### Housekeeping
**Room state machinery.** Tracks `Room.status` (available / occupied /
maintenance / cleaning) and `Room.housekeeping_status` (clean / dirty /
inspected / out_of_order). Cleaners + supervisors update from the
housekeeping board.

- Routes: `app/routes/housekeeping.py`, `app/routes/maintenance.py`,
  `app/routes/rooms.py`
- Templates: `app/templates/housekeeping/`, `app/templates/maintenance/`,
  `app/templates/rooms/`

### Folio
**The money spine.** One folio per booking; folio items are line
charges (room nights, mini-bar, POS posts, adjustments). Invoices are
generated from folios.

- Models: `Booking`, `Invoice`, `FolioItem`, `CashierTransaction`
- Services: `app/services/folio.py`, `app/services/invoices.py`
- Decision: `docs/decisions/0002-folio-as-money-spine.md`

### Cashiering
**Money-in.** Manual cashier transactions, payment reconciliation,
bank-statement upload. Folio is read; only `CashierTransaction` and
`Invoice.amount_paid` are written.

- Routes: `app/routes/cashiering.py`, `app/routes/accounting.py`
- Service: `app/services/cashiering.py`

### POS (Point of Sale)
**Restaurant / bar / F&B.** Terminal screen for staff, catalog admin
for managers. Posts charges to a guest folio when a guest is seated;
otherwise creates a walk-in transaction.

- Routes: `app/routes/pos.py`, `app/routes/menu_orders.py` (online QR)
- Templates: `app/templates/pos/`, `app/templates/menu/`

### Booking Engine
**Public-facing direct booking.** Guests search availability, pick a
room, fill a form, get a booking ref. No payment integration in V1.

- Routes: `app/routes/booking_engine.py`, `app/routes/public.py`
- Service: `app/services/booking_engine.py`

### Reporting
**Read-only analytics.** Operator + admin dashboards, P&L, occupancy,
revenue, source mix.

- Routes: `app/routes/reports.py`, `app/routes/dashboard.py`
- Service: `app/services/reports.py`

### Channel manager (direction)
**Path to OTA distribution.** V1 ships a complete schema +
admin UI + sandbox forms for the booking_com pilot. Inbound import,
modification, and cancellation pipelines all run end-to-end on
staging. **Zero outbound HTTP in V1** — real OTA clients are the next
phase.

- Routes: `app/routes/channels.py`, `app/routes/channel_exceptions.py`
- Services: `app/services/channels.py`, `app/services/channel_import.py`
- Models: `ChannelConnection`, `ChannelRoomMap`, `ChannelRatePlanMap`,
  `ChannelSyncJob`, `ChannelSyncLog`, `ChannelImportException`,
  `ChannelInboundEvent`
- Background design docs: `docs/channel_manager_architecture.md`,
  `docs/channel_manager_build_phases.md`,
  `docs/channel_manager_risk_checklist.md`

### Multi-property (direction)
**Schema-deep, UI-shallow.** Wave-1 models (`Room`, `Booking`,
`Invoice`, `FolioItem`, etc.) carry a `property_id` column that's
correctly populated on every write. The Property table exists. There
is no property switcher UI yet; routes hard-code the singleton
property.

- Service: `app/services/property.py`
- Background design docs:
  `docs/multi_property_foundation_plan.md`,
  `docs/multi_property_access_model.md`,
  `docs/multi_property_migration_strategy.md`
- Decision: `docs/decisions/0006-property-aware-before-multi-property.md`

## Cross-cutting concerns

### Business date
Hotels need a calendar day that doesn't roll over at midnight (a guest
checking in at 02:00 is still "today" from operations' perspective).
**`BusinessDateState`** is the singleton row that the Night Audit
process advances. **`current_business_date()`** in
`app/services/night_audit.py` returns it; templates expose it as
`{{ business_date }}`.

- Decision: `docs/decisions/0003-business-date-separate-from-server-time.md`
- Plan: `docs/accounts_business_date_night_audit_plan.md`

### Stay segments
A guest can occupy Room A for night 1, Room B for night 2 (mid-stay
move). Modeled as **one `Booking` + N `StaySegment` rows**. The
booking remains the single source of truth for guest, dates, folio,
payments. Renderers respect segments where present.

- Decision: `docs/decisions/0005-stay-segments-for-mid-stay-room-change.md`

### Activity log + audit
Every state-changing service writes an `ActivityLog` row. Metadata is
flat scalars only — no message bodies, no payment data, no secrets.
The audit log is the recovery story for "what happened?"

- Service: `app/services/audit.py`
- Model: `ActivityLog`

### Branding
A multi-tenant white-label seam. Every template reads `{{ brand.name }}`,
`{{ brand.short_name }}`, `{{ brand.primary_color }}`,
`{{ brand.logo_path }}`. Defaults live in `app/services/branding.py`;
overrides come from `BRAND_*_OVERRIDE` env vars (used by staging).

### Activity isolation between prod and staging
- Separate Postgres databases.
- Separate uploads directories.
- Separate WhatsApp tokens (sandbox or absent on staging).
- Staging banner ribbon makes the environment unmistakable.

## What is NOT in the system (and why)

- **No microservices.** One Flask app is enough.
- **No message queue.** Sync work happens in-request; long jobs use
  `flask` CLI commands triggered manually or by cron.
- **No background worker.** When real OTA clients ship (next sprint),
  we'll add a small APScheduler-style worker — not a Celery cluster.
- **No GraphQL.** REST-on-Flask is enough for two consumers
  (the operator UI and the public booking engine).
- **No SPA front-end.** Server-rendered Jinja with islands of
  vanilla JS for drag-and-drop. Tailwind CDN + custom design-system
  CSS variables.
- **No PCI-handling code.** All payment is operator-recorded;
  no card numbers ever flow through this app.

## File-by-file map

For the file-to-feature map, see `docs/architecture/module_map.md`.
For the entity diagram, see `docs/architecture/data_model.md`.
For external integrations, see `docs/architecture/integrations.md`.
