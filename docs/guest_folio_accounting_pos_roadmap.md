# Guest Folio + Accounting + POS Roadmap

> **STATUS: PHASE 1 (Guest Folio V1) IMPLEMENTED ON `feature/guest-folio-v1`.**
>
> Phases 2+ remain planning only. The Folio model, migration, manual-charge
> form, void flow, audit logging, and tests are all built. Production has
> not been deployed yet — branch is local + pushed to origin only.
>
> Author: roadmap drafted 2026-04-29. V1 status section added same day.
> Review owner: Sheeza Manzil operator.

---

## V1 STATUS — what is implemented now

**Implemented on branch `feature/guest-folio-v1` (off main `3372dcf`):**

- `FolioItem` model in `app/models.py` with all 21 spec fields, 4 indexes,
  5 FKs, signed `total_amount`, and `is_voided` / `is_open` properties.
- Hand-written Alembic migration
  `migrations/versions/d8a3e1f29c40_add_folio_items_table.py` — creates
  ONLY the `folio_items` table + indexes/FKs. Downgrade drops only
  that table. No existing tables are altered.
- Pure helper module `app/services/folio.py` with `ITEM_TYPES`,
  `STATUSES`, `SOURCE_MODULES`, `validate_folio_item`,
  `normalize_folio_item_type`, `signed_total`, `get_open_folio_items`,
  `folio_balance`, `calculate_folio_totals`, `display_folio_item_label`.
- Admin-only blueprint `app/routes/folios.py` with two POST routes:
  - `POST /bookings/<id>/folio/items` — add a folio item
  - `POST /bookings/<id>/folio/items/<id>/void` — void an open item
  No DELETE endpoint exists; voiding is the only undo mechanism.
- Booking-detail page integration (`app/templates/bookings/detail.html`)
  with a Guest Folio panel: totals summary, item table with status
  badges, void buttons, and an admin add-item form (collapsed by default).
- ActivityLog actions: `folio.item.created`, `folio.item.voided` with a
  strict metadata whitelist (booking_id, booking_ref, folio_item_id,
  item_type, source_module, amount, status, voided).
- Tests: `tests/test_folio.py` with 53 tests covering model insertion,
  enum whitelists, validator rules, signed-total math, balance
  calculation, route auth (anonymous/staff/admin), validation, audit
  metadata privacy, void rules (already-voided rejection, wrong-booking
  404), no-DELETE-route AST guard, migration shape.
- Full test suite green: **343 / 343 passing** locally.

**Intentionally deferred to later phases:**

- Auto-posting room nights as folio items (Phase 4 — see §4.1 below for
  the double-counting rule)
- Linking folio payments to `Invoice.amount_paid` (Phase 4)
- Accounting reports by category (Phase 5)
- PDF guest statements (Phase 3)
- Restaurant POS web app (Phase 6)
- Online menu / QR ordering (Phase 7)
- Kitchen Display Screen (Phase 8)

**Accounting integration warning (BINDING):**

V1 is **additive only** — `Booking.total_amount` continues to be the
sole source of truth for room revenue. No existing accounting report
reads `folio_items` yet. This avoids any risk of double-counting room
revenue. Phase 4 will introduce the migration path; until then, folio
revenue must be reported separately from booking revenue.

**Deployment note:**

This feature **requires a database migration** (`d8a3e1f29c40`). Phase A
backup → Phase C `flask db upgrade` → Phase D verification. No env-var
changes; no `requirements.txt` changes.

---

## 0. Why this exists

Sheeza Manzil's app today is a **booking + invoice + AI-assisted comms**
platform. The next horizon is to evolve it into a **full online
property-management / accounting / POS system** for a small Maldivian
guesthouse, suitable for running on phones and tablets, with no printer
required.

Two business outcomes drive the roadmap:

1. **Charge guests during their stay.** Today the app handles
   pre-arrival booking + payment. There is no clean way to add laundry,
   transfer, restaurant, or excursion charges to a stay and bill them
   on checkout.
2. **Run the restaurant online.** A guesthouse-scale restaurant should
   not need a physical receipt printer or POS terminal. Bills should be
   PDF, shareable digitally; the staff "POS" should be a web app on a
   phone or tablet.

