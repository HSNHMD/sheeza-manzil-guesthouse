# Channel Manager — Risk Checklist (planning only)

> **Purpose:** enumerate every realistic way a channel manager
> integration can hurt the property, with the mitigation that must be
> in place **before** real OTA traffic. Items marked **HARD** are
> non-negotiable launch blockers.

Use this as a pre-flight checklist. If any HARD item is not satisfied,
the integration must stay in sandbox.

---

## Section 1 — Catastrophic Risks (HARD blockers)

### R-01 · HARD · Sell our staging room on real Booking.com
**Scenario:** Sandbox / production credentials get crossed; a test
booking is published to the real Booking.com extranet.
**Mitigations:**
- `ChannelConnection.environment` field (`sandbox` | `production`).
  Defaults to `sandbox`. Promotion to `production` requires an
  explicit admin action with a confirm-by-typed-string guard.
- **Production credentials never live in `.env.staging`.** They live
  ONLY on the production host.
- The outbound HTTP client refuses to call a production endpoint when
  `FLASK_ENV=staging` (or equivalent guard).
- Per-environment unit-test that exercises the cross-environment guard.

### R-02 · HARD · Overbooking
**Scenario:** Two channels confirm two reservations for the same room
on the same night. One guest arrives, finds nobody to receive them.
**Mitigations:**
- Inbound import path runs `services.inventory.check_bookable()` in a
  DB transaction with a per-Room row lock.
- If full → `ChannelReservation.status = 'failed_overbook'`, no
  Booking row is created, and an alert fires.
- We **never** auto-walk a guest. Every overbook failure is an
  operator decision.
- Outbound deltas after every `Booking` create/cancel/modify so OTAs
  see fresh inventory within ≤ 60s.
- Cap on stale availability: any cached number > 5 minutes old is
  resynced before the next outbound publish.

### R-03 · HARD · Charges go to the wrong folio
**Scenario:** OTA reservation gets imported but linked to the wrong
existing booking; charges land on the wrong guest.
**Mitigations:**
- Unique `(channel_code, external_reservation_id)` constraint on
  `ChannelReservation` — duplicate inserts impossible.
- Imported `Booking` always carries `source='channel_<code>'` and an
  `external_reservation_id` — operators can audit.
- Existing anti-double-count rule: every FolioItem belongs to exactly
  one booking (re-asserted by Group Bookings V1 tests).

### R-04 · HARD · Credentials / payment data leaked
**Scenario:** OTA payload is logged verbatim; payload contains a
guest's card number or our API key shows up in logs.
**Mitigations:**
- `SyncLog.request_summary` is **truncated to ≤ 120 chars** at write
  time; full body is NEVER persisted to the DB.
- Credentials live in env vars (or a secret manager); the column
  `credentials_secret_ref` stores only an opaque ID.
- ActivityLog metadata sanitizer (already in place) drops anything
  that isn't a primitive — extends to `channel.*` rows.
- CI step: regex-grep the codebase for likely API key shapes
  (`pk_live_`, `xoxb-`, `AIza…`) before every CM-related deploy.

### R-05 · HARD · PMS unavailable when OTA expects sync
**Scenario:** Our DB or app crashes while an OTA is mid-modification;
state diverges silently.
**Mitigations:**
- All inbound operations are idempotent. Re-running the import for
  the same `external_reservation_id` returns the same Booking and
  bumps `modification_count`.
- OTA-side retries (they all have them) are received cleanly.
- Outbound jobs use `SyncJob.attempt_count` + exponential backoff;
  dead-letter at attempt 5 with operator alert.

### R-06 · HARD · OTA outage takes down our PMS
**Scenario:** Booking.com API hangs at 30s; our /bookings/new request
times out because we tried to publish inline.
**Mitigations:**
- **Zero inline OTA calls** from request handlers.
- Every outbound publish goes through a `SyncJob` worker.
- If the queue is broken, the PMS keeps booking direct customers
  normally — only OTA syncs delay.

---

