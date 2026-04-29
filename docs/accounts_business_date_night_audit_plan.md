# Accounts + Business Date + Night Audit — Design Plan

> **STATUS: PLANNING ONLY — NO IMPLEMENTATION YET**
>
> This document is a forward-looking design. None of the models,
> migrations, routes, or UI described here exist yet. Field names,
> table shapes, route paths, and audit-action vocabularies are all
> open until an explicit "build phase X" approval is given.
>
> The goal is to align on the financial / operational architecture
> *before* writing code, so we don't end up with revenue counted
> twice, daily reports that drift between server clock and operator
> reality, or a night audit that silently mutates state.
>
> Companion doc: `docs/guest_folio_accounting_pos_roadmap.md` (Folio
> V1 already shipped on staging; Phases 2–8 sketched there).
>
> Author: planning notes captured 2026-04-29.
> Review owner: Sheeza Manzil / Maakanaa Village operator.

---

## 0. Why this design exists

Three forces drive this document:

1. **Folio V1 ships on staging** with manual extras only (laundry,
   restaurant, fees, discounts, payments). It's deliberately additive —
   it does NOT post room nights, so it cannot double-count room revenue.
   That decision was correct, but it means we have **no daily room
   revenue capture mechanism** yet. Reports that want "MVR earned
   yesterday" still have to derive room revenue from `Booking.total_amount`
   spread across check-in/check-out dates — which is fragile.
2. **Cashiering doesn't exist.** Payments today land on `Invoice.amount_paid`
   directly. There's no record of *which cashier* posted the payment,
   *which method* was used, *what the reference number is*, or whether
   the cash drawer balanced at end of shift. Real hospitality operations
   need every one of those.
3. **Server clock ≠ business day.** A guest checks out at 02:30 on June 1,
   but operationally that's still "May 31's late checkout" until night
   audit closes May 31. If we report on `created_at >= 2026-06-01`, we
   miscount that revenue. If we report on `business_date == '2026-05-31'`
   *until night audit completes*, we get it right. **Every property
   management system worth using makes this distinction.** We need to
   bake it in before reports are written, not retrofit it.

This document plans **the full chain** — Folio → Cashiering → Business
Date → Night Audit → Reporting → POS — so each piece fits the next.

---

## 1. Guest Folio

The folio is the running tab on a stay. Each chargeable event lands as
one `FolioItem`. Folio V1 (already shipped on staging — branch
`feature/guest-folio-v1`) covers manual extras; this section sketches
how V2 + V3 extend it without breaking V1.

### 1.1 Today (V1, shipped)

`FolioItem` columns currently in production-shaped migration
`d8a3e1f29c40`:

```
id, created_at, updated_at,
booking_id (FK), guest_id (FK), invoice_id (FK nullable),
item_type      ∈ ITEM_TYPES (13 slugs: room_charge, restaurant, …)
description, quantity, unit_price, amount,
tax_amount, service_charge_amount, total_amount,   ← signed
status         ∈ open | invoiced | paid | voided
source_module  ∈ manual | booking | accounting | pos | system
posted_by_user_id, voided_at, voided_by_user_id, void_reason,
metadata_json
```

`total_amount` is stored *signed*: charges positive, payments and
discounts negative. Voided items contribute zero to the balance.
Source-of-truth = `app/services/folio.py::folio_balance()`.

**V1 does NOT auto-post room nights.** `Booking.total_amount` stays
the canonical room-revenue field for now — preserving backward
compatibility with all current accounting reports.

### 1.2 V2 — Refunds, adjustments, partial settlement

These are **already representable in V1's data model** but lack UI:

| Concept | How it's encoded | UI work needed |
|---|---|---|
| **Refund** | `item_type='payment'` with positive amount on a guest who already had a negative payment row. Net contribution to balance is positive (i.e., money returned to the guest). | A separate "Refund" form that pre-fills `item_type='payment'` and inverts the sign. Audit row uses `folio.refund_issued` action so reports can distinguish refunds from new payments. |
| **Adjustment** | Existing `adjustment` item_type, signed. Used for "we credited this guest 200 MVR for the broken AC." | Already has UI in V1; needs a "reason" enum (compensation / correction / loyalty / other) so reports can group adjustments by intent. |
| **Discount** | `item_type='discount'` (always negative). | V1 has UI. V2: support percent-based discounts (e.g., 10% off the room) where the discount amount is computed from a target line at post time and locked. |
| **Partial settlement** | One booking → one or more invoices, each with its own `Invoice.amount_paid`. Folio items get `invoice_id` set when included. | New "Issue partial invoice" admin action that picks a subset of open folio items and creates a closed invoice. Folio items become `status='invoiced'` and a tighter audit row records which items were swept in. |

