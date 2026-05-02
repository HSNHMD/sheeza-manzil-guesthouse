# Channel Manager — Build Phases (planning only)

> **Status:** PLANNING DOCUMENT.
> Companion to `docs/channel_manager_architecture.md` (the design)
> and `docs/channel_manager_risk_checklist.md` (the things that can
> go wrong).
>
> **Hard rule:** No phase moves to "production" credentials until the
> exit gate of the previous phase is satisfied. The phases below are
> ordered by **risk reduction**, not by feature completeness.


## Phase 0 — Decisions & Sign-off (ZERO code)

### Goals
- Confirm property targets the right OTAs.
- Confirm we have legal & commercial agreements in place.
- Confirm the "no inline OTA calls, async only" architectural rule.

### Deliverables
- Owner reads `docs/channel_manager_architecture.md` end-to-end.
- Owner reads `docs/channel_manager_risk_checklist.md` and accepts
  every HARD item.
- OTA shortlist with **one** chosen first target (recommended:
  Booking.com — see Section 12).
- Decision recorded: which currency the property publishes in
  (recommend USD for Maldives), and whether rates are
  tax-inclusive or tax-exclusive (recommend **inclusive** to match
  what Booking.com expects in most markets).

### Exit gate
- Sign-off on the architecture + risk checklist.
- Sandbox account requested from chosen OTA (Booking.com sandbox
  takes 1–2 weeks to provision).


## Phase 1 — Source Tracking + Cancellation Policy + Tax Foundation

### Goals
Plug the three biggest gaps the audit found:

1. `Booking.source` (and friends).
2. `CancellationPolicy` model + `RatePlan.cancellation_policy_id`.
3. Minimal tax / service-charge model.

These are PMS-internal changes. **Zero OTA traffic** in this phase.

### Deliverables
- New columns on `Booking`:
  - `source` (string, indexed) — `'direct' | 'admin_form' |
    'staff_walkin' | 'public_form' | 'booking_engine'` and later
    `'channel_<code>'`.
  - `external_reservation_id` (nullable string, indexed; unique
    composite with `source` for OTA imports).
  - `currency` (string, default property currency).
- New `CancellationPolicy` model:
  - `code` (unique), `name`, `free_until_days_before` (int, nullable),
    `forfeit_percentage_after_free_until` (float),
    `no_show_charge_percentage` (float), `is_active`.
- `RatePlan.cancellation_policy_id` FK.
- New `TaxRule` model:
  - `code`, `name`, `percentage`, `applies_to_types` (csv:
    `'room' | 'food' | 'service' | 'goods' | …`),
    `is_inclusive` (bool), `is_active`.
- `Invoice.currency` column; `Invoice.tax_amount` populated based on
  `TaxRule` rather than always 0.
- Reports V1 ancillary breakdown gains a `source` filter so we can
  answer "what's our direct vs OTA share?" on day 1.
- All new columns get migration + tests.

### Exit gate
- All existing bookings have `source` populated (backfilled from
  ActivityLog metadata where available, else `'direct'`).
- At least one `CancellationPolicy` and one `TaxRule` defined and
  attached to the property.
- Reports breakdown by source visibly working on staging.
- Tests pass; no production deploy.


## Phase 2 — One-way Availability / Rate Export Simulation

### Goals
Build the **outbound** machinery (mapping, scheduling, retry) but
publish to a **fake provider only**.

### Deliverables
- Models: `ChannelConnection`, `ChannelRoomMap`, `ChannelRatePlanMap`,
  `SyncJob`, `SyncLog`, `ChannelInventorySnapshot`.
- Admin pages:
  - `/admin/channels/` — list connections.
  - `/admin/channels/<id>` — detail with mapping, sync queue, log.
  - `/admin/channels/<id>/maps` — RoomType / RatePlan mappings.
  - `/admin/channels/sync-queue` — job queue + dead-letter.
- Provider abstraction in `app/services/channels/`:
  - `base.ProviderClient` interface — abstract methods for
    `publish_inventory(date_range, room_type)`,
    `publish_rates(date_range, rate_plan)`,
    `publish_restrictions(date_range, room_type)`.
  - `fake.FakeProvider` — writes payloads to a local table for
    inspection, simulates 200 / 429 / 5xx based on a config flag.
- Async job runner. **Recommendation for first iteration:**
  DB-backed cron + a `flask channels run-pending` CLI command driven
  by a `crontab` entry. Avoids the operational complexity of
  Celery/RQ at this scale. Re-evaluate after 30 days of traffic.
- Event hooks: every `Booking` create / cancel / modify enqueues a
  targeted `SyncJob` for the affected dates. Every `RateOverride` /
  `RateRestriction` / `RatePlan` save enqueues a job too.