## Section 2 — Operational Risks

### R-10 · Lost reservations during a sync window
**Scenario:** OTA confirms a reservation at 14:32:01; we don't poll
until 14:35:00; in that 3-minute gap a direct guest snaps up the room.
**Mitigations:**
- Default poll interval ≤ 90 seconds for active hours.
- For OTAs that support webhooks (Booking.com Connect), prefer
  webhook over poll.
- If overbook does occur, it's caught at import time (R-02) and goes
  to manual review — not a silent confirmation.

### R-11 · Cancellation policy mismatch
**Scenario:** OTA's cancellation deadline is "free until 7 days
before arrival" but our internal RatePlan says "free until 14 days."
A guest cancels 10 days out and is refunded fully; the OTA charges us
commission anyway.
**Mitigations:**
- `ChannelRatePlanMap.cancellation_policy_external_id` makes the
  external policy explicit per channel.
- `RatePlan` gains a `cancellation_policy_id` FK + a
  `CancellationPolicy` model (free-window days, percentage forfeit,
  no-show fee). HARD prerequisite — see Section 5.

### R-12 · Modification storm
**Scenario:** Guest fiddles with their reservation 12 times in 2 hours
on the OTA dashboard; we get 12 modification webhooks.
**Mitigations:**
- Idempotent modification handler — every payload reproduces the
  current target state, not a delta.
- `ChannelReservation.modification_count` lets us spot abnormal
  activity.
- If a single `external_reservation_id` mutates more than 5 times
  in 1 hour, route it to manual review.

### R-13 · OTA credit card declined / VCC out of activation window
**Scenario:** Booking imports with a virtual card that becomes
chargeable 3 days before arrival; we forget to charge it; guest
no-shows; we owe the OTA commission and have nothing to bill.
**Mitigations:**
- VCC support is **DEFERRED** until Phase 5+. Until then, all OTA
  bookings are treated as "pay at property" and the operator chases
  the OTA via their dashboard.
- When VCC ships: a scheduled job reads the VCC activation date from
  the OTA payload, charges via the existing CashierTransaction flow,
  and writes a clear audit row.

### R-14 · Currency mismatch
**Scenario:** OTA sends rates in EUR; we display in MVR; the imported
total is wrong by the spread.
**Mitigations:**
- HARD prerequisite (see Section 5): `Booking.currency` snapshot,
  `Invoice.currency`, `CashierTransaction.currency` all consistent.
- Channel publish uses `RatePlan.currency` literally — never
  auto-converts.
- If the OTA insists on a different currency for display, that's an
  OTA-side setting; our publish is in property currency.

### R-15 · Tax / service charge mismatch
**Scenario:** OTA shows "USD 100 / night incl. tax" but our system
publishes "USD 100 / night excl. tax + 12% GST + 10% service" — guest
arrives expecting different total.
**Mitigations:**
- HARD prerequisite (see Section 5): a tax/service-charge model with
  inclusive/exclusive flag, plus a per-channel "publish inclusive"
  toggle.
- Default behaviour: publish inclusive of all mandatory charges, so
  the OTA-displayed total matches ours.
- Until the tax model exists, we publish only as exclusive and rely
  on operator knowledge — ACCEPTABLE for sandbox-only work.

### R-16 · Stale rate / inventory after RateOverride edit
**Scenario:** Operator edits a RateOverride at 09:00 to bump
high-season nights to USD 850; the next outbound publish doesn't run
until midnight; OTAs sell at USD 600 in the meantime.
**Mitigations:**
- Event-driven publish: any RateOverride / RateRestriction / RatePlan
  edit enqueues a targeted `SyncJob` immediately.
- Operator UI shows "last published" timestamp per channel so they
  can see their change went out.

### R-17 · Group / multi-room reservations
**Scenario:** OTA sends a single reservation that bundles 5 rooms.
**Mitigations:**
- Import each room as its own Booking, link via the existing
  `BookingGroup` table (auto-create a group with code
  `IMPORT-<channel>-<external_id>`).