### 1.3 V3 — Auto-post room nights (the hard one)

This is the migration that lets folio replace `Booking.total_amount`
as the canonical room-revenue source. Done badly, it double-counts;
done correctly, it unlocks daily revenue reports keyed on business date.

**Proposed cut-over rule (binding):**

* For every active booking on every business-date close, night audit
  posts ONE folio item per night just ended:
  ```
  item_type   = 'room_charge'
  source      = 'system'
  description = 'Room night YYYY-MM-DD'
  unit_price  = room.price_per_night (snapshotted)
  quantity    = 1
  amount      = unit_price
  total_amount = amount + tax + service charge
  business_date = the night that just closed (see §3)
  ```
* `Booking.total_amount` becomes a *derived* read of
  `sum(folio_items where item_type='room_charge' for this booking)`
  but **only after a one-shot backfill script** seeds existing bookings
  with their historical room_charge rows. Until backfill runs and is
  verified, Booking.total_amount remains the source of truth.
* Reports gate on a `ROOM_REVENUE_SOURCE` config flag with two values:
  `booking_total` (today) and `folio_room_charges` (post-V3). The flag
  flips the moment the backfill is verified. **Both cannot be true** —
  that's the anti-double-count rule, enforced by code-review and a
  test that asserts only one is referenced in any given report.

### 1.4 Refunds vs. payments — money flow clarity

Hospitality reporting needs to distinguish these four cases:

| Folio row | Net total | Means | Reported as |
|---|---|---|---|
| `payment`, total = -1200 | reduces balance by 1200 | guest paid the property | Income (cash receipt) |
| `payment`, total = +1200 | increases balance by 1200 | property refunded the guest | Refund (negative cash receipt) |
| `discount`, total = -200 | reduces balance by 200 | not a payment; a price adjustment | Discount expense (or contra-revenue) |
| `adjustment`, total = ±X | signed by admin | corrections, comps, errors | Adjustment (separate line in P&L) |

Audit log distinguishes these via separate actions:
`folio.payment_received`, `folio.refund_issued`, `folio.discount_applied`,
`folio.adjustment_made` — even though they all hit the same `folio_items`
table.

### 1.5 Folio balance vs. amount due

* **Folio balance** = `sum(non-voided total_amount)`. Negative balance
  means the property owes the guest (e.g., overpayment). Zero = settled.
  Positive = guest owes.
* **Amount due** = balance ONLY for items where `status in ('open',
  'invoiced')`. Excludes `paid` (already settled) and `voided`. This is
  the number printed on a guest statement asking for payment.

---

## 2. Cashiering

This is the missing layer between *receiving payment* and *recording
it on a folio*. Today, `Invoice.amount_paid` is the only payment record;
who took the payment, by what method, with what reference number, are
all lost. Cashiering V1 fixes that.

### 2.1 The model gap

Right now:
```
Invoice.amount_paid    (Float, default 0)
Invoice.payment_method (String, e.g. "bank_transfer")
```

That's not enough. We need:

* WHO recorded the payment (cashier user_id)
* WHEN, and against which BUSINESS DATE
* HOW (cash, card, bank transfer, online gateway)
* REFERENCE (slip number, last-4 of card, transaction id, etc.)
* SHIFT (which cashier session was open at the time)
* REVERSIBILITY (refunds, voids — see §1.4)

### 2.2 Proposed `Payment` model (V1)