This document is the planning bridge between today's booking app and
that target architecture. It is intentionally split into **8 phases**
so each can ship and earn revenue improvement on its own — no big-bang.

---

## 1. Guest Profile / Guest Portfolio

The current `Guest` model carries name, phone, email, ID document fields
and is one-to-many with `Booking`. A **Guest Portfolio** view layers a
unified history on top of that without changing the model:

### 1.1 Goals

- One screen showing everything we know about a guest
- Stay history and lifetime value at a glance
- Notes the front desk can rely on for repeat guests
- Document references (passport, ID) clearly tracked

### 1.2 Information dimensions

| Dimension | Source |
|---|---|
| Identity | `Guest.first_name`, `last_name`, `email`, `phone`, `nationality`, `passport_number` |
| Stay history | `Booking.*` filtered by `guest_id`; oldest → newest, totals per stay |
| Lifetime value | sum of `Booking.total_amount` and `Invoice.amount_paid` |
| Preferences (NEW, free-text) | room preference, allergies, late-checkin habits, dietary notes |
| Notes (NEW, append-only) | per-stay or lifetime — written by admins, visible to staff |
| Document refs | `Booking.id_card_filename`, link to the latest valid ID upload |
| Past invoices | `Invoice.*` joined via Booking |
| Past communications | `WhatsAppMessage.*` filtered by `guest_id` |
| Activity timeline | `ActivityLog.*` rows that touched any of this guest's bookings |

### 1.3 New fields likely needed

These are **proposed**, not built:

- `Guest.preferences_json` — free-text JSON blob; small, no schema lock-in
- `Guest.notes_text` — append-only text area
- `Guest.is_repeat` — derived flag (count of completed bookings ≥ 2),
  could also be a property
- `Guest.lifetime_total_mvr` — derived; can compute on read or
  cache later if expensive

Keep schema migrations small. Most "portfolio" UI can be built without
any new column at all — just join on `guest_id`.

### 1.4 UX placement

- New route: `GET /guests/<int:guest_id>` — admin-only profile page
- Card layout: identity → stay timeline → outstanding balance →
  preferences → notes → documents → past WhatsApp → past activity
- Make this the canonical "double-click on a guest" landing page

---

## 2. Guest Folio (the central running account)

### 2.1 Concept

A **folio** is the guest's running tab during a stay. Every chargeable
event — room nights, laundry, restaurant items, manual adjustments,
payments — lands as a `FolioItem`. The folio's net balance is the
guest's outstanding debt at any moment.

> **Why a separate model and not just more `Invoice` rows?**
> Invoices are *closed* documents — once issued, line items shouldn't
> change retroactively. A folio is the *live* ledger during the stay.
> At checkout we snapshot folio items into one or more invoices.

### 2.2 Proposed `FolioItem` shape

```python
class FolioItem(db.Model):
    __tablename__ = 'folio_items'

    id              = Integer  PK
    booking_id      = FK -> bookings.id (NOT NULL, CASCADE on delete? probably SET NULL + audit)
    guest_id        = FK -> guests.id   (NOT NULL — for portfolio queries)
    invoice_id      = FK -> invoices.id (NULLABLE — set when item is rolled into an invoice)
    created_at      = DateTime, default=utcnow
    posted_at       = DateTime, default=utcnow  # when the charge actually happened
    posted_by_user_id = FK -> users.id (NULLABLE for system-posted)

    # Type taxonomy — frozen list, see below
    item_type       = String(30)  # see _ITEM_TYPES enum

    description     = String(255)  # human label
    quantity        = Numeric(10,2)  default 1
    unit_price      = Numeric(12,2)  # MVR
    amount          = Numeric(12,2)  # quantity * unit_price (snapshot, NOT computed)
    tax_amount      = Numeric(12,2)  default 0
    service_charge_amount = Numeric(12,2) default 0
    total_amount    = Numeric(12,2)  # amount + tax + service charge

    # Lifecycle
    status          = String(20)  # 'open' | 'invoiced' | 'paid' | 'voided'
    voided_reason   = String(255) NULL
    voided_by_user_id = FK -> users.id NULL

    # Provenance
    source_module   = String(20)  # 'booking' | 'pos' | 'manual' | 'system' | 'import'
    source_ref      = String(64)  # POS order id, manual ref, system rule id, etc.

    metadata_json   = Text NULL    # tax breakdown, POS receipt id, etc.
```

