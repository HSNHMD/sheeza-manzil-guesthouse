# Channel Manager / External Distribution — Architecture (planning only)

> **Status:** PLANNING DOCUMENT. No code, no migrations, no real OTA
> sync. Scope is to design a safe path so we can decide what to build,
> in what order, and what NOT to do yet.
>
> **Scope:** future architecture for connecting the PMS to OTAs
> (Booking.com, Expedia, Agoda, Airbnb), and later direct XML/API
> partners. **GDS is explicitly out of scope** for the foreseeable
> future — the property profile (Maldivian guesthouse) does not
> warrant the GDS overhead.
>
> **Non-goals for V1:** payment processing on behalf of OTAs, virtual
> credit cards, group inventory pooling, multi-property rollups,
> revenue management automation.


## 1. Channel Manager — Core Responsibilities

The channel manager is a **distinct subsystem** that sits between the
PMS (the source of truth) and one or more external distribution
channels. It owns:

| Responsibility | Direction | Notes |
|---|---|---|
| Inventory sync (room counts) | PMS → OTA | per RoomType, per night |
| Availability sync (open/close days) | PMS → OTA | derived from `RateRestriction.stop_sell` + physical inventory |
| Restrictions sync (min/max stay, CTA, CTD) | PMS → OTA | from `RateRestriction` rows |
| Rate sync (nightly rates, plan structure) | PMS → OTA | from `RatePlan` + `RateOverride` |
| Reservation import | OTA → PMS | new bookings created externally |
| Modification sync | OTA → PMS | dates/guests/special-requests changes |
| Cancellation sync | OTA → PMS | + cancellation-fee bookkeeping |
| Channel mapping | both | maps internal RoomType / RatePlan ⇄ each OTA's external IDs |
| Sync audit / error log | internal | every API call recorded for debugging |

The channel manager **does not** make pricing or availability
*decisions*. Those decisions live in the existing Rates & Inventory
V1 layer. The CM only **publishes** what Inventory says and
**consumes** what OTAs send back.


## 2. Internal Prerequisites Audit (current PMS state, 2026-04-30)

### ✅ Ready

| Area | Status | Why it's ready |
|---|---|---|
| **Room Types** | ✅ READY | `RoomType` (id, code unique, name, max_occupancy, base_capacity, is_active). Migration `c5d2a3f8e103` backfilled from existing strings. |
| **Rate Plans** | ✅ READY | `RatePlan` (code unique, room_type_id FK, base_rate, currency, is_refundable, is_active). |
| **Date-driven pricing** | ✅ READY | `RateOverride` (room_type FK, optional rate_plan FK, start_date, end_date inclusive, nightly_rate, is_active). Composes via `services.inventory.nightly_rate_for()`. |
| **Restrictions** | ✅ READY | `RateRestriction` (min_stay, max_stay, closed_to_arrival, closed_to_departure, stop_sell, is_active, dated). Most-restrictive wins in `services.inventory.restrictions_on()`. |
| **Inventory math** | ✅ READY | `services.inventory.count_available()` already excludes maintenance, OOO, conflicting bookings, RoomBlocks. Returns integer per-type availability for any date span. |
| **Booking lifecycle vocabulary** | ✅ READY | `unconfirmed / pending_verification / new_request / pending_payment / payment_uploaded / payment_verified / confirmed / checked_in / checked_out / cancelled / rejected`. Wide enough to model OTA states. |
| **Group bookings** | ✅ READY | `BookingGroup` (group_code, master_booking_id, billing_mode). OTAs occasionally send group reservations as separate bookings; we can attach them post-import. |
| **Audit trail** | ✅ READY | `ActivityLog` already used everywhere; CM can extend with `channel.*` actions. |
| **Business date** | ✅ READY | `BusinessDateState` controls when a day "closes." OTA modifications received after Night Audit need explicit operator handling — see Risk #3. |
| **Folio / Cashiering** | ✅ READY | OTA-virtual-card payments and pay-at-property both fit the existing FolioItem + CashierTransaction model. The "VCC" case is a regular cashier transaction with a method like `card` / `bank_transfer`. |
| **Concurrency guard for booking creation** | ✅ READY | `services.inventory.check_bookable()` re-runs availability inside `services.booking_engine.create_direct_booking()`. The same guard works for the import path. |