```python
class Payment(db.Model):
    __tablename__ = 'payments'

    id, created_at,                    # row metadata
    business_date,                     # see §3 — flipped only by Night Audit
    booking_id (FK, nullable),         # null for non-booking receipts
    invoice_id (FK, nullable),         # null until allocated
    folio_item_id (FK, nullable),      # set once posted to folio

    method ∈ cash | card | bank_transfer | online_gateway | adjustment_credit
    reference_number (str, optional)   # slip #, txn id, last-4
    amount,                            # always positive — direction is on the type
    direction ∈ incoming | outgoing    # outgoing = refund

    cashier_user_id (FK users.id)      # who pressed save
    shift_id (FK, nullable)            # see §2.4

    status ∈ pending_verification | verified | voided | refunded
    voided_at, voided_by_user_id, void_reason

    notes (str 500, nullable)
```

**Key decision: `Payment` and `FolioItem` are two tables, NOT one.**

* A Payment is a *cash event* — money physically changed hands.
* A FolioItem is a *ledger line* — moves the guest's balance.

When a payment is posted to a folio, the route writes BOTH a `Payment`
row AND a corresponding `FolioItem` row with `item_type='payment'`,
linked via `folio_item_id`. That way:
- Reports about cash flow use `payments` (filter by method, by cashier,
  by shift).
- Reports about guest balances use `folio_items` (filter by booking).
- They cross-reconcile via the `folio_item_id` foreign key.

### 2.3 Payment methods

Initial vocabulary, kept tight:

```
cash               — physical cash, requires drawer reconciliation
card               — POS terminal swipe / tap; reference = last-4
bank_transfer      — local Maldivian bank; reference = slip number
online_gateway     — Stripe / Payhere / similar; reference = txn id
adjustment_credit  — non-cash credit (comp, voucher, loyalty); needs
                     manager override
```

Each method has a different settlement timing and a different
reconciliation flow. Reports group revenue by method so the operator
sees, e.g., "cash: 4 200 MVR, card: 12 800 MVR, bank: 8 600 MVR" at
end of day.

### 2.4 Shift / cashier session (V1.5)

If the property has multiple cashiers (one at front desk, one at the
restaurant POS later), we need a `CashierShift`:

```python
class CashierShift(db.Model):
    id, opened_at, opened_by_user_id, opening_float (cash drawer start),
    closed_at, closed_by_user_id, closing_count, closing_variance,
    business_date,
    notes
```

Each `Payment` carries `shift_id`. Closing a shift requires the cashier
to count the drawer; the system computes expected = opening_float +
sum(cash payments) − sum(cash refunds) and surfaces variance. Audit
row `shift.closed` carries the variance for trend monitoring.

V1 of cashiering can ship without shifts — single-user staging works
fine. Shifts become real when the restaurant opens and there are 2+
cashiers active simultaneously.

### 2.5 Refunds and voids

* **Void a payment** (within same business date, before deposit): sets
  `status='voided'`, removes the corresponding folio_item via void
  flow. Cash drawer adjusts. Treated as if the payment never happened.
* **Refund** (after deposit, or across business dates): does NOT void
  the original payment. Instead creates a NEW payment row with
  `direction='outgoing'`, linked to the original via
  `metadata.refunded_payment_id`. Folio gets a new positive folio_item.
  Both rows show in the audit trail; reports show refund as a separate
  contra-revenue line.

The void/refund distinction matters for tax reporting. Voided payments
disappear from the daily report. Refunded payments stay in the report
but are offset by the refund row.

---

## 3. Business Date

The single most important architectural decision in this document.

### 3.1 The principle

> **Business date is what the operator says today is.
> It changes when the operator says it changes.
> The server clock is irrelevant.**

Why: a guesthouse running 24/7 has events that span midnight. If a
late-departure invoice posts at 01:30 server time, but the operator's
"today" hasn't ended yet (they haven't run night audit), that invoice
must count toward "yesterday's revenue." Filtering reports on
`created_at >= today_midnight()` would put it in the wrong day.

Every PMS handles this. Most call it "audit date" or "business date."
The server clock and the business date are kept in lockstep MOST of
the time, but they diverge for the few hours each night between
midnight and night audit completion.

### 3.2 The data shape

Single-property today, multi-property later. So:

```python
class PropertyState(db.Model):
    """One row per property. Carries the current business date and any
    other property-wide state that doesn't fit elsewhere."""
    id, property_code (str, unique),
    current_business_date,                # the date until night audit closes
    last_night_audit_completed_at,        # actual server clock at last close
    last_night_audit_user_id,
    night_audit_in_progress (bool),       # locks out concurrent runs
    night_audit_started_at, night_audit_started_by
```

