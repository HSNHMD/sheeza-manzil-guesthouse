# Master product roadmap

> **Last updated:** 2026-05-03.
> Tracks the product direction at a quarter-by-quarter resolution.
> Anything more granular lives in `docs/handoff/next_steps.md`.

## Pillar 1 — Operational reliability (the floor)

The platform must not lose money, lose bookings, or page operators
at 3 AM. Everything below depends on this pillar staying solid.

| Status | Item |
|---|---|
| ✅ Done | Reservation Board as front-office spine (decision 0001) |
| ✅ Done | Folio + invoices + cashiering (decision 0002) |
| ✅ Done | Business date / Night Audit V1 (decision 0003) |
| ✅ Done | Activity log on every state change |
| ✅ Done | Maintenance / Work Orders V1 |
| ✅ Done | Staging-first workflow (decision 0004) |
| ✅ Done | Recovery / handoff documentation (this sprint) |
| 🟡 In flight | Production-merge spike (`docs/handoff/next_steps.md` sprint 1) |
| 🔜 Next | Nightly DB backup automation (cron + retention) |
| 🔜 Next | Off-site DB dump to R2 (uploads already wired) |
| ⏳ Later | Operator alerting (email or Slack) on `cancel_unsafe_state` and on service down |

## Pillar 2 — Distribution (channel manager)

Move from "we know what we'd build" to "OTAs are sending us live
reservations." Pilot is **booking_com**.

| Status | Item |
|---|---|
| ✅ Done | Channel Manager Foundation V1 (5 models + admin UI) |
| ✅ Done | OTA Reservation Import + Exception Queue V1 |
| ✅ Done | OTA Modification + Cancellation Handling V1 |
| 🔜 Next | Real Booking.com sandbox HTTP client |
| 🔜 Next | Scheduled inbound poll / webhook receiver |
| 🔜 Next | Outbound availability + rate push |
| ⏳ Later | Dead-letter retry worker |
| ⏳ Later | Second pilot channel (Expedia or Agoda — pick after 30 incident-free days on Booking.com) |
| ⏳ Later | Per-property channel scoping in admin UI |

## Pillar 3 — Multi-property

Move from "schema-deep, UI-shallow" to a real property switcher.

| Status | Item |
|---|---|
| ✅ Done | Wave-1 schema (every wave-1 model carries `property_id`) |
| ⏳ Later | Per-route query-scoping audit (the unblocking work) |
| ⏳ Later | Property switcher in admin chrome |
| ⏳ Later | Per-property branding + landing |
| ⏳ Later | Per-property permissions |

Background: `docs/multi_property_foundation_plan.md`,
`docs/multi_property_access_model.md`,
`docs/multi_property_migration_strategy.md`.

## Pillar 4 — Revenue management

Better pricing decisions; less spreadsheet, more system.

| Status | Item |
|---|---|
| ✅ Done | Rate plans + restrictions schema |
| ✅ Done | Inventory check (`services.inventory.check_bookable`) |
| ⏳ Later | Yield management UI (price bands by occupancy) |
| ⏳ Later | Forecasting dashboard (trailing 30 / forward 90) |

## Pillar 5 — Guest experience

Things the guest sees directly.

| Status | Item |
|---|---|
| ✅ Done | Public booking engine V1 |
| ✅ Done | Online menu (QR ordering) V1 |
| ✅ Done | WhatsApp inbound + AI draft (production) |
| ⏳ Later | Stripe / PayMaya online payment for booking engine |
| ⏳ Later | Self-service check-in (post-COVID guest expectation) |

## Pillar 6 — Reporting & insights

Less ad-hoc SQL; more in-app reporting.

| Status | Item |
|---|---|
| ✅ Done | P&L V1 |
| ✅ Done | Bank reconciliation upload |
| ✅ Done | Payment reconciliation V1 |
| ✅ Done | Reports overview |
| ⏳ Later | Source-mix dashboard (direct vs OTA vs walk-in over time) |
| ⏳ Later | RevPAR / ADR / occupancy trend |
| ⏳ Later | Channel sync health dashboard |

## Background design docs in `/docs`

These pre-existing docs cover deeper material referenced from this
roadmap:

| Doc | Covers |
|---|---|
| `docs/accounts_business_date_night_audit_plan.md` | Business date + Night Audit detailed design |
| `docs/admin_dashboard_plan.md` | Admin dashboard direction |
| `docs/channel_manager_architecture.md` | Channel manager architecture |
| `docs/channel_manager_build_phases.md` | 4-phase build plan toward real OTA HTTP |
| `docs/channel_manager_risk_checklist.md` | Risk register for the OTA work |
| `docs/guest_folio_accounting_pos_roadmap.md` | Folio / accounting / POS direction |
| `docs/multi_property_foundation_plan.md` | Multi-property wave plan |
| `docs/multi_property_access_model.md` | Per-property permissions design |
| `docs/multi_property_migration_strategy.md` | Migration approach for multi-property |

## How to update this roadmap

Add a row, set status, link the relevant ADR or handoff entry.
Don't delete completed rows — historical record matters when
someone asks "did we ever consider X?"