### ⚠️ Partial / Risky

| Area | Status | Gap |
|---|---|---|
| **Booking source tracking** | ⚠️ PARTIAL | `Booking` table has **no `source` column**. We log `source` in `ActivityLog.metadata_json` (`admin_form`, `staff_walkin`, `public_form`, `booking_engine`) but it is NOT a queryable column. Channel manager **MUST** add `Booking.source` (or `booking_source_id` FK) before import. Without it, "what's our OTA share?" reports are impossible. |
| **Currency** | ⚠️ PARTIAL | `RatePlan.currency` (default USD), `CashierTransaction.currency` (default MVR), `Invoice` has no currency column. OTAs send rates in their own currencies (often EUR/USD). Need a canonical property currency + per-booking `currency` snapshot. |
| **Tax / service charge** | ⚠️ PARTIAL | `Invoice.tax_amount` and `FolioItem.tax_amount` exist as columns but always default to 0. No tax-rate config, no Maldives GST + Service Charge structure. OTAs typically expect "rate before tax" + "rate after tax" on the same payload. |
| **Cancellation policy** | ⚠️ PARTIAL | A booking can be `cancelled`, but there is **no cancellation policy model** (free-cancellation deadline, percentage forfeit, no-show fee). OTAs require this to be declared per RatePlan. |
| **ID document collection** | ⚠️ PARTIAL | The legacy `/submit` flow **requires** ID upload; the new `/book/*` engine does not. OTA imports won't bring an ID document at all — staff must collect at check-in. We need an explicit "id_collected_at" column or status badge so Front Office can chase it. |
| **Reservation conflict handling** | ⚠️ PARTIAL | `services.inventory.check_bookable()` is solid for direct bookings, but the import path has not yet been tested against scenarios like "OTA sends a reservation for a date already sold to direct" (race window between cache + sync). Needs explicit "import-time overbook detection" branch. |
| **Payment timing** | ⚠️ PARTIAL | Existing flows assume "pay before arrival via bank transfer" or "settle at checkout." OTAs introduce **virtual credit cards (VCC)** that can be charged X days before arrival, OR **pay-at-property** OTA bookings where the OTA invoices commission separately. Folio integration needs explicit handling of: VCC pre-charge, OTA commission invoice, no-show charge logic. |

### ❌ Not ready

| Area | Status | Plan |
|---|---|---|
| **Per-channel mapping models** | ❌ MISSING | Need new tables: `ChannelConnection`, `ChannelRoomMap`, `ChannelRatePlanMap`. Section 4 below. |
| **Async job runner** | ❌ MISSING | Today everything runs inline in the request. CM sync must be **out-of-band** with retries + dead-letter. We need a job queue (Celery / RQ / DB-backed). Section 8. |
| **Outbound HTTP wrapper with provider abstraction** | ❌ MISSING | We have `services.whatsapp` as the only outbound HTTP service. CM needs a per-provider client interface so we can mock/replay every call. |
| **Duplicate import detection** | ❌ MISSING | No unique constraint on "external_ref + channel" — required to make import retries idempotent. |
| **Inventory snapshot table** | ❌ MISSING (probably) | OTAs need "what was published when." Useful for debugging "you sent us 0 rooms but our system showed 3 last night." Optional V1; mandatory V2. |


## 3. Channel-Specific Concerns (factual, vendor-neutral)

### Booking.com
- API style: XML over HTTPS (now also Connect API REST).
- Strong: well-documented, stable, Maldives-relevant, 24/7 support, certification flow is rigorous.
- Hard: certification process can take 4–8 weeks; their "rate plan derived" model means our parent/child rate plans need to map cleanly.
- VCC: yes, charged according to the cancellation policy timeline.

### Expedia
- API style: REST (Expedia Partner Central API).
- Strong: well-documented, OAuth-based.
- Hard: Lodging Exchange (legacy) vs Partner Central — must pick deliberately. Less Maldives traffic than Booking.com for our segment.

