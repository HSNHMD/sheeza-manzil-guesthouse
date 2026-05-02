# Data model — major entities + relationships

> **Last updated:** 2026-05-03.
> Authoritative source: `app/models.py`. This document highlights the
> 34 model classes and the relationships that matter for reasoning
> about the system.

## Quick stats

- **34 model classes** in `app/models.py`.
- **24 Alembic migrations** under `migrations/versions/`.
- **Latest migration head:** `d6a2f59b8e34` (`add_channel_inbound_events`).

## The four big entities

### `User`
Operator + admin accounts. Has `role` (admin / staff) and `department`
(front_office / housekeeping / restaurant / accounting). The
post-login dispatcher in `app/services/landing.py` reads `department`
to decide where to send a staff user.

Key relationships:
- `Booking.created_by → User.id`
- `WorkOrder.assigned_to_user_id → User.id`
- `WorkOrder.reported_by_user_id → User.id`
- `ActivityLog.actor_user_id → User.id`
- `ChannelImportException.reviewed_by_user_id → User.id`

### `Room`
Physical room inventory. Has `room_type` (legacy string) plus
`room_type_id` (FK to `RoomType` — preferred) and three state fields:
- `status` — operational (available / occupied / maintenance / cleaning)
- `housekeeping_status` — clean / dirty / inspected / out_of_order

`Room` carries `property_id` (Multi-Property V1 foundation).

Key relationships:
- `Booking.room_id → Room.id`
- `RoomBlock.room_id → Room.id`
- `StaySegment.room_id → Room.id`
- `WorkOrder.room_id → Room.id` (nullable; SET NULL)
- `Room.assigned_to_user_id → User.id` (housekeeping assignee)

### `Booking`
The reservation. The single source of truth for guest, dates, room,
folio, payments, history.

```
Booking
├── booking_ref         e.g. "BKR8XHUO" — unique, 8 chars
├── room_id             nullable while held; required when assigned
├── guest_id
├── check_in_date / check_out_date
├── num_guests
├── status              unconfirmed / pending_verification / confirmed /
│                       checked_in / checked_out / cancelled
├── total_amount
├── source              direct / walk_in / whatsapp / booking_engine /
│                       booking_com / expedia / agoda / airbnb / other
├── external_source     mirror of source for OTA-imported bookings
├── external_reservation_ref   OTA-side id; partial UNIQUE with
│                              external_source
├── billing_target      guest / group
├── property_id         Multi-Property V1 foundation
└── created_by          → User.id
```

Key relationships:
- `Invoice.booking_id → Booking.id`
- `FolioItem.booking_id → Booking.id` (via folio)
- `StaySegment.booking_id → Booking.id` (CASCADE)
- `WorkOrder.booking_id → Booking.id` (nullable; SET NULL)
- `ChannelImportException.linked_booking_id → Booking.id` (nullable; SET NULL)
- `ChannelInboundEvent.linked_booking_id → Booking.id` (nullable; SET NULL)

### `Invoice`
Money owed for a booking. One booking can have many invoices (e.g.
deposit + final). Has `amount_paid`; the difference between
`total_amount` and `amount_paid` is the balance.

Key relationships:
- `Invoice.booking_id → Booking.id`
- `FolioItem.invoice_id → Invoice.id`
- `CashierTransaction` writes `Invoice.amount_paid` and
  `Invoice.payment_status` ("unpaid" / "partial" / "paid").

## Money / folio cluster

| Model | Role |
|---|---|
| `Invoice` | One per money "envelope" attached to a booking |
| `FolioItem` | One per line charge (room night, mini-bar, POS post, adjustment) |
| `CashierTransaction` | One per money movement (cash, card, transfer) |
| `BankTransaction` | Imported bank-statement rows used in reconciliation |

The folio is **the money spine**. See
`docs/decisions/0002-folio-as-money-spine.md`. Cashiering writes to
`CashierTransaction` and `Invoice.amount_paid`. POS writes to
`FolioItem` (and possibly `CashierTransaction` for the cash drawer).

## Stay-segment cluster

| Model | Role |
|---|---|
| `Booking` | The whole reservation |
| `StaySegment` | One row per (room × date-range) within the stay |

