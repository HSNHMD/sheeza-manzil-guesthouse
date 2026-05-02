# Module map — feature ↔ files

> **Last updated:** 2026-05-03.
> Authoritative map from "the X feature" to the routes / services /
> templates / models that implement it. Refresh whenever a sprint
> introduces a new blueprint or moves things around.

## How to read this file

Each section is one **blueprint** (= URL prefix). For each:
- **Routes:** the file under `app/routes/`.
- **Service(s):** the file(s) under `app/services/` that hold business
  logic. Routes call services; services call models.
- **Templates:** the directory under `app/templates/`.
- **Models:** SQLAlchemy classes from `app/models.py`.
- **Decision / docs:** ADR or design docs that frame the module.

## Auth

| Slot | Where |
|---|---|
| Routes | `app/routes/auth.py` |
| Service | `app/decorators.py` (`@admin_required`, `@login_required`) |
| Templates | `app/templates/auth/` |
| Models | `User` |
| Notes | Login at `/appadmin` (admin) and `/console` (staff). Staff post-login is dispatched by department via `app/services/landing.py`. |

## Dashboard

| Slot | Where |
|---|---|
| Routes | `app/routes/dashboard.py` |
| Service | (composes services from cashiering, folio, front_office) |
| Templates | `app/templates/dashboard/` |
| Notes | Post-login landing for admins. Quick KPIs across modules. |

## Front office

| Slot | Where |
|---|---|
| Routes | `app/routes/front_office.py` |
| Service | `app/services/front_office.py` (counts), `app/utils.py` (`hotel_date()`) |
| Templates | `app/templates/front_office/` |
| Notes | Arrivals / Departures / In House / index — all use `hotel_date()` so they respect the business date. |

## Reservation Board

| Slot | Where |
|---|---|
| Routes | `app/routes/reservation_board.py` |
| Service | `app/services/board.py`, `app/services/board_actions.py` |
| Templates | `app/templates/board/` |
| Models | `Booking`, `Room`, `RoomBlock`, `StaySegment` |
| Notes | The premium operational view. Drag/drop room moves. JS uses an IIFE pattern — `tests/test_board_js_syntax.py` guards against duplicate-`const` regressions. |

## Bookings

| Slot | Where |
|---|---|
| Routes | `app/routes/bookings.py` |
| Helpers | `generate_booking_ref()` (in routes file) |
| Templates | `app/templates/bookings/` |
| Models | `Booking`, `Guest` |

## Guests

| Slot | Where |
|---|---|
| Routes | `app/routes/guests.py` |
| Templates | `app/templates/guests/` |
| Models | `Guest` |

## Calendar

| Slot | Where |
|---|---|
| Routes | `app/routes/calendar.py` |
| Templates | `app/templates/calendar/` |
| Notes | Legacy availability calendar. The Reservation Board is the operational replacement; calendar still renders for power users. |

## Housekeeping

| Slot | Where |
|---|---|
| Routes | `app/routes/housekeeping.py` |
| Service | `app/services/housekeeping.py` |
| Templates | `app/templates/housekeeping/` |
| Models | `Room` (housekeeping_status), `RoomBlock`, `HousekeepingActivity` |

## Rooms

| Slot | Where |
|---|---|
| Routes | `app/routes/rooms.py` |
| Templates | `app/templates/rooms/` |
| Models | `Room`, `RoomType` |

## Maintenance / Work Orders

| Slot | Where |
|---|---|
| Routes | `app/routes/maintenance.py` |
| Service | `app/services/maintenance.py` |
| Templates | `app/templates/maintenance/` |
| Models | `WorkOrder` |
| Notes | Admin-only V1. Severe issues flip `Room.housekeeping_status='out_of_order'` + `Room.status='maintenance'`. |

## Folio + Invoices

| Slot | Where |
|---|---|
| Routes | `app/routes/folios.py`, `app/routes/invoices.py` |
| Service | `app/services/folio.py`, `app/services/invoices.py` |
| Templates | `app/templates/folios/`, `app/templates/invoices/` |
| Models | `Invoice`, `FolioItem`, `Booking`, `BookingGroup` |
| Notes | Folio is the money spine. See `docs/decisions/0002-folio-as-money-spine.md`. |

## Cashiering / Accounting