### Agoda
- API style: XML (Agoda YCS API).
- Strong: high traffic for South-East Asia / Maldives.
- Hard: documentation less consistent than Booking.com, certification slower.

### Airbnb
- API style: REST, but the **Channel Manager API requires Airbnb-approved channel manager status** — independent integrations are gated. Most properties use SiteMinder / Cloudbeds / etc. as a middleman.
- Hard: not viable as a first integration. Treat as Phase 6+.


## 4. Recommended Internal Data Models

> All field types are tentative; the goal here is the **shape** and the
> **invariants**, not column-precise SQL.

### `ChannelConnection`
One per OTA we publish to. Holds credentials + property mapping.

```
ChannelConnection
- id
- channel_code: 'booking_com' | 'expedia' | 'agoda' | 'airbnb' | 'direct_xml'
- display_name: 'Sheeza Manzil — Booking.com'
- property_external_id: e.g. Booking.com hotel_id
- credentials_secret_ref: ID into a secret store (NEVER in DB)
- environment: 'sandbox' | 'production'
- is_active: bool
- last_handshake_at, last_handshake_ok: bool
- min_lead_time_hours: how close to arrival the OTA may sell
- max_lead_time_days: how far in advance the OTA may sell
- supports: csv of capability flags ('availability', 'rates',
                                       'restrictions', 'reservations',
                                       'modifications', 'cancellations')
- notes
- created_at / updated_at
```

Why a row per environment: sandbox creds and prod creds are
fundamentally different lives. Mixing them is the #1 cause of
"why did we just sell our staging room on real Booking.com" outages.

### `ChannelRoomMap`
Maps **one internal `RoomType`** to its external counterpart on a
specific channel. **The same internal type can map differently per
channel** (a "Deluxe Twin" might be "Deluxe Room with Twin Beds" on
Booking.com but "Twin Standard" on Agoda).

```
ChannelRoomMap
- id
- channel_connection_id FK
- room_type_id FK
- external_room_id: string (channel's id)
- external_room_name_snapshot: string  (kept for human debug)
- inventory_count_override: nullable int (defaults to count of physical
                                          rooms of this type)
- is_active: bool
- created_at / updated_at
```

### `ChannelRatePlanMap`
Maps **one internal `RatePlan`** to its external counterpart per
channel. Same internal plan can publish differently per OTA.

```
ChannelRatePlanMap
- id
- channel_connection_id FK
- rate_plan_id FK
- external_rate_plan_id: string
- external_rate_plan_name_snapshot: string
- meal_plan_external_id: nullable string  (e.g. BB, HB, FB on Booking.com)
- cancellation_policy_external_id: nullable string
- is_active: bool
```

### `ChannelReservation`
One row per **incoming OTA reservation**, linked to the internal
`Booking` it created.

```
ChannelReservation
- id
- channel_connection_id FK
- external_reservation_id: string
- channel_code: redundant denormalized (for filter speed)
- booking_id FK to Booking (nullable; null while pending review)
- raw_payload_json: text — full incoming payload, redacted of sensitive
                            fields before storage
- status: 'imported' | 'rejected' | 'manual_review' | 'failed_overbook'
- modification_count: int  (how many edits the OTA has sent since import)
- last_modification_at
- created_at / updated_at
- Unique index: (channel_connection_id, external_reservation_id)
```

That unique index is **the** anti-duplicate-import guarantee.

