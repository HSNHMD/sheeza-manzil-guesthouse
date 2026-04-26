# Admin Booking Dashboard Improvements — Plan

**Status: PLAN ONLY — NOT IMPLEMENTED.**
This document describes the next feature in the booking-status-lifecycle thread.
It exists so the next implementation pass has a clear starting point.

**Prerequisite:** The lifecycle module from commit `b3c8e70` ("feature: add
booking and payment lifecycle helpers") must be in place. That commit is on
branch `feature/booking-status-lifecycle` (pushed to origin). All work below
extends from that branch.

**Hard prerequisite NOT yet satisfied:** the existing
`app/routes/bookings.py::confirm` route's precondition list still rejects the
new-vocab statuses (`payment_uploaded`, `pending_payment`, `payment_verified`).
A new public booking submitted today (status `payment_uploaded`) cannot be
admin-confirmed via the existing UI. Phase 0 of this plan addresses that gap
before any other admin-action work.

---

## 1. Booking list filters by `booking_status` and `payment_status`

The current `/bookings/` list filter is a single `status` query-string param
that's mapped to the legacy 6 booking-status values plus a special `unpaid`
sentinel. Replace with two independent filters that compose cleanly with the
new vocabulary.

### Required UI changes

`app/templates/bookings/index.html`:
- Replace the single `<select name="status">` (currently 7 hardcoded options)
  with two side-by-side selects:
  - `name="booking_status"` populated from the `BOOKING_STATUSES` Jinja global
    + an "All bookings" empty option.
  - `name="payment_status"` populated from `PAYMENT_STATUSES` Jinja global
    + an "All payments" empty option.
- Render every option's label via `status_label(option, None, None)` so the
  copy stays consistent with badges.
- Keep the existing `date` and `search` params unchanged.

### Required route change

`app/routes/bookings.py::index`:
- Read both `booking_status` and `payment_status` from `request.args`.
- Apply them as additive filters on the existing query (left-join Invoice,
  add WHERE clauses for each present param).
- Keep the legacy `status` param working for one release as a fallback that
  maps to `booking_status` (so existing bookmarks survive).
- The "unpaid" derived filter (currently a special case) becomes a derived
  query: `Invoice.balance_due > 0 AND booking_status NOT IN ('cancelled', 'rejected')`.
- The KPI counters (arrivals_today, departures_today, in_house) are not
  affected by the filter; they remain global.

---

## 2. Booking detail page — show full lifecycle state

`app/templates/bookings/detail.html`:
- Top-right corner already shows the fused status badge via `status_label` /
  `status_badge` (done in commit `b3c8e70`).
- Add a small "Lifecycle" section under the badge listing:
  - `booking_status: <value>` (raw column value, monospace, for ops debugging)
  - `payment_status: <value>` (raw, monospace)
  - `balance_due: MVR <amount>` (derived)
  - "Valid next actions:" — list of buttons enabled per the action-button
    matrix below.
- The existing payment panel (lines 137-200ish) stays as-is — it was
  refactored in `b3c8e70` to derive panel color from `balance_due` rather
  than hardcoded `payment_status`.

---

## 3. Admin action buttons — what becomes possible per state

The action-button visibility matrix is the heart of this work. Each button
maps to a route. Visibility is gated by the current `(booking_status, payment_status)`
pair. Use the lifecycle module's `is_valid_status_pair()` helper to validate
the *target* pair before any DB write.

| Action button | Source state(s) | Target state | Route (existing or new) |
|---|---|---|---|
| **Mark Payment Pending Review** | `(payment_uploaded, mismatch)` | `(payment_uploaded, pending_review)` | NEW `POST /bookings/<id>/payment/mark-pending-review` |
| **Verify Payment** | `(payment_uploaded, pending_review)`, `(payment_uploaded, mismatch)` | `(payment_verified, verified)` | NEW `POST /bookings/<id>/payment/verify` |
| **Mark Payment Mismatch** | `(payment_uploaded, pending_review)` | `(payment_uploaded, mismatch)` | NEW `POST /bookings/<id>/payment/mark-mismatch` (free-text reason field) |
| **Reject Payment** | `(payment_uploaded, pending_review)`, `(payment_uploaded, mismatch)` | `(rejected, rejected)` | NEW `POST /bookings/<id>/payment/reject` |
| **Confirm Booking** | `(payment_verified, verified)` | `(confirmed, verified)` | EXISTING `POST /bookings/<id>/confirm` (precondition needs widening) |
| **Cancel Booking** | any non-terminal `booking_status` | `(cancelled, <preserve>)` | EXISTING `POST /bookings/<id>/cancel` (already widely-applicable) |
| **Reject Booking** | `new_request`, `pending_payment`, `payment_uploaded`, `payment_verified` | `(rejected, <map>)` | NEW `POST /bookings/<id>/reject` (free-text reason field) |
| **Check In** | `(confirmed, verified)` | `(checked_in, verified)` | EXISTING `POST /bookings/<id>/checkin` (precondition: requires `balance_due == 0` OR explicit override) |
| **Check Out** | `(checked_in, verified)` | `(checked_out, verified)` | EXISTING `POST /bookings/<id>/checkout` (no change) |
| **Record Payment** (cash/card at desk) | `(confirmed, verified)` with `balance_due > 0` | `(confirmed, verified)` (just updates `amount_paid`) | EXISTING `POST /bookings/<id>/payment` (no status change, just amount) |

### Button visibility rule (canonical)

For each action `A` with a target pair `(b_target, p_target)`:
1. Render the button only if the current pair is in `A`'s "Source state(s)" list.
2. Click handler POSTs to the route.
3. Route handler validates: `is_valid_status_pair(b_target, p_target)` AND
   the current pair is permitted as a source. If either fails → 400 + flash
   message (don't change state).

This matrix should live in `app/booking_lifecycle.py` as a constant
`ALLOWED_TRANSITIONS` (dict keyed on action name) so the source-state list
isn't duplicated between templates and routes.

---

## 4. Required route changes (summary)

| File | Change |
|---|---|
| `app/routes/bookings.py::index` | Add `booking_status` + `payment_status` query params alongside legacy `status` |
| `app/routes/bookings.py::confirm` | Widen precondition list to accept `('payment_verified',)` (after Phase 0); set both `booking.status='confirmed'` and `invoice.payment_status='verified'` (no longer `'paid'`) |
| `app/routes/bookings.py::checkin` | Update preconditions: require `(confirmed, verified)` source; gate on `balance_due == 0` or explicit `--allow-balance-due` form param |
| `app/routes/bookings.py::record_payment` | Stop writing `'partial'`/`'paid'`; always write `'verified'` once `amount_paid > 0` (verification trust); rely on derived `balance_due > 0` for partial display |
| `app/routes/invoices.py::record_payment` | Same as above |
| `app/routes/bookings.py` (NEW handlers) | 4 new POST routes: verify-payment, mark-mismatch, reject-payment, mark-pending-review, reject-booking |
| `app/routes/bookings.py` | All status-mutation paths must call `is_valid_status_pair()` before commit (and respond with flash + 400 if invalid) |

---

## 5. Required template changes (summary)

| File | Change |
|---|---|
| `app/templates/bookings/index.html` | Two-filter UI (booking_status + payment_status) populated from Jinja globals; per-row badges already use helpers |
| `app/templates/bookings/detail.html` | Add Lifecycle section + dynamic action button group (one button per row in the §3 matrix where the current state is a valid source); existing badge + payment panel unchanged |
| `app/templates/calendar/index.html` | Update cell color logic to use `status_badge` helper (currently hardcodes `'unconfirmed'`/`'pending_verification'`/`'confirmed'`/`'checked_in'` — needs refresh for new vocabulary) |
| `app/templates/public/confirmation.html` | Status-conditional copy currently keys on `'pending_verification'`; rewrite to use `status_label` for guest-facing display, plus a small `if booking.status == 'payment_uploaded'` branch for "we received your slip — admin will verify" copy |
| `app/templates/staff/room.html` | Filter dropdown options (lines 614-616) currently hardcode `paid/partial/unpaid` — update to new vocabulary so staff can filter by `verified` etc. |
| `app/templates/invoices/index.html` | Filter dropdown options (line 20-22) — same |

---

## 6. Required permission/auth checks

The current routes use `@login_required` for everything and a `_staff_guard`
`before_request` hook that confines non-admins to `/staff/*`. The new admin
actions are all admin-only by nature. Required hardening:

- New routes for verify/reject/mismatch must be wrapped in an
  `@admin_required` decorator (currently doesn't exist — would need to be
  added, e.g. checking `current_user.is_admin` and aborting with 403).
- The existing routes `/bookings/<id>/confirm`, `/checkin`, `/checkout`,
  `/cancel` are already admin-only by virtue of being under `/bookings/*`
  which the staff guard does not allow staff users into. Keep them that way.
- `record_payment` route is also admin-only currently — no change needed.
- The "Reject Booking" route, when invoked, should ideally trigger a
  notification to the guest. Per project rules, AI must NOT auto-send
  WhatsApp; the implementation should DRAFT the message and present it
  for admin approval before sending.

---

## 7. Database schema / migration

**No new schema change required for the dashboard work itself.**

The existing columns accommodate all new values:
- `bookings.status` is `String(20)` (max needed: `payment_uploaded` = 16 chars)
- `invoices.payment_status` is `String(20)` (max needed: `pending_review` = 14 chars)

**Optional defensive migration (HIGHLY RECOMMENDED before this branch deploys):**
A one-shot Alembic migration to rewrite legacy values in existing rows:

```sql
-- Rewrite legacy booking statuses to new vocabulary
UPDATE bookings SET status = 'pending_payment'   WHERE status = 'unconfirmed';
UPDATE bookings SET status = 'payment_uploaded'  WHERE status = 'pending_verification';
-- 'confirmed', 'checked_in', 'checked_out', 'cancelled' are unchanged.

-- Rewrite legacy invoice payment_statuses to new vocabulary
UPDATE invoices SET payment_status = 'not_received' WHERE payment_status = 'unpaid';
UPDATE invoices SET payment_status = 'verified'     WHERE payment_status IN ('paid', 'partial');
```

Without this migration, legacy rows will continue to render correctly via
the helper's `normalize_legacy_payment_status` map, but the new filter
dropdowns won't surface them by their legacy string. This is acceptable for
phased rollout but should be tightened eventually.

A future *non-optional* migration would add a Postgres CHECK constraint
enforcing the canonical vocabulary at the DB level. Out of this dashboard
plan's scope.

---

## 8. Risk list

| # | Risk | Mitigation |
|---|---|---|
| R1 | New routes for verify/reject/mismatch could be hit before legacy rows are migrated, leading to a mix of vocab in the DB | Defensive: every state-mutation route uses `is_valid_status_pair` before commit and reads `normalize_legacy_payment_status` for display. Already in place from `b3c8e70`. |
| R2 | `bookings.py::confirm` precondition still rejects new-vocab statuses (Phase 0 issue) | **MUST be fixed before any admin-confirm UX change**. One-line widening of the `if booking.status not in (...)` tuple. |
| R3 | The "Verify Payment" action needs admin attention but currently the UI auto-confirms when `confirm` is clicked with a slip present (writes `payment_status='paid'` immediately) | New design splits confirm vs verify-payment. Existing route stays for back-compat but its auto-mark logic should be removed once the new verify-payment route is live. Sequence carefully. |
| R4 | Filter dropdowns showing new vocab will not surface legacy rows by their legacy filter values | Either migrate legacy rows (recommended — see §7) or add legacy entries to the dropdown for one release as a transitional measure. |
| R5 | `accounting.py` has 6 query filters using `Invoice.payment_status.in_(['paid', 'partial'])` — these miss new `'verified'` rows | Update those queries either to include `'verified'` OR refactor to derived `balance_due == 0` test. The derived test is vocabulary-agnostic and preferred. |
| R6 | Calendar cells will show wrong colors for new-vocab bookings (template hardcodes old values) | Update `app/templates/calendar/index.html` to use `status_badge` helper |
| R7 | Public confirmation copy (`templates/public/confirmation.html`) keys on `'pending_verification'` — guests with new `'payment_uploaded'` status see no message | Add new branch for new vocab; leave old branch for back-compat |
| R8 | Reject reason / mismatch reason isn't currently a column on `bookings`; capturing it requires either a new column or a free-text note | New column `Booking.admin_notes` (text, nullable) — small, additive migration; OR repurpose existing `bookings.special_requests` which feels wrong. **Decision needed.** |
| R9 | Permission decorator `@admin_required` doesn't exist | Add a small one-line decorator in `app/routes/auth.py` or a new `app/decorators.py` |
| R10 | Templates that gain new buttons are admin-only pages already; staff console won't show them | Confirm at implementation time — the staff console (`/staff/*`) shouldn't expose admin actions |

---

## 9. Recommended build order

This is the order I'd implement, each item small enough to be its own commit:

### Phase 0 — Unblock public bookings (BLOCKING for new bookings)

1. **Widen `bookings.py::confirm` precondition list** to accept new-vocab statuses
   - `('unconfirmed', 'pending_verification', 'pending_payment', 'payment_uploaded', 'payment_verified')`
   - Update the auto-mark-paid logic to write `'verified'` instead of `'paid'`
   - One file, ~10 lines. Trivial.
2. **Same widening for `checkin`, `checkout`, `edit`, `cancel`** preconditions

### Phase 1 — Filter UI and detail-page lifecycle section

3. **Two-filter dropdown** on `bookings/index.html` + route handling for both params
4. **Lifecycle section** in `bookings/detail.html` (raw values + balance_due)
5. **Update `accounting.py` queries** to be vocabulary-agnostic via `balance_due` checks (or include `'verified'` in the existing `.in_()` lists)
6. **Update calendar template** to use `status_badge`
7. **Update public confirmation page** for new vocab branches

### Phase 2 — New admin actions

8. **Add `@admin_required` decorator** in a new `app/decorators.py`
9. **Add `ALLOWED_TRANSITIONS` constant** in `app/booking_lifecycle.py`
10. **Implement `POST /bookings/<id>/payment/verify`** + the matching button in detail.html
11. **Implement `POST /bookings/<id>/payment/mark-mismatch`** + button + reason input
12. **Implement `POST /bookings/<id>/payment/reject`** + button + reason input
13. **Implement `POST /bookings/<id>/reject`** (booking-level reject) + button
14. **Implement `POST /bookings/<id>/payment/mark-pending-review`** (un-mismatch)
15. **Refactor `record_payment` routes** to write `'verified'` and rely on derived partial-display; remove the legacy `'partial'`/`'paid'` writes

### Phase 3 — Migration + cleanup

16. **Optional defensive migration** rewriting legacy rows (per §7)
17. **Remove transitional fallbacks** in templates (the `'paid'`/`'partial'` defensive checks become unnecessary once all rows are migrated)
18. **Add CHECK constraint** for the canonical vocabularies (separate migration)

### Estimated commit count: 18 (small, reviewable, each with its own verification)

---

## 10. What this plan deliberately does NOT include

- Real WhatsApp/email sends from the new admin actions (project rule:
  AI drafts only, human approves). The notification-on-reject flow lands
  as a separate "AI message-drafting" feature, not part of this dashboard
  work.
- Reporting/analytics dashboards (separate concern).
- Bulk actions (multi-select + apply to many) — explicitly out of scope.
- Permission roles beyond admin/staff — current 2-role system is sufficient
  for this plan.
- Booking deletion improvements — current delete is admin-only and
  destructive-by-design; not changing that here.

---

*Plan prepared 2026-04-27; tracker file lives at `docs/admin_dashboard_plan.md`.*
*Implementation will be discussed and authorized before any code lands.*