- Tests: hammer the FakeProvider with concurrent jobs; assert no
  duplicate publishes, no missed events, no inline calls in the
  request path.

### Exit gate
- Owner can see, on staging, every booking + rate edit fire a
  SyncJob.
- The SyncLog page shows the full job timeline.
- A failing FakeProvider triggers exponential backoff and ends in
  the dead-letter queue cleanly.
- Zero real OTA traffic.


## Phase 3 — Sandbox Reservation Import

### Goals
Build the **inbound** machinery against the FakeProvider, then
swap in the real OTA's **sandbox** endpoint.

### Deliverables
- `ChannelReservation` model with the (channel_code,
  external_reservation_id) unique index.
- Inbound polling worker for FakeProvider — reads pre-canned
  payloads from the FakeProvider table; promotes to the real OTA
  sandbox once the local pipeline is stable.
- Import flow:
  1. Pull payload.
  2. Idempotency check (unique index).
  3. Re-run `services.inventory.check_bookable()` — if not
     bookable, status `failed_overbook`.
  4. Otherwise create `Booking` with `source='channel_<code>'`
     and `external_reservation_id` set.
  5. Auto-create `BookingGroup` if payload bundles multiple rooms.
  6. Write `channel.reservation_imported` audit row.
- Manual-review queue page at
  `/admin/channels/<id>/manual-review`.
- Front Office surfaces the channel badge on booking detail +
  reservation board.
- Hardening of `services.booking_engine.create_direct_booking()`
  to take a row-lock on `Room` for the duration of the create.
- Booking.com sandbox account configured; one full happy-path
  reservation imported end-to-end.

### Exit gate
- 50 sandbox reservations imported across happy + edge cases:
  overbook, modification, cancellation, multi-room, currency
  variation, restriction violation. All land in the right state
  (created / manual_review / failed_overbook).
- Manual-review playbook documented (one page).
- Zero real OTA production traffic.


## Phase 4 — One OTA Pilot Integration (Booking.com)

### Goals
Promote one channel from sandbox to production. ONE channel, ONE
property.

### Deliverables
- Production credentials provisioned via secret manager.
- Soft-launch flag — start by publishing **rates + restrictions
  only**, NO inventory. This means the OTA shows our pricing but
  cannot accept bookings yet (use OTA's "set inventory to 0"
  config). Buys us 1–2 weeks of safe rate-publish traffic.
- After 14 days of clean rate publish: turn on inventory. Now
  the OTA can sell.
- After 7 days of clean live bookings: scale up the publish window
  from 30 days → 365 days.
- Per-channel runbook:
  - How to disable the channel in 60 seconds.
  - How to handle a stuck SyncJob.
  - How to handle a manual-review queue entry.
  - Owner contact at the OTA + ticket portal URL.
- Daily ops dashboard: queue depth, job success rate, last-publish
  timestamp per channel, manual-review queue size.

### Exit gate
- 30 consecutive days of incident-free operation:
  - No overbook.
  - No charged-twice events.
  - Manual-review queue size never above 3 for more than 4 hours.
  - At least one full revenue cycle (booking → check-in → checkout
    → reconcile) for an OTA-imported reservation.
- Owner can articulate, in 1 sentence each, the six "what if"
  scenarios from Section 7 of the architecture doc.


## Phase 5 — Cancellation, Modification, VCC, No-Show

### Goals
Round out the full reservation lifecycle. This is the phase where
we add complexity that Phase 4's "happy path only" deferred.

### Deliverables
- Modification handling: idempotent re-application of dates /
  guests / notes; auto-route to manual-review on availability
  conflict.
- Cancellation handling:
  - OTA cancellation → Booking.status = 'cancelled' +
    cancellation-fee FolioItem if policy demands it.
  - Cancellation reconciliation report: "OTA says cancelled, our
    Booking is checked-in" exception list.
- No-show:
  - Auto-detect when a Booking with arrival = today AND status =
    'confirmed' has not been checked in by 23:59 of business date.
  - Apply no-show fee per `CancellationPolicy.no_show_charge_percentage`.
  - Operator approves the auto-charge before posting.
- VCC support:
  - Charging via the OTA's tokenized endpoint.
  - Scheduled job triggered N days before arrival per VCC
    activation date.
  - Charges land in CashierTransaction with `metadata.source =
    'channel_<code>_vcc'`.
- Refunds:
  - Operator-triggered through the existing cashiering refund
    flow; auto-refund is NEVER a default.