A booking with no segments renders by `booking.room_id`. A booking
with segments renders by walking `booking.stay_segments` (ordered
by `start_date`). See
`docs/decisions/0005-stay-segments-for-mid-stay-room-change.md`.

## Channel manager cluster

```
ChannelConnection (one per property × OTA)
├── ChannelRoomMap (RoomType ↔ external_room_id)
├── ChannelRatePlanMap (RatePlan ↔ external_rate_plan_id)
├── ChannelSyncJob (queued / running / success / failed / skipped /
│                   dead_lettered)
├── ChannelSyncLog (append-only event log)
├── ChannelImportException (manual-review queue;
│                           issue_type ∈ conflict / mapping_missing /
│                           invalid_payload / parse_error /
│                           booking_not_found / cancel_unsafe_state /
│                           modification_unsafe_state)
└── ChannelInboundEvent (idempotency ledger;
                         UNIQUE(channel_connection_id, external_event_id))
```

V1 makes **zero outbound HTTP**. The "test sync" button writes a
`test_noop` `ChannelSyncJob` and a matching `ChannelSyncLog`. See
`docs/architecture/integrations.md` for the path to real OTA
clients.

## Maintenance cluster

| Model | Role |
|---|---|
| `WorkOrder` | One per room/property issue |
| `Room` | Linked via `room_id`; flipped to OOO by severe issues |
| `Booking` | Linked via `booking_id` (nullable); used when an issue is tied to a guest stay |

Vocabularies (whitelisted on the model):
- `category` — plumbing / electrical / hvac / cleaning / furniture /
  appliance / safety / general
- `priority` — low / medium / high / urgent
- `status` — new / assigned / in_progress / waiting / resolved / cancelled

## Multi-property cluster (foundation)

Wave-1 models all carry `property_id`:
- `Room`, `Booking`, `Invoice`, `FolioItem`, `Guest`,
  `WhatsAppMessage`, `WorkOrder` (via `room.property_id`),
  `ChannelConnection`, `Property`.

`services.property.current_property_id()` returns the singleton
property today. Multi-property routing is intentionally NOT wired —
see `docs/decisions/0006-property-aware-before-multi-property.md`.

## Audit + activity

| Model | Role |
|---|---|
| `ActivityLog` | One row per state-changing service call |
| `WhatsAppMessage` | One per inbound/outbound WhatsApp message |

`ActivityLog.metadata_json` is a flat scalar dict — no raw payloads,
no message bodies, no card numbers. The Activity page at
`/admin/activity` is the recovery story for "what happened?"

## Migration chain (chronological)

```
a21b045cc4b5  initial_schema
b4c1f2d6e892  add_room_housekeeping_fields
e4f7a2b1c8d3  add_notes_to_room
e7c1a4b89d62  add_room_blocks_table
ddc320fae194  add_drive_id_columns
d8a3e1f29c40  add_folio_items_table
d6a7b9c0e215  add_pos_tables
e8b3c4d7f421  add_guest_order_tables
f9a4b8d2c531  add_booking_groups
f1c5b2a93e80  add_cashier_transactions_table
c5d2a3f8e103  add_rates_inventory_tables
c2b9f4d83a51  add_whatsapp_messages_table
f3a7c91b04e2  add_activity_log_table
0c5e7f3b842a  add_property_settings
1d9b6a4f5e72  add_property_foundation
3f7b1c8e2a04  add_stay_segments_table
4d8e3c91a76b  add_user_department
a3b8e9d24f15  add_business_date_and_night_audit_tables
2e8c4d7a3f51  add_channel_foundation
a8f3c91d5b27  add_work_orders_table
c4f7d2a86b15  add_channel_import_exceptions
d6a2f59b8e34  add_channel_inbound_events  ← current head
```

> The chronological order above is the order Alembic applies them;
> `flask db history` is the live answer.

## Authoritative file paths

| Concern | Path |
|---|---|
| All model definitions | `app/models.py` |
| Migration files | `migrations/versions/` |
| ER background docs | `docs/architecture/` (this folder) |
| Channel-specific schema notes | `docs/channel_manager_architecture.md` |
| Multi-property schema notes | `docs/multi_property_*.md` |