### `ChannelInventorySnapshot`
Time-series row of "what we published when, to which channel." Useful
for forensics ("our system shows 3 free rooms but the OTA sold 5 — what
did we tell them?"). Optional in V1, recommended by V3.

```
ChannelInventorySnapshot
- id
- channel_connection_id FK
- room_type_id FK
- snapshot_date: date  (the night, not the publish date)
- inventory_published: int
- rate_published: float
- restrictions_published_json: text  (composed restriction object)
- snapshot_taken_at: datetime
```

### `SyncJob`
Outbound work item. Pluralized so a sync job covers many days × many
types in one operational unit.

```
SyncJob
- id
- channel_connection_id FK
- job_type: 'availability' | 'rates' | 'restrictions' | 'full_resync'
- scope_json: text  (which dates / room types this job covers)
- status: 'queued' | 'running' | 'succeeded' | 'failed' | 'dead_lettered'
- attempt_count: int
- max_attempts: int  (default 5)
- last_error_text: nullable
- queued_at, started_at, finished_at: datetimes
- requested_by_user_id FK nullable
```

### `SyncLog`
Append-only diary of every API call (request → response). Sized for
~6 weeks of retention; older rows archived/truncated by maintenance
job.

```
SyncLog
- id
- created_at (indexed)
- channel_connection_id FK
- direction: 'outbound' | 'inbound'
- endpoint: string
- http_status: int nullable
- duration_ms: int nullable
- request_summary: short string  (NEVER full body — see Risk #4)
- response_summary: short string
- ok: bool
- correlated_sync_job_id FK nullable
- correlated_channel_reservation_id FK nullable
```


## 5. Room / Rate Mapping Design Principles

1. **Every external published unit has exactly ONE internal RoomType.**
   No M:N. If an OTA wants two room "groups" that we sell as one
   internal type, we publish twice (two `ChannelRoomMap` rows pointing
   at the same `room_type_id`).

2. **The opposite is allowed and common:** one internal RoomType can
   map to N external room IDs across N channels. Each map is its own
   row.

3. **Inventory is computed per-channel, not split per-channel.**
   If we have 3 Deluxe rooms physically, we publish "3 available" to
   every active channel. We do **NOT** allocate "1 to Booking.com, 1
   to Agoda, 1 to direct" — that's manual buckets and causes
   underbooking. Overbooking is prevented by the inbound side
   (`ChannelReservation` import does a fresh availability check
   before creating the Booking; if the room is gone, the reservation
   lands in `manual_review` and operator decides).

4. **`inventory_count_override`** on `ChannelRoomMap` exists for the
   one legitimate exception: protecting "house" inventory we never
   want to sell to OTAs. Default is None (= use physical count).

5. **Rate parity is policy, not enforcement.** OTAs require rate
   parity in their contracts; we publish the same rate everywhere
   from the same `RatePlan`. The map rows let us *attach* OTA-specific
   metadata (meal plan code, cancellation policy id) without
   duplicating the rate itself.


## 6. Sync Direction Matrix

| Data | Direction | Frequency | Trigger |
|---|---|---|---|
| Inventory (rooms-available per night) | PMS → OTA | every N minutes + on every Booking create/cancel/modify | event-driven + scheduled |
| Rates (per-night nightly_rate) | PMS → OTA | on RateOverride / RatePlan change + scheduled nightly | event + cron |
| Restrictions (min/max stay, CTA, CTD, stop_sell) | PMS → OTA | on RateRestriction change + scheduled nightly | event + cron |
| Reservations (new) | OTA → PMS | every N minutes (poll) OR webhook | OTA push or pull |
| Modifications (date/guest/notes change) | OTA → PMS | same as new | same |
| Cancellations | OTA → PMS | same as new | same |
| Cancellation policy | PMS → OTA (config) + OTA → PMS (per booking) | rare | only when policy edited |
| Restrictions overrides set inside the OTA dashboard | OTA → PMS (manual review) | n/a | NEVER auto-import — operator reviews |
| Guest contact info changes after import | OTA → PMS | per modification | manual review for major changes |

Hard rule: **PMS is canonical for inventory + rates + restrictions**.
**OTAs are canonical for reservations originating on their side.**
We never let an OTA dashboard quietly re-write our rate.


## 7. Conflict Strategy

### 7.1 Duplicate reservation imports
- Unique index `(channel_connection_id, external_reservation_id)` on
  `ChannelReservation` blocks the second insert.
- On constraint violation, we update the existing row's
  `modification_count + 1` and re-link if the booking_id changed.
- Operator never sees a duplicate Booking row.

### 7.2 Imported reservation for already-full dates
- Import flow re-runs `services.inventory.check_bookable()` BEFORE
  creating the Booking.
- If it returns `ok=False`, the `ChannelReservation` is created in
  status `failed_overbook` with the reasons recorded; **no Booking
  is created**.
- Front Office gets a notification; operator either: (a) finds an
  open room of the same type and creates the Booking manually, or
  (b) cancels through the OTA and accepts the commission risk.
- Auto-create-anyway is **never** a default.

### 7.3 Stale OTA availability
- Tracked via `ChannelInventorySnapshot`. If the snapshot says we
  published "3" 30 minutes ago but our DB now says "1", we send a
  delta update immediately AND log a stale-availability incident
  for the operator.
- Hard-cap on stale window: any cached number older than 5 minutes
  is treated as unknown.

### 7.4 OTA sends a modification that conflicts internally
Examples: guest extended their stay, but the new departure date is
already sold to a direct booking; OR they changed dates entirely.

- New dates run through `check_bookable()` for the modified span.
- If OK → apply.
- If not OK → mark `ChannelReservation.status = 'manual_review'`,
  flash to operator, do NOT mutate the Booking until operator
  decides.

### 7.5 Race conditions
- Hot path is "OTA confirms a sale at moment T1; direct guest books
  same room at T1 + 50ms before our outbound delta has reached the
  OTA." Solution:
  - All inbound inserts of `Booking` go through a single
    `create_booking()` that re-validates availability inside a DB
    transaction with a **row lock on `Room`** (existing pattern;
    needs to be hardened).
  - Outbound deltas use the existing `SyncJob` queue with a
    "merge consecutive identical jobs" rule so we don't fire 12
    parallel updates per booking.

### 7.6 Cancellation mismatch
- OTA says "cancelled," we already checked the guest in.
- Always favor what actually happened on property. The Booking
  status stays `checked_in`/`checked_out`. The `ChannelReservation`
  is annotated with `external_cancelled_at`; operator chases the
  commission with the OTA via their dashboard.

### 7.7 Channel outage
- Sync jobs that fail with 5xx / connection error retry with
  exponential backoff (5s, 30s, 5min, 30min, 4h, 24h). After
  max_attempts (default 5), the job lands in `dead_lettered` and
  the operator sees a banner.
- **The PMS keeps working through OTA outages.** No PMS write path
  is ever blocked on an OTA call.


## 8. Safety Architecture

| Guardrail | Why | Concrete shape |
|---|---|---|
| Async sync jobs, never inline | A 30s OTA roundtrip in a /bookings/new request blocks check-in | `SyncJob` table + a worker (RQ / Celery / cron) |
| Retry with exponential backoff | Transient network errors are common | columns `attempt_count`, `max_attempts`, schedule by `next_run_at` |
| Dead-letter queue | Catches the irrecoverable | `SyncJob.status='dead_lettered'` + admin UI to retry / dismiss |
| Manual-review state | Some conflicts must NOT be auto-resolved | `ChannelReservation.status='manual_review'` + dedicated queue page |
| Audit trail for every import / export | Required for OTA disputes | `SyncLog` (append-only) + `ChannelReservation.raw_payload_json` |
| One-channel disable kill switch | Pull a bad channel without breaking others | `ChannelConnection.is_active=False` halts all jobs scoped to that connection |
| Sandbox-only by default | Avoid catastrophic prod sells | new connections start in `environment='sandbox'`; promotion to prod is a manual admin action with confirmation |
| Read-only dry-run mode | Catch bugs before they touch real data | every outbound job has a `--dry-run` flag that builds the payload but does not POST |
| Secrets never in DB | Stop credentials leaks | `credentials_secret_ref` points at env / vault; the actual values live in environment variables or a secret manager |
| All sensitive payloads redacted before storage | OTA payloads include guest IDs, card last-4 | `SyncLog.request_summary` truncates to ~120 chars; full body NEVER persisted |
| Rate-limit aware client | OTAs throttle aggressively | per-channel token bucket; back off + queue on 429 |


## 9. ActivityLog Vocabulary (proposed)

When the channel manager actually ships, all events flow through
`services.audit.log_activity()`:

| Action | Trigger |
|---|---|
| `channel.connection_created` | new `ChannelConnection` row |
| `channel.connection_updated` | toggle / settings edit |
| `channel.connection_disabled` | kill-switched |
| `channel.room_mapped` | `ChannelRoomMap` created |
| `channel.rate_plan_mapped` | `ChannelRatePlanMap` created |
| `channel.sync_job_queued` | new `SyncJob` |
| `channel.sync_job_succeeded` / `.failed` / `.dead_lettered` | terminal job states |
| `channel.reservation_imported` | new `Booking` from OTA |
| `channel.reservation_modified` | inbound modification applied |
| `channel.reservation_cancelled` | inbound cancellation applied |
| `channel.reservation_manual_review` | conflict → operator queue |
| `channel.overbook_blocked` | import refused due to full inventory |

Strict metadata whitelist: `channel_code, connection_id,
external_reservation_id, sync_job_id, attempt_count, room_type_id,
booking_id, booking_ref, dates`. Never log credentials, never log full
payloads, never log card data.


## 10. Out of Scope for V1 (explicit list)

| Feature | Reason it's deferred |
|---|---|
| Virtual credit card auto-charging | Requires PCI compliance scope review + secure tokenization vault |
| Group rate / contract rate publication | OTA group rate APIs are inconsistent; manual quoting is fine for our scale |
| Multi-property fleet management | Single property today |
| GDS / Sabre / Amadeus | Property profile doesn't justify the integration cost |
| Revenue management (auto-pricing) | Out of scope; the operator sets rates |
| Dynamic packaging (room + ferry transfer) | Folio supports ancillary; OTA dynamic packaging APIs are channel-specific and immature |
| Automatic upselling on import | Channel managers can suggest upgrades on import; manual is safer for V1 |
| Native Airbnb integration | Their channel-manager API is gated to approved partners |


## 11. Component Diagram (rough)

```
┌─────────────────────────────────────────────────────────────────┐
│                         PMS (existing)                          │
│                                                                 │
│   RoomType    RatePlan    RateOverride    RateRestriction       │
│       │           │            │                │               │
│       └───────────┴────────────┴────────────────┘               │
│                          │                                      │
│            services.inventory.* (canonical)                     │
│                          │                                      │
│   Booking ◄──── services.booking_engine.* ◄── /book/* + admin   │
│       │                                                         │
│       └──► FolioItem  ──► CashierTransaction                    │
└────────────────────────│────────────────────────────────────────┘
                         │
                         │ events: booking_created/cancelled/modified
                         │ events: rate_changed, restriction_changed
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                  Channel Manager (NEW, future)                  │
│                                                                 │
│   ChannelConnection ◄── ChannelRoomMap ── ChannelRatePlanMap    │
│           │                    │                  │             │
│           ▼                    ▼                  ▼             │
│        SyncJob ──► Provider Client (per OTA)                    │
│           │              │                                      │
│           └──► SyncLog   │                                      │
│                          ▼                                      │
│                ChannelInventorySnapshot                         │
│                                                                 │
│   ChannelReservation ◄── inbound polling / webhook              │
│           │                                                     │
│           └──► creates Booking via existing                     │
│                services.booking_engine.create_direct_booking    │
│                (with source='channel_<x>')                      │
└─────────────────────────────────────────────────────────────────┘
```

Notice that **the channel manager does not call OTA APIs from request
handlers**. Every external call goes through a `SyncJob`. The PMS
write path is fully decoupled from OTA latency / outage.


## 12. Implementation Notes for Future Builders

- **Start with a "fake OTA" provider**: a local service that responds
  to our outbound calls with deterministic JSON. Lets us build the
  whole machinery (mapping, scheduling, retry, conflict UX) without
  touching a real API. Promote to a real sandbox only when the fake
  layer has zero open bugs.
- **Build the inbound path first**, not the outbound. Importing a
  reservation that already has all its data is far simpler than
  publishing rates correctly across 365 dates × N room types × N
  rate plans. Get one OTA's reservation import working end-to-end
  before any rate sync.
- **Idempotency is mandatory.** Every operation must be safe to run
  twice. Use the (channel_code, external_id) unique index for
  reservation imports; use deterministic `SyncJob` keys for outbound.
- **The PMS is cancellation-policy-blind today.** Adding cancellation
  policies is a hard prerequisite for any OTA work — the OTA needs
  to know what to do when a guest cancels, and our system needs to
  charge the right fee.

---

*Document owner: PMS architecture. Last updated: 2026-04-30.*