Single property: one row with `property_code='sheeza'` (or `maakanaa`
on staging). Bootstrap during the migration that creates the table.

### 3.3 Where business_date is stamped

Every transactional row that participates in daily reports gets a
`business_date` column at write time:

* `folio_items.business_date`
* `payments.business_date`
* `cashier_shifts.business_date`
* `room_blocks` — keep `start_date` / `end_date` (already exist); blocks
  are not transactional in the daily-revenue sense.
* `bookings` — DO NOT add `business_date` to bookings; their `check_in_date`
  already carries the right semantics. The room-charge folio items
  posted by night audit will carry `business_date` correctly.

Stamping happens at the route handler, reading from `PropertyState.current_business_date`,
NOT from `date.today()`. A small helper `current_business_date()` wraps
this lookup; tests can override it.

### 3.4 UI display

Every page that shows "today" needs to display the business date,
not the server clock date:

* Top toolbar of the Reservation Board: "Business date: Wed, Apr 29"
  with a small clock icon if it differs from server clock.
* Reports default to `business_date = current_business_date`. Date
  filters apply to business date, not server date.
* Night audit screen prominently shows the date about to close.
* Activity log entries show both timestamps when they differ
  ("posted at 01:23, business date Apr 29").

Mobile fallback: just "Apr 29" in the top bar — no clock comparison
needed unless explicitly asked.

### 3.5 Why not just call it "today"?

Because today's operator and today's accountant disagree about what
"today" means. The accountant wants the closed-books version; the
operator wants the live-ops version. Naming the field `business_date`
makes it unambiguous in code reviews and reports.

### 3.6 Failure modes to guard against

* **Concurrent night audits.** `night_audit_in_progress` flag taken at
  the start, released only at completion or rollback. Other admin
  attempts to start one get a friendly "Night audit by Hassan in
  progress since 21:14" message.
* **Server clock skew or NTP failure.** Reports use `business_date`,
  not `created_at`. The server clock can be wrong by an hour and
  nothing breaks except the timestamps printed alongside.