### Exit gate
- All four lifecycle scenarios demonstrably work end-to-end on
  production:
  - guest cancels free-window: OTA cancels, our Booking cancels,
    no fee.
  - guest cancels late-window: OTA cancels, our Booking cancels,
    fee posted to folio, VCC charged.
  - guest no-shows: auto-detected, operator approves, fee posted.
  - guest modifies dates inside availability: applied.
  - guest modifies dates outside availability: manual-review.


## Phase 6 — Additional Channels

### Goals
Add Agoda, then Expedia. Airbnb still excluded (not viable as direct
integration without channel-manager certification).

### Deliverables
- Agoda provider client + sandbox account + 30-day pilot identical
  to Phase 4 cycle.
- Expedia provider client + sandbox account + 30-day pilot.
- Cross-channel rate parity check: a daily report flagging any
  discrepancy between the rate published by us per channel.
- Metasearch / direct-booking widget (Booking.com Connect's "Book
  Direct") evaluation — out of scope but worth tracking as Phase 7.

### Exit gate
- Two channels live with no overbook events for 30 days.
- The "disable one channel without breaking another" kill switch
  has been exercised at least once (planned outage drill).


## Phase 7+ — Out of Scope but on the Radar

| Feature | Why deferred |
|---|---|
| Airbnb (proper) | Requires Airbnb-approved channel-manager status |
| GDS (Sabre/Amadeus) | Not justified for a single Maldivian guesthouse |
| Revenue management / dynamic pricing | Operator owns rates today; CM is publish-only |
| Group rate / contract rate publication | OTA group APIs are inconsistent |
| Multi-property fleet | Single property today |
| OTA review pull / response | Reputation management is a separate sprint |


## Phase Schedule (rough, calendar-time)

> Estimates assume one engineer full-time on the CM. They are
> deliberately conservative — every "this is just config" item in
> OTA work has bitten teams hard.

| Phase | Duration | Calendar gate |
|---|---|---|
| 0 — sign-off | 1–2 weeks (mostly OTA sandbox provisioning) | Launch when OTA sandbox arrives |
| 1 — source/cancellation/tax foundation | 2–3 weeks | All migrations green; reports filter by source |
| 2 — outbound simulation | 3–4 weeks | FakeProvider passes; no inline OTA calls |
| 3 — sandbox import | 3–4 weeks | 50 sandbox reservations imported clean |
| 4 — Booking.com pilot | 6–8 weeks (soft-launch + 30-day pilot) | 30 incident-free days |
| 5 — cancellation/VCC/no-show | 4–6 weeks | All lifecycle scenarios on prod |
| 6 — Agoda + Expedia | 6–10 weeks (overlapping) | Both live, parity report green |

Total: roughly **6–9 months** to "two channels live, full lifecycle,
zero incidents." Anyone promising less is selling something.


## OTA Recommendation: Booking.com First

### Why Booking.com over Agoda / Expedia / Airbnb

| Criterion | Booking.com | Agoda | Expedia | Airbnb |
|---|---|---|---|---|
| Maldives traffic for guesthouse segment | ★★★★★ | ★★★★ | ★★★ | ★★ |
| API documentation quality | ★★★★★ | ★★★ | ★★★★ | n/a |
| Sandbox availability | yes | yes | yes | gated |
| Direct-integration viability | yes | yes | yes | **no** (channel-manager only) |
| Webhook support | yes (Connect API) | partial | partial | n/a |
| VCC support | yes (mature) | yes | yes | n/a |
| Onboarding pain (calendar weeks) | 4–8 | 6–10 | 6–10 | n/a |
| Maturity of Maldives partner support | high | medium | medium | low |

Booking.com is the highest-traffic OTA for our property type, has the
most mature direct-integration story, and the cleanest sandbox /
webhook flow. Airbnb is structurally not viable as a first
integration without becoming an Airbnb-approved channel manager (we
are not).

After Booking.com is stable for 30 days, **Agoda** is the second
target — it has the second-highest Asia-Pacific traffic for our
segment. Expedia goes third because the Expedia Group network
(Expedia, Hotels.com, Vrbo) is significant but the
Maldives-guesthouse share is smaller than Booking.com or Agoda.


## Reference Build Order Summary

```
Phase 0  ──► Phase 1 (PMS prereqs) ──► Phase 2 (outbound simulation)
                                              │
                                              ▼
Phase 3 (sandbox import) ──► Phase 4 (Booking.com pilot, soft-launch)
                                              │
                                              ▼
Phase 5 (cancellation/VCC) ──► Phase 6 (Agoda → Expedia)
```

Every phase is a real project. Skipping any of them — particularly
Phase 1 and Phase 2 — guarantees an incident in Phase 4.

---

*Document owner: PMS architecture. Last updated: 2026-04-30.*