### 2.3 `item_type` enum

- `room_charge` — auto-posted on stay nights or rate adjustments
- `restaurant` — POS-posted
- `laundry`
- `transfer` — speedboat / domestic flight
- `excursion`
- `fee` — late checkout, damage, smoking penalty
- `discount` — negative `total_amount`
- `payment` — negative `total_amount` (reduces folio balance)
- `adjustment` — manual admin correction (positive or negative)

### 2.4 Indexes

- `(booking_id, posted_at)` — folio rendering
- `(guest_id, posted_at)` — portfolio queries
- `(invoice_id)` — invoice rollup
- `(status)` — open / unpaid reports

### 2.5 Status transitions

```
open ──[admin invoices stay]──> invoiced ──[payment matches]──> paid
  └──[admin voids before invoicing]──> voided
```

Once `invoiced`, items cannot be edited — they belong to the closed
invoice. Voids after invoicing must go through a refund / credit
adjustment that creates a NEW negative folio item.

### 2.6 Folio balance calculation

```
folio_balance(booking) =
    sum(total_amount for status in ('open','invoiced')) -
    sum(abs(total_amount) for item_type == 'payment')
```

The room nights themselves are NOT silently double-counted with the
existing `Booking.total_amount` — see **Section 4 ("avoid double
counting")** for the rule.

---

## 3. Charge Posting

### 3.1 Form-based manual posting

A "Post charge" form on the booking detail page (admin/staff gated):

| Field | Notes |
|---|---|
| item_type (select) | restaurant, laundry, transfer, excursion, fee, discount, adjustment |
| description (text) | required |
| quantity (number) | default 1 |
| unit_price (MVR) | required |
| tax % (number) | from configured GST rate |
| service charge % | from configured service-charge rate |

Server computes `amount`, `tax_amount`, `service_charge_amount`,
`total_amount` and snapshots them — never recompute on display.

### 3.2 System-posted charges

- **Room nights**: posted automatically when the booking transitions to
  `confirmed`, with one folio item per night, `item_type='room_charge'`,
  `source_module='system'`. (Or one summary item per stay — to be
  decided based on reporting needs.)
- **Late-checkout fee**: posted by a daily cron when a stay overshoots
  its `check_out_date` without a status change.
- **POS-posted**: see Section 6.

### 3.3 Permission model

| Role | Can post | Can void open | Can void invoiced | Can refund |
|---|---|---|---|---|
| staff (front desk) | yes (manual + transfer + laundry) | yes | no | no |
| admin | all | yes | via credit adjustment | yes |
| system | yes (room/late fees) | n/a | n/a | n/a |

Every post + void must write an `ActivityLog` row with safe metadata
(no card numbers, no names beyond what's already on the booking).

---

## 4. Accounting Integration

### 4.1 Avoiding double-counting room revenue (CRITICAL)

The existing `Booking.total_amount` already represents the booked room
charge. Posting room nights as folio items risks double-counting.
**Rule (proposed):**

- Phase 1–2 (folio model + UI only): folio is **purely additive** —
  it represents *extras only*, not room nights. The room charge stays
  on the Booking. Folio balance reads:
  `extras_total = sum(folio_items where item_type != 'payment')`
  `total_due  = booking.total_amount + extras_total - payments_received`
- Phase 4+ (when payment allocation arrives): room charges become
  folio items and `Booking.total_amount` becomes a *derived* read of
  `sum(room_charge folio items)`. Old bookings auto-backfill on first
  read.

This keeps the V1 ship safe — no risk of suddenly overstating revenue.

### 4.2 Folio → Invoice rollup

At checkout (or any explicit "issue invoice" admin action):

1. Snapshot all `status='open'` folio items for the booking
2. Create or update an `Invoice` row
3. For each folio item, set `invoice_id`, transition `status='invoiced'`
4. Invoice total = `sum(folio_items.total_amount)` for that invoice
5. Lock invoice — subsequent edits forbidden

### 4.3 Payments

Payments in the existing system are tracked on `Invoice.amount_paid`.
With folio:

- A payment posts a folio item with `item_type='payment'` and a
  **negative** `total_amount`.
- Optionally also reflected on `Invoice.amount_paid` for backward
  compatibility until accounting reports are migrated.
- Allocation: Phase 4 introduces `Payment` model with explicit
  `allocations(folio_item_id, amount)` for partial payments and
  multi-invoice settlements.

### 4.4 Outstanding balance

Per-booking and per-guest views:

- Booking outstanding = `folio_balance(booking)` (see 2.6)
- Guest lifetime outstanding = sum across all unsettled bookings

### 4.5 Revenue reports

Group by `item_type`, optionally crossed with date and `source_module`:

- "MVR revenue by category, last 30 days" — table + bar chart
- "Restaurant revenue trend" — line chart
- "Outstanding by guest" — flag long-overdue tabs
- All reports must respect the **double-counting rule** in 4.1
  during the transition window

### 4.6 Export

- CSV export of folio items by date range (admin-only)
- PDF export of guest statement (Section 5)
- Future: integrate with QuickBooks / Xero via CSV (out of scope here)

---

## 5. PDF Billing

### 5.1 Goals

- Generate a **guest statement** PDF showing all folio items + payments
- Generate an **invoice** PDF for closed invoices
- No printer required — share digitally (WhatsApp, email)

### 5.2 Library choice (proposed)

- `weasyprint` — HTML/CSS → PDF, easy templating from existing
  Jinja templates. Heavy install (Cairo etc.) but the cleanest output.
- `reportlab` — pure-Python, faster install, but no HTML templating.
- `xhtml2pdf` — middle ground.

Recommendation: WeasyPrint because we already render hospitality docs
in Jinja and the design parity matters to a small operator's brand.

### 5.3 Output contents

| Section | Source |
|---|---|
| Header — Sheeza Manzil branding | static asset |
| Guest block | Guest model |
| Stay block | Booking dates, room, nights |
| Itemized charges | folio items (open + invoiced) grouped by category |
| Payments received | folio items where item_type='payment' |
| Subtotals | computed |
| Tax + service charge breakdowns | computed |
| Grand total + balance due | computed |
| Footer — bank details | the existing `payment_instructions.py` block |

### 5.4 Distribution

- Download button on booking detail page (admin)
- "Send via WhatsApp" — uses existing `send_text_message` wrapper +
  Meta Cloud API media upload (Phase 3+, may need PR-specific Meta
  template approval for documents)
- Email (only when SMTP support is added — currently no SMTP code)

### 5.5 Privacy

- PDFs are generated on demand, never persisted to disk in production
  (stream → response)
- If we later want to attach to an outgoing email, persist to R2 with
  a signed expiring URL, **never** to local disk

---

## 6. Restaurant POS (later)

### 6.1 Scope

A **web app POS** — usable on a phone or tablet, no native code, no
hardware printer:

- Touch-friendly menu grid
- Categories (breakfast / mains / drinks / desserts / specials)
- Items with prices + tax/service-charge config
- Order builder: add to current order, modify quantity, add notes
- Settle: charge to a stay (folio) OR settle directly (cash/card/online)
- Bill output: PDF + WhatsApp share

### 6.2 Data model sketch (NOT to build now)

```python
class MenuCategory:
    id, name, sort_order, is_active

class MenuItem:
    id, category_id, name, description,
    price, tax_pct, service_charge_pct,
    image_url, is_active, available_from_time, available_to_time

class POSOrder:
    id, created_at, opened_by_user_id, table_number,
    booking_id (NULLABLE — null when walk-in),
    status: 'open'|'sent_to_kitchen'|'ready'|'served'|'settled'|'voided',
    settled_at, payment_method ('charge_to_room'|'cash'|'card'|'online')

class POSOrderItem:
    id, order_id, menu_item_id, quantity, unit_price_snapshot,
    notes (e.g., "no onions"), kitchen_status, voided_at, voided_reason
```

### 6.3 Charge-to-room integration

When `payment_method == 'charge_to_room'`:

1. Resolve booking from `POSOrder.booking_id` (admin selects from a
   list of currently checked-in guests — no free-text input)
2. Settle the order: `POSOrder.status='settled'`
3. Post one `FolioItem(item_type='restaurant')` per order, NOT per item
   (keeps the folio readable). The `metadata_json` carries the order
   item breakdown for audit / future detail view.

### 6.4 No printer

- Kitchen sees orders on a Kitchen Display Screen (KDS) at a fixed
  tablet — no printout
- Guest receipt is a PDF, sharable via WhatsApp, optionally emailed
- Cash drawer reconciliation is a daily admin report, not a per-receipt
  process

### 6.5 Offline / weak internet

This is a Maldives guesthouse — connectivity matters.

- Phase 6 ships ONLINE-only POS. Acceptable for v1.
- Phase 6.5 (deferred): IndexedDB queue for orders posted while offline,
  drained when connection returns. Item prices snapshotted at order
  time so price drift doesn't surprise the guest.

### 6.6 Permissions

- Server / waitstaff: open orders, add items, send to kitchen
- Admin: void / refund, edit menu, daily reports
- Kitchen: view incoming orders, mark items ready
- Guest (Phase 7): view their menu via QR, place orders into their
  current stay

---

## 7. Online Menu / QR Ordering (later)

### 7.1 Flow

1. Guest scans a per-table QR code → `https://app.sheezamanzil.com/menu?t=<table>`
2. Phone-friendly menu page loads (no login)
3. Guest browses, builds an order, optionally types a name + room number
4. Submit → order lands in POS as `status='pending_staff_confirm'`
5. Staff confirms on the POS tablet → posts to kitchen + folio
6. Guest gets WhatsApp-deliverable PDF receipt at end of meal

### 7.2 Authentication

QR menus are **public** but rate-limited and CAPTCHA-gated for ordering.
Order confirmation is staff-side — guests cannot self-confirm.

### 7.3 Risks specific to this flow

- Menu spam: rate limit submissions per QR / IP
- Wrong room number: staff must visually confirm guest at pickup or
  before charging to a room
- Off-property orders: detect by IP / Wi-Fi network, optionally only
  accept orders from on-property Wi-Fi

### 7.4 No printing

The online menu is web-native. Item images stored in R2. No PDFs
printed in-house. The kitchen tablet renders the order live.

---

## 8. Suggested build phases

| # | Phase | Deliverable | Approx. effort |
|---|---|---|---|
| 1 | Folio model | `FolioItem` model + migration + manual-charge POST route + audit log | 1–2 days |
| 2 | Folio display | Booking-detail page shows folio table + running balance | 0.5–1 day |
| 3 | PDF guest statement | WeasyPrint integration + statement template + download route | 1–2 days |
| 4 | Payment allocation | Explicit `Payment` rows + allocation against folio items + outstanding view | 1–2 days |
| 5 | Accounting reports | Revenue-by-category, outstanding-by-guest, CSV export | 1–2 days |
| 6 | Simple POS web app | Menu CRUD + order builder + settle to folio | 3–5 days |
| 7 | Online menu / QR | Public menu page + QR generation + staff confirmation flow | 2–3 days |
| 8 | Kitchen / order workflow | KDS view + item-status transitions + daily POS report | 1–2 days |

Each phase is independently shippable. Phases 6–8 are a separate
mini-project gated on Phases 1–5 being live and stable.

---

## 9. Risks

### 9.1 Tax / service-charge config

- GST rate, service charge % — must be **configurable**, not hardcoded
- New `app/config/tax_settings.py` or DB-backed settings — TBD
- Folio item snapshots tax/SC at post time so a future config change
  doesn't retroactively rewrite history

### 9.2 Avoid double-counting room revenue

- Phase 1–2 keeps folio additive (extras only); see §4.1
- Phase 4+ migrates room charges into folio safely
- A single accounting period must NEVER count a room night twice

### 9.3 Permissions

- Staff vs admin role split (already in `User.role`)
- New "kitchen" role likely needed at Phase 8
- Void / refund must always require admin

### 9.4 Void / refund rules

- Open items: free void by poster + supervisor
- Invoiced items: cannot edit; create credit adjustment instead
- Refunds: admin-only; require external payment-rail action +
  audit row

### 9.5 Audit logging

- Every post / void / refund / invoice issuance writes ActivityLog
- Metadata whitelist:
  `folio_item_id, booking_id, guest_id, item_type, total_amount, status,
   actor_user_id, source_module`
- Description ≤ 500 chars, NEVER includes guest payment details

### 9.6 Offline / weak internet

- Phase 1–5: online-only is fine (admin tasks, not real-time ordering)
- Phase 6+ POS: ship online-only first; add IndexedDB queue at 6.5

### 9.7 PDF formatting

- WeasyPrint quirks: PDF page-break behavior with long folio tables,
  font embedding for Dhivehi support, image scaling
- Build a snapshot PDF during Phase 3 and review with the operator

### 9.8 Data privacy

- Folio rows hold no card numbers (PCI scope avoided by routing card
  payments through external gateway later)
- Guest PDFs may include passport-derived names — never include the
  passport NUMBER in any PDF
- Bank details on PDFs are the public business account — no risk
- Guest statements served over HTTPS only; never logged in journal
  or audit

### 9.9 Existing-data backfill

- When folio launches, existing closed bookings have no folio rows.
  Create a one-off script (NOT a migration) that backfills room-night
  folio items with `source_module='import'`, `status='paid'` for
  invoiced bookings. Verify totals match before flipping
  `Booking.total_amount` to derived (Phase 4).

### 9.10 Reversibility

- Each phase ships behind a feature flag where possible
- Folio table can be left empty without breaking the booking app
- POS module is a separate blueprint — disable by unregistering

---

## 10. Recommended first implementation after current AI sprint

**Guest Folio V1** — ship this first. It's the foundation for everything
else and provides immediate operator value (manual charge tracking).

### 10.1 V1 scope

- New `FolioItem` model (full shape from §2.2)
- Hand-written alembic migration (one new table + indexes)
- New blueprint `app/routes/folio.py` with three admin-gated routes:
  - `GET  /bookings/<id>/folio` (renders inside booking detail or a tab)
  - `POST /bookings/<id>/folio/post` (manual charge form)
  - `POST /folio/<int:item_id>/void` (admin-only void)
- Booking-detail page integration: a "Folio" panel showing items + balance
- ActivityLog actions:
  - `folio.item.posted`
  - `folio.item.voided`
- Strict metadata whitelist as in §9.5
- No PDF, no POS, no payment allocation yet
- Tests: ≥ 20 covering model, route auth, validation, audit safety,
  void rules

### 10.2 Out of scope for V1

- Auto-posting room nights (defer to Phase 4)
- Editing folio items (only post + void)
- Payment posting (defer to Phase 4 — payments stay on `Invoice` for now)
- POS / restaurant / online menu (Phases 6–8)
- PDF generation (Phase 3)
- Reports (Phase 5)

### 10.3 V1 success metric

Operator can track non-room charges (laundry, transfer, etc.) per stay
without spreadsheets, and see a running balance during the stay. Audit
log shows who posted what and when. Tests pass; no regressions; nothing
auto-changes status anywhere.

---

## 11. Out-of-scope for this entire roadmap

The following are intentionally **not** in this document and should be
treated as separate planning efforts when the time comes:

- Channel manager integration (Booking.com, Airbnb)
- Direct credit-card capture (PCI scope — use external gateway)
- Multi-property support (Sheeza Manzil is single-property)
- Multi-currency (MVR-only for now)
- Loyalty / rewards program
- Inventory management for restaurant ingredients
- Staff scheduling / payroll
- Housekeeping task management beyond the existing `housekeeping_status`
- Mobile native apps (web-first only)

---

## 12. References to existing code (do NOT modify based on this doc)

- `app/models.py`: `Booking`, `Invoice`, `Guest`, `Room`, `ActivityLog`,
  `WhatsAppMessage`, `User`
- `app/services/audit.py`: `log_activity` — reuse for all folio audits
- `app/services/payment_instructions.py`: bank block — reuse on PDFs
- `app/decorators.py`: `@admin_required` — reuse for all folio + POS
  admin routes
- `app/routes/bookings.py`: existing detail + AI-draft routes — folio
  panel will integrate here in Phase 2
- `migrations/versions/*`: hand-written alembic style preferred —
  keep the same convention for any new folio migration

---

**End of roadmap.** When ready to start, request:

> "Start next feature: Guest Folio V1"

…and a fresh inspection + implementation plan will be produced before
any code is written.