* **Operator forgets to run night audit.** Business date sits frozen.
  All transactions that day land on the prior business date — visible
  to the operator in the toolbar pill ("Business date is 2 days
  behind real date"). System never auto-rolls.

---

## 4. Night Audit V1

Night audit is the daily ritual that closes the books, posts room
charges, and rolls business date forward. **It is human-confirmed
and reversible up until commit; everything beyond that point is
permanent and audited.**

### 4.1 The flow (admin-only screen)

1. Admin clicks "Run night audit" from the dashboard.
2. System runs PRE-CHECKS (read-only). Any blocker → cannot proceed.
3. Operator reviews PRE-CHECK warnings (non-blocking issues like
   "guest 5 is still pending payment review" or "expected arrival
   Hassan Demo not yet checked in"). Each warning shows a one-click
   action ("Mark as no-show", "Verify payment now") or a "I've
   acknowledged" toggle.
4. After all warnings resolved or acknowledged, system shows the
   AUDIT REPORT preview: room charges that *would* be posted, daily
   revenue summary, payment summary by method, opening / closing
   balances by cashier shift.
5. Operator types the current business date into a confirmation
   field (literally the date string — defends against muscle-memory
   confirms) and clicks "Close Apr 29".
6. System runs the actual write transaction:
   * Post one `room_charge` folio_item per active booking (V3 only —
     until then this step is skipped, room revenue stays on
     `Booking.total_amount`).
   * Compute the daily-revenue snapshot row (see §6).
   * Increment `PropertyState.current_business_date` by 1 day.
   * Stamp `last_night_audit_completed_at` and `last_night_audit_user_id`.
   * Audit row `nightaudit.completed` with full metadata snapshot.
7. Redirect to a "Night audit complete" page that lets the operator
   download the audit report PDF (V2 of night audit) and confirms the
   new business date in the toolbar.

Total wall-clock target: under 2 minutes for a property of this size.

### 4.2 Pre-checks: blocking vs. warning

**Blocking** (cannot proceed until resolved):

* `night_audit_in_progress = True` already (someone else is running it).
* Any `Payment` with `status='pending_verification'` for the closing
  business date. (Either verify or void before close.)
* Any open `CashierShift` for the closing business date.
* No `current_business_date` set (data corruption / fresh install).
* Server time is more than 6 hours BEFORE the closing business date.
  (Defends against "you're trying to close 2026-05-01 from a server
  that thinks it's 2026-04-30 morning" — usually a clock-skew bug.)

**Warning** (proceed with explicit acknowledgement):

* Expected arrival not checked in by audit time. (Mark no-show?)
* Expected departure not checked out by audit time. (Late checkout?
  System auto-applies a configurable late-checkout fee folio item if
  configured.)
* Folio with positive balance for a checked-out guest. (Outstanding
  amount due; should issue invoice.)
* Folio with negative balance for an active guest. (Overpayment;
  issue refund or apply to future stay.)
* Cashier shift open with > 0 variance against expected.

### 4.3 The audit report

A single page (HTML preview + PDF export V2):

* Property + date being closed
* Room nights posted: count and total revenue
* Folio activity: charges, discounts, payments, adjustments by category
* Payments by method: cash / card / bank transfer / online gateway /
  adjustment-credit
* Cashier shift summary: opening, closings, variances
* In-house tonight: room count, occupancy %
* Arrivals processed today, departures processed today, no-shows
* Comparison vs. yesterday: +/- vs. last business date

This becomes the source for §6 reporting.

### 4.4 Audit log trail

Every step writes an `ActivityLog` row, narrowly scoped:

```
nightaudit.started        — actor, business_date_being_closed
nightaudit.precheck_blocked — for each blocker, one row with safe metadata
nightaudit.warning_ack    — actor acknowledged warning N
nightaudit.completed      — actor, old_business_date, new_business_date,
                            counts (room_nights, payments, folio_items)
nightaudit.aborted        — actor, reason, at which step
```

No real names, slip data, or guest details in metadata — same
whitelist discipline as the existing audit subsystem.

### 4.5 Reversibility

A completed night audit is **immutable**. There is no "undo" button.
What's available:

* Adjustments / corrections in the next business date (e.g., re-post a
  room charge that was missed, with reason="post-audit correction").
* A separate `nightaudit.rolled_back` admin tool that requires a
  multi-line reason, can only roll back the LAST audit, and writes a
  high-priority audit row. Not in V1 — defer until clearly needed.

### 4.6 What night audit explicitly does NOT do (V1)

* Auto-charge no-show fees. (Operator sets the fee manually if they
  want one; defaults to no fee.)
* Auto-issue invoices on checkout. (Phase 4 of folio work.)
* Email or WhatsApp reports anywhere. (Print/download only.)
* Touch payment status. (Pre-checks block the close if anything is
  pending; nothing is auto-verified.)

---

## 5. AI-assisted Night Audit

AI's role here is **summarization and recommendation, never action.**
Every state change goes through a human click.

### 5.1 What AI MAY do

* **Pre-audit summary.** Generate a 3–5 sentence "today in one
  paragraph" — "12 in-house, 3 arrivals, 2 departures, no-shows: 1.
  Cash drawer reconciled clean. One payment for booking BK0042 is
  still pending review." Operator reads this BEFORE the pre-check
  pass; helps catch surprises.
* **Anomaly detection.** Cross-reference today's metrics against
  rolling 30-day averages. Surface things like "Cash receipts today
  4× higher than median — verify." Just a note, not a block.
* **Action suggestions.** When a warning appears ("departure not
  checked out"), AI may suggest "based on prior pattern, this guest
  typically late-checks-out by 14:00 — consider applying the
  configured late fee." But the click stays with the operator.
* **Post-audit narrative.** After close, generate a 1-paragraph
  summary suitable for owner WhatsApp ("Apr 29: 8 nights sold, MVR
  4 800 in revenue, 1 outstanding folio of MVR 600. Click here for
  the full PDF.") — operator chooses to send or not.
* **Report drafting.** Compose the body of email / WhatsApp report
  messages using existing AI draft infrastructure (`ai_drafts.py`).

### 5.2 What AI MUST NEVER do

* **Run night audit autonomously.** No scheduled "AI night audit at
  03:00 every day" — full stop.
* **Write to ANY transactional table.** No folio_items, no payments,
  no booking status changes, no invoice mutations. Every AI route
  is read-only.
* **Mark payments verified.** Even if the AI is "100% sure" the bank
  slip matches the expected amount, the human verifies.
* **Flip business_date.** Only the night-audit confirm flow does that.
* **Bypass pre-check blockers.** AI can summarize blockers; it cannot
  resolve them.
* **Be on the critical path.** If the AI provider is down, night
  audit must still be runnable manually. AI is decoration on the
  human flow, not a dependency of it.

### 5.3 The plumbing

Same provider-pluggable infrastructure as `ai_drafts.py` (Gemini
default, Anthropic alternative). New helper:
`generate_night_audit_summary(property_state, day_metrics)` returns
a result-dict identical in shape to the existing `generate_draft()`.

Privacy contract identical to V1 of AI drafts: prompt never includes
passport numbers, full phone numbers, or guest payment details.
Audit metadata is whitelisted — provider, model, length_chars only;
never the prompt text or AI output body.

### 5.4 Failure modes

* AI provider unreachable → night audit page hides the AI summary
  block, posts a small "AI summary unavailable" line, and the rest
  of the flow works unchanged.
* AI returns empty/garbled output → render placeholder "AI summary
  unavailable" — operator can still complete the audit.
* AI output contains hallucinated numbers → low risk because the
  AI receives ONLY pre-aggregated metrics from server-side queries;
  it has no DB access. But: every number AI prints is rendered with
  a hover tooltip showing "this came from query X" so the operator
  can spot-check.

---

## 6. Accounting / Reporting layer

The reporting layer reads `business_date`-stamped data from
`folio_items` and `payments`. It NEVER reads `created_at`. This is
the discipline that lets reports stay correct across midnight.

### 6.1 Daily revenue snapshot

At night-audit completion, write one row per property per business
date to `daily_revenue_snapshots`:

```
property_code, business_date,
room_revenue,                # from posted room_charge folio items
ancillary_revenue_by_category (json) {
   restaurant: ..., laundry: ..., transfer: ..., excursion: ...,
   goods: ..., service: ..., fee: ..., damage: ..., other: ...
},
discounts, adjustments,
payments_by_method (json) { cash: ..., card: ..., bank: ..., ... },
refunds_by_method (json),
opening_balance, closing_balance,
arrivals, departures, in_house_count,
notes
```

This snapshot is **immutable** once written — same as the audit row.
Reports for older periods read snapshots; reports for the current
(open) day compute live from folio_items.

This is the anti-fragile move: the historical record is durable
even if the underlying folio rows get edited later (which they
shouldn't, but defense in depth).

### 6.2 Standard reports (ordered by build priority)

| Report | Data source | Audience |
|---|---|---|
| Daily summary | snapshot OR live | operator, owner |
| Monthly P&L (revenue side) | snapshots aggregated | accountant |
| Outstanding folios (aging) | live folio_items, by check_out_date age | operator |
| Cashier shift report | live `payments` + `cashier_shifts` | cashier |
| Tax / GST report | snapshots, segmented by `tax_amount` field | accountant |
| Source-of-business | bookings, segmented by referral channel | manager |

### 6.3 Aging buckets for outstanding folios

```
0-7 days:    routine — guest still in-house or just checked out
8-30 days:   needs follow-up
31-60 days:  flag, manual review
60+ days:    write-off candidate
```

Aging report groups outstanding folios into these buckets. Operator
can WhatsApp a payment reminder from each row (using existing AI
draft infrastructure).

### 6.4 Anti-double-counting rules (BINDING)

These are the most important constraints in the entire reporting layer:

1. **Room revenue source is configurable, not both.** Until
   night-audit-room-charge-posting (V3 of folio) is live and backfilled,
   every room revenue report reads `Booking.total_amount` distributed
   over check_in/check_out dates. After the cut-over, every room revenue
   report reads `sum(folio_items where item_type='room_charge')`. Never
   both. A unit test asserts this for every report module.
2. **Payments and folio items track the same money via foreign keys.**
   A `Payment` row creates a `FolioItem`. Reports about cash flow read
   `payments`. Reports about guest balances read `folio_items`. They
   reconcile via `folio_item_id`. No report sums both tables.
3. **Snapshots are read-only.** Once a daily snapshot row is written,
   nothing rewrites it. Corrections happen via adjustment rows in the
   NEXT business date, with a clear reason.
4. **Voided rows are excluded from totals.** Already enforced for folio;
   needs the same discipline on payments.
5. **`Booking.total_amount` is NOT summed for revenue reports until
   the V3 cut-over.** Use the per-night distribution helper instead.

### 6.5 Export formats

* **CSV** for accountant import (per snapshot, columns clearly named).
* **PDF guest statement** (per booking, on demand) — already on the
  Phase 3 roadmap in `guest_folio_accounting_pos_roadmap.md`.
* **PDF night audit report** (per business date, generated at close).

WeasyPrint is the recommended PDF engine; matches the operator's
brand-consistent invoice rendering.

---

## 7. POS integration (later)

When the restaurant POS module ships (Phase 6 of the folio roadmap),
its design must respect the architecture set out here. Three rules:

### 7.1 POS posts to FOLIO, not to ACCOUNTING

Every restaurant order that's "charged to room" creates one
`FolioItem` with:
```
item_type     = 'restaurant'
source_module = 'pos'
source_ref    = pos_order_id     # link back to the POS order
amount        = subtotal
tax_amount    = posted at order time
service_charge_amount = posted at order time
business_date = current_business_date at the moment of post
```

POS does NOT directly write to `daily_revenue_snapshots` or any
reporting table. The single source of truth for "restaurant revenue
on day X" is the sum of `folio_items where item_type='restaurant'
and business_date = X`.

### 7.2 Settled-direct POS payments still go through the cashiering layer

A POS order paid in cash at the till also creates:
- A `Payment` row (method='cash', cashier_user_id, shift_id).
- A `FolioItem` with item_type='payment' linked to the order via
  `metadata.pos_order_id`.

This way the cash drawer reconciliation still includes restaurant
sales, and the daily revenue snapshot picks them up the same way it
picks up room charges.

### 7.3 Folio item, not invoice line

POS orders never create `Invoice` rows directly. Invoices are issued
at folio-rollup time (checkout), when the operator decides to settle.
Some POS orders may settle within a single business date and never
become formal invoices — that's fine. Folio is the ledger; Invoice is
a closure document.

---

## 8. Suggested phased build order

Each phase is independently shippable on staging, audited on staging
for ≥ 1 week of operator usage, then promoted to production after a
manual review checklist.

| # | Phase | Status | Depends on |
|---|---|---|---|
| 1 | **Folio V1** | ✅ shipped on staging | — |
| 2 | **Folio V2** — refunds, adjustments UI, partial settlements | planning | Folio V1 |
| 3 | **Cashiering V1** — Payment model, payment methods, references, cashier_user_id | planning | Folio V1 |
| 4 | **Business Date V1** — PropertyState model, business_date stamp on folio + payments, UI display | planning | Cashiering V1 |
| 5 | **Night Audit V1** — pre-checks, audit report preview, business-date rollover, audit log | planning | Business Date V1 |
| 6 | **Daily revenue snapshot** — write at night-audit close, immutable, report queries | planning | Night Audit V1 |
| 7 | **AI-assisted Night Audit** — summary + anomaly detection + post-audit narrative (read-only) | planning | Night Audit V1 + existing AI drafts |
| 8 | **Folio V3** — auto-post room nights from night audit, deprecate Booking.total_amount as revenue source (with backfill + cut-over flag) | planning | Daily snapshot V1, Night Audit V1 |
| 9 | **Standard reports** — Daily summary, P&L, aging, cashier shift, tax | planning | Folio V3 |
| 10 | **Cashiering V2** — shifts, drawer reconciliation, multi-cashier | planning | Cashiering V1 |
| 11 | **PDF outputs** — guest statement, night audit report | planning | Reports V1 |
| 12 | **POS module** — menu, orders, charge-to-room, kitchen display | planning | Folio V3, Cashiering V2 |
| 13 | **Online menu / QR** | planning | POS V1 |

Phases 2–8 are the financial backbone. Phases 9–13 are operator
delight on top.

### Earliest meaningful operator value
Phase 5 (Night Audit V1) is when the operator first sees a real "close
the day" experience. Even without auto-posted room charges (Phase 8),
the audit still surfaces blockers, locks the business date, and
produces a paper trail. That's already a step change vs. "look at the
bookings list and trust the timestamps."

---

## 9. Open decisions

These need an explicit operator answer before implementation begins:

1. **Night audit cutoff time.** Most properties run audit between
   00:30 and 03:00 local time. Should the system warn the operator
   if night audit runs more than X hours late?
2. **Late checkout fee.** Configurable per-property? Per-rate-plan?
   Or always manual? For V1, assume manual — operator posts a
   `fee` folio item with description "Late checkout" if they want one.
3. **No-show handling.** Default = mark as cancelled, no charge. Or
   default = mark as cancelled, charge first-night fee? V1: mark
   as cancelled, no charge; operator manually adds fee if applicable.
4. **Multi-currency.** Today everything is MVR. Foreign currency
   payments? V1: all amounts in MVR; foreign payment is recorded as
   MVR equivalent at posting time; the original-currency note goes
   in `Payment.notes`.
5. **GST / tax rate changes.** Tax_amount is snapshotted per folio
   line. If GST rate changes mid-stay, line items posted before the
   change keep the old rate; new lines get the new rate. **Not** a
   retroactive recalculation.
6. **Walk-in vs reserved.** Today every booking has a guest record.
   Walk-ins should still create a booking + guest, not skip the
   guest record.
7. **Owner accounts / direct bills.** A booking with `invoice_to`
   set to a corporate name pays via accounts receivable, not at
   checkout. AR is its own table; defer to a later phase.

---

## 10. References to existing code

Anchors for the implementation team — names that already exist on
staging today:

* `app/models.py::FolioItem` — folio model, V1 shipped
* `app/services/folio.py` — folio helpers, balance math
* `app/routes/folios.py` — `add_item` + `void_item`
* `app/services/audit.py::log_activity` — reuse for ALL new audit rows
* `app/services/board_actions.py` — pure conflict-check helpers
  (move/extend/block); same shape works for night audit pre-checks
* `app/services/ai_drafts.py` — provider-pluggable AI infrastructure;
  reuse for the night-audit narrative generator
* `app/services/branding.py` — brand-aware report headers
* `app/booking_lifecycle.py` — canonical booking + payment status
  vocabularies; reuse for night-audit pre-check classification
* Migrations to date: `f3a7c91b04e2` (audit log), `c2b9f4d83a51`
  (whatsapp messages), `d8a3e1f29c40` (folio items), `e7c1a4b89d62`
  (room blocks — staging only). Future migrations parent from
  `e7c1a4b89d62` until promoted to production.

---

## 11. Out of scope for this plan

The following are intentionally **not** addressed here. Each one is
its own design effort when the time comes:

* Channel manager integration (Booking.com, Airbnb sync)
* Direct credit-card capture (PCI scope — use Stripe / Payhere)
* Multi-property consolidated reporting
* Loyalty / rewards programs
* Inventory management (restaurant ingredients, minibar restock)
* Staff scheduling / payroll
* Connection to formal external accounting software (QuickBooks /
  Xero / Tally) — **maybe** export-only via CSV at first

---

## 12. Pre-build checklist (before code starts)

Before any of the phases above are implemented, the following must be
explicitly approved by the operator:

* [ ] Confirm field-name conventions (`business_date`, `payments`,
      `cashier_shifts`, `daily_revenue_snapshots`).
* [ ] Confirm tax / service-charge handling: snapshotted at post time
      (not retroactive on rate change).
* [ ] Confirm refund convention: new contra-payment row, not void of
      original.
* [ ] Confirm POS architecture: folio is the single source of truth
      for revenue.
* [ ] Confirm AI scope: read-only, summarize / suggest only, never
      auto-execute.
* [ ] Confirm night audit reversibility: irreversible after commit;
      corrections go in next business date.
* [ ] Confirm shift model: V1 single-cashier, V2 multi-cashier when
      restaurant POS ships.

When all boxes are ticked, request:
> "Build Phase 2: Folio V2"
> "Build Phase 3: Cashiering V1"
> etc.

…and a fresh inspection + implementation plan will be produced before
any code is written, following the same pattern used for Folio V1,
Inbound WhatsApp V1, AI Reply Drafts V2, etc.

**End of plan.**