- The `BookingGroup` is created with billing_mode='master' if the
  payload indicates a single payer; operator can flip.
- Master billing booking selection is initially the first imported
  room; operator can reassign on the group detail page.

### R-18 · Closed-to-arrival on the night a guest tries to move into
**Scenario:** Operator closes Saturday to arrival because of an event;
OTA has cached availability and accepts a Saturday arrival anyway.
**Mitigations:**
- Restrictions publish must run within ≤ 60 seconds of the change.
- On import, we re-run `services.inventory.check_bookable()` which
  enforces CTA — overbook path catches this case.

### R-19 · Refund / chargeback flow
**Scenario:** OTA cancels a booking that was paid via VCC; refund
requirement is unclear; we accidentally double-refund.
**Mitigations:**
- HARD prerequisite for VCC era: explicit refund decision routed
  through cashiering admin, not auto.
- Until VCC ships: refunds are operator-initiated via the existing
  cashiering refund flow.

### R-20 · Test bookings polluting reports
**Scenario:** OTA certification process requires us to make ~20 test
bookings; they end up in /reports/revenue.
**Mitigations:**
- Test bookings carry `source='channel_<x>_sandbox'` and a clear
  reference number prefix.
- Reports V1 ancillary breakdown can filter out sandbox sources
  (small Phase 3 patch on Reports).
- Sandbox bookings auto-cancel after a configurable retention window.

---

## Section 3 — Compliance / Legal Risks

### R-30 · GDPR / data residency
**Scenario:** OTA payload includes EU guest data; we store it on a
non-EU server; we may be in violation of GDPR.
**Mitigations:**
- Audit storage region of the staging + production hosts.
- Add a Data Processing Agreement section to the staff playbook.
- Document the data flow (OTA → PMS → R2 backups).
- Treat passport / ID images already collected via the legacy
  `/submit` flow as a higher-sensitivity tier; CM imports do NOT
  ingest ID images.

