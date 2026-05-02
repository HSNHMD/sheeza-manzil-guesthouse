# 0002 — Folio as money spine

**Status:** Accepted (2026-Q1)

## Context

Hotels track money along three axes that don't reduce to each other:
1. **What was charged** (room nights, mini-bar, POS posts,
   adjustments) → line items.
2. **What was billed** (a tax-compliant document the guest receives)
   → invoice.
3. **What was paid** (cash, card, transfer) → cashier transactions.

The PMS needs all three. A single "amount owed" field on `Booking`
isn't enough — it can't represent partial pays, refunds, group
billing, void / reissue cycles, or POS posts to a closed folio.

## Decision

The **folio is the money spine**. Specifically:
- `FolioItem` is the canonical line-charge ledger. Every charge —
  whether it came from the booking, POS, or an operator adjustment —
  is a `FolioItem`.
- `Invoice` is a money envelope. One booking can have many invoices
  (deposit + final, group splits, etc.). Each invoice references
  folio items and carries `amount_paid` / `payment_status`.
- `CashierTransaction` is a money movement. Writes update the
  parent invoice's `amount_paid` and `payment_status`.

`Booking.total_amount` is a **denormalized convenience** for the
board / list views. It is NOT the source of truth — the source of
truth is the folio.

## Consequences

**Easier:**
- Group billing is just "many invoices, one booking group."
- Refunds are a negative `CashierTransaction` against the same
  invoice.
- POS posts to an in-house guest become folio items on the open
  invoice.
- Audit trail is straightforward — every money movement is a row.

**Harder:**
- More tables to keep consistent. Cashiering V1 + Payment
  Reconciliation V1 (`docs/guest_folio_accounting_pos_roadmap.md`)
  handle the consistency rules.
- Reports that want "revenue today" must go through the folio, not
  `Booking.total_amount`.
- Future automation (auto-cancellation refund) MUST go through the
  folio. The OTA cancellation handler (`apply_cancellation`) refuses
  to cancel a booking with `Invoice.amount_paid > 0` for this reason.

## Alternatives considered

- **Single `Booking.total_amount` + `Booking.paid_amount`.** Rejected
  — can't represent group billing, can't represent multi-invoice
  scenarios, audit trail is "trust me."
- **Per-line `BookingCharge` table on Booking, no Invoice
  intermediate.** Rejected — invoices are tax-compliant documents in
  many jurisdictions; we need them as first-class objects.