| Slot | Where |
|---|---|
| Routes | `app/routes/cashiering.py`, `app/routes/accounting.py` |
| Service | `app/services/cashiering.py`, `app/services/accounting.py` |
| Templates | `app/templates/cashiering/`, `app/templates/accounting/` |
| Models | `CashierTransaction`, `BankTransaction`, `Invoice` |

## Night Audit

| Slot | Where |
|---|---|
| Routes | `app/routes/night_audit.py` |
| Service | `app/services/night_audit.py` |
| Templates | `app/templates/night_audit/` |
| Models | `BusinessDateState` |
| Notes | Advances the business date. See `docs/accounts_business_date_night_audit_plan.md`. |

## POS (terminal + catalog)

| Slot | Where |
|---|---|
| Routes | `app/routes/pos.py` |
| Service | `app/services/pos.py` |
| Templates | `app/templates/pos/` |
| Models | `POSCategory`, `POSItem`, `CashierTransaction` (charges) |

## Online menu (QR)

| Slot | Where |
|---|---|
| Routes | `app/routes/menu_orders.py` |
| Templates | `app/templates/menu/` |
| Models | `MenuOrder`, `MenuOrderItem` |

## Booking Engine (public)

| Slot | Where |
|---|---|
| Routes | `app/routes/booking_engine.py`, `app/routes/public.py` |
| Service | `app/services/booking_engine.py` |
| Templates | `app/templates/booking_engine/`, `app/templates/public/` |

## Reports

| Slot | Where |
|---|---|
| Routes | `app/routes/reports.py` |
| Service | `app/services/reports.py` |
| Templates | `app/templates/reports/` |

## Channel manager (admin-only)

| Slot | Where |
|---|---|
| Routes | `app/routes/channels.py`, `app/routes/channel_exceptions.py` |
| Service | `app/services/channels.py`, `app/services/channel_import.py` |
| Templates | `app/templates/channels/`, `app/templates/channel_exceptions/` |
| Models | `ChannelConnection`, `ChannelRoomMap`, `ChannelRatePlanMap`, `ChannelSyncJob`, `ChannelSyncLog`, `ChannelImportException`, `ChannelInboundEvent` |
| Pilot | `booking_com` |
| Decisions | `docs/channel_manager_architecture.md`, `docs/channel_manager_build_phases.md`, `docs/channel_manager_risk_checklist.md` |

## Property settings

| Slot | Where |
|---|---|
| Routes | `app/routes/property.py`, `app/routes/property_settings.py` |
| Service | `app/services/property.py`, `app/services/branding.py` |
| Templates | `app/templates/property/`, `app/templates/property_settings/` |
| Models | `Property`, `PropertySetting` |

## Inventory / Rates

| Slot | Where |
|---|---|
| Routes | `app/routes/inventory.py` |
| Service | `app/services/inventory.py` |
| Templates | `app/templates/inventory/` |
| Models | `RatePlan`, `Restriction`, `RoomTypeInventory` |

## Groups

| Slot | Where |
|---|---|
| Routes | `app/routes/groups.py` |
| Templates | `app/templates/groups/` |
| Models | `BookingGroup` |

## WhatsApp inbox + webhook

| Slot | Where |
|---|---|
| Routes | `app/routes/whatsapp_webhook.py` |
| Service | `app/services/whatsapp.py`, `app/services/ai_drafts.py` |
| Templates | `app/templates/whatsapp/` |
| Models | `WhatsAppMessage`, `AIDraft` |
| Notes | Production-deployed. AI drafts use `AI_DRAFT_PROVIDER` env var (Gemini today; mockable for staging). |

## Activity log

| Slot | Where |
|---|---|
| Routes | `app/routes/activity.py` |
| Service | `app/services/audit.py` |
| Templates | `app/templates/activity/` |
| Models | `ActivityLog` |
| Notes | Every state-changing service emits one row. Metadata is flat scalars; never raw payloads. |

## Diagnostics

| Slot | Where |
|---|---|
| Routes | `app/routes/diag.py` |
| Service | `app/services/version.py` |
| Templates | `app/templates/diag/` |
| Notes | `/admin/diag` shows deployed sha + env summary. Critical for verifying any deploy. |

## Staff portal

| Slot | Where |
|---|---|
| Routes | `app/routes/staff.py` |
| Templates | `app/templates/staff/` |
| Notes | Department-scoped landing pages. Staff are bounced off non-whitelisted paths by `_staff_guard` in `app/__init__.py`. |