### R-31 · OTA Terms of Service
**Scenario:** Booking.com bans direct-bypass behavior (sending guests
the property's direct URL post-booking).
**Mitigations:**
- All OTA-imported bookings are flagged with `source='channel_<x>'`
  so staff don't send them direct-booking discount campaigns.
- Group Bookings + CRM (when it ships) must respect the source flag
  and exclude OTA-originated guests from re-marketing.

### R-32 · PCI scope creep
**Scenario:** We start storing VCC card numbers; we are now
in PCI scope.
**Mitigations:**
- Card data NEVER persisted to the PMS DB. Tokens / vault references
  only.
- VCC integration uses the OTA's tokenization endpoint — we charge
  through their API, never store the PAN.

---

## Section 4 — Operational UX Risks

### R-40 · Front Office can't tell OTA bookings apart at a glance
**Scenario:** Staff confuses an OTA pre-paid VCC booking with a
direct cash booking; charges twice.
**Mitigations:**
- Booking detail surfaces the channel: badge "Booking.com" / "Direct",
  payment timing chip ("VCC charged X days ahead" / "pay at property").
- Reservation Board room rail shows a small channel icon.

### R-41 · Operator can't disable one bad channel without nuking all
**Mitigations:**
- `ChannelConnection.is_active = False` halts all jobs scoped to
  that connection while leaving other channels untouched.
- "Disable channel" is a one-click button on the connection detail
  page.

### R-42 · Operator can't tell why a sync failed
**Mitigations:**
- Per-job admin view: status, attempts, last_error, retry button.
- Per-channel queue page lists `SyncJob` rows newest-first.
- Dead-letter banner on the dashboard (visible to admins) when any
  job has been dead-lettered in the last 24 hours.

### R-43 · Manual review queue grows unbounded
**Mitigations:**
- Daily report on `ChannelReservation.status='manual_review'` count.
- Auto-escalate if a row sits in manual review > 4 hours during
  business hours.

---

## Section 5 — Hard Prerequisites Before Real OTA Sync

These items MUST be satisfied before any non-sandbox traffic. Coding
order is suggestive, not strict, but **all** boxes must be ticked.

| # | Prerequisite | Owner area | Notes |
|---|---|---|---|
| 1 | `Booking.source` column (string, indexed) | core booking | Phase 1 of the build plan. Without this, every report we run is OTA-blind. |
| 2 | `Booking.external_reservation_id` (nullable string, indexed) | core booking | One column to record the OTA's unique id. |
| 3 | `Booking.currency` snapshot column | core booking | Default property currency; OTAs that publish in a different display currency still write the property currency. |
| 4 | `Invoice.currency` column | invoicing | Mirrors `Booking.currency`. |
| 5 | `CancellationPolicy` model + `RatePlan.cancellation_policy_id` FK | rates & inventory | OTA cannot publish a rate plan without a defined cancellation policy. |
| 6 | Tax / service-charge model (rate, inclusive/exclusive flag, applies-to-types) | accounting | Required for accurate publish + import. |
| 7 | Async job runner (`SyncJob` + worker) | infrastructure | Could be DB-backed cron in early phases; Celery/RQ later. Avoid inline calls under all circumstances. |
| 8 | Outbound HTTP wrapper with provider abstraction | services | Per-channel client interface; mockable; rate-limit aware; retry-aware. |
| 9 | Sandbox-only default for new connections | safety | Promotion to production requires explicit admin action. |
| 10 | Secrets store / env-var convention | safety | Credentials never in DB. |
| 11 | Idempotent inbound import (unique constraint on (channel, external_id)) | new tables | Anti-duplicate. |
| 12 | Channel-aware Front Office UI hints | front office | Booking detail + reservation board badges. |
| 13 | Manual-review queue page | new admin UI | Where overbook + conflict cases go to die. |
| 14 | Dead-letter inspection page | new admin UI | One-click retry / dismiss. |
| 15 | Per-channel disable kill switch | new admin UI | One toggle per `ChannelConnection`. |
| 16 | Reports filter by `Booking.source` | reports | "Direct vs OTA share" is the first question every owner asks. |
| 17 | OTA test-booking reconciliation | reports | Filter sandbox traffic out of revenue. |
| 18 | Cancellation/no-show fee flow at the cashiering layer | cashiering | When OTA says "no-show," we charge per the policy. |
| 19 | At least one mock provider that covers the full happy path | testing | Lets us prove the machinery works without touching real OTA APIs. |
| 20 | Documentation: per-channel runbook | docs | Booking.com runbook BEFORE Booking.com goes live, etc. |

---

## Section 6 — Pre-Launch Checklist (when going live with first real OTA)

Print this. Tick every box, or do not promote to production.

- [ ] All HARD blockers (Section 1) resolved + tests in place.
- [ ] Cancellation policy model live + at least one policy attached
      to every active RatePlan.
- [ ] Tax model live + each RatePlan declares whether it publishes
      inclusive or exclusive of tax.
- [ ] `Booking.source`, `Booking.external_reservation_id`,
      `Booking.currency` columns in place + populated for new bookings.
- [ ] Async job runner deployed; dead-letter queue visible to admin.
- [ ] Mock provider passes 100% of integration-style tests.
- [ ] Sandbox integration with the chosen OTA passes end-to-end:
      publish rates, publish restrictions, receive a test reservation,
      receive a modification, receive a cancellation, all written
      cleanly to the PMS with audit trail.
- [ ] Property staff trained on the manual-review queue (read +
      action playbook documented).
- [ ] Rollback plan documented: how to disable the channel, how to
      reconcile imported reservations if we abort.
- [ ] Production credentials stored in a secret manager (not in env
      files committed to the repo).
- [ ] Observability: dashboard for queue depth, job success rate,
      manual-review queue size, last-publish timestamp per channel.
- [ ] On-call rota: who looks at the dashboard the first 14 days.

---

*Document owner: PMS architecture. Last updated: 2026-04-30.*
