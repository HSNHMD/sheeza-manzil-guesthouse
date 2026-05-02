# Next steps — recommended priority queue

> **Last updated:** 2026-05-03.
> Read `current_state.md` first. This file assumes you understand the
> 48-commit gap between `main` and `feature/reservation-board`.

## The single most important next decision

**Should the 48-commit `feature/reservation-board` stack be promoted
to `main` and deployed to production, or should staging continue to
soak first?**

Until that decision is made, every new sprint widens the gap. There is
no automation to bring the gap forward — the production deploy is
manual, gated, and uses the runbook in `docs/runbooks/production_deploy.md`.

## Immediate next sprint (1 of 2)

**Production-merge spike.** Not a feature sprint; a delivery sprint.

Goals:
1. Re-baseline `main` from `feature/reservation-board` in a single
   reviewed PR.
2. Take a fresh production DB backup (see
   `docs/runbooks/backup_restore.md`).
3. Run the 16 staging-only Alembic migrations against production in a
   maintenance window, verifying each step. The migration chain to
   apply (in order) is:
   - `0c5e7f3b842a_add_property_settings`
   - `1d9b6a4f5e72_add_property_foundation`
   - `3f7b1c8e2a04_add_stay_segments_table`
   - `4d8e3c91a76b_add_user_department`
   - `a3b8e9d24f15_add_business_date_and_night_audit_tables`
   - `a8f3c91d5b27_add_work_orders_table`
   - `c5d2a3f8e103_add_rates_inventory_tables`
   - `c2b9f4d83a51_add_whatsapp_messages_table`  *(verify whether
     this is already in prod — production already runs WhatsApp)*
   - `2e8c4d7a3f51_add_channel_foundation`
   - `c4f7d2a86b15_add_channel_import_exceptions`
   - `d6a2f59b8e34_add_channel_inbound_events`
   - …plus any others not yet applied. **The ground truth is
     `flask db heads` on production vs the `migrations/versions/`
     directory.** Don't trust this list blindly — re-derive it.
4. Smoke-test every module from `current_state.md` against production
   data, using the staging URLs as a checklist.

Estimated effort: 1 day of careful work + 1 maintenance-window evening.

**Prerequisite:** read `docs/runbooks/rollback.md` BEFORE starting so
you know how to back out cleanly if anything goes wrong.

## Second next sprint (2 of 2)

**Real outbound OTA HTTP client for booking_com sandbox.**

Today the channel manager has full models, mappings, an exception
queue, modify+cancel handlers, and a sandbox import form — but
zero outbound HTTP. This sprint adds:
1. A typed Booking.com sandbox client (sandbox endpoints only,
   credentials in env).
2. A scheduled inbound poll (or webhook receiver) that calls
   `services.channel_import.import_reservation` /
   `apply_modification` / `apply_cancellation` with real OTA
   payloads.
3. Outbound availability + rate push wired into existing
   `ChannelSyncJob` queue.

**Hard prerequisites** (do NOT start this until each is true):
- Production-merge spike completed (sprint 1 above).
- Real Booking.com sandbox credentials provisioned + stored in
  staging `.env` (NOT in repo).
- A documented OTA escalation contact list.

## Blocked items

| Item | Blocked by | Notes |
|---|---|---|
| Multi-property UI | Foundation depth audit (every wave-1 model is property-aware at the column level, but routes still hard-code `current_property_id()`) | See `docs/multi_property_foundation_plan.md`. Adding a property switcher requires a per-route audit; estimate 1 sprint. |
| Real OTA client | Production-merge + sandbox creds | See above. |
| OTA dead-letter retry worker | Real OTA client | Until we make outbound calls, there's nothing to retry. |
| Refund automation on cancel_unsafe_state | PCI compliance review | Cashiering V1 deliberately keeps refund decisions operator-only. |
| Photo / attachment upload on work orders | R2 wiring on staging | Tracked in `project_sheeza_r2_gap.md`. |

## Prerequisites for risky modules

Before any of these touch production, the listed prerequisite MUST be
satisfied:

| Risky module | Prerequisite |
|---|---|
| Real OTA outbound calls | Sandbox + production credential separation; rate-limit + circuit-breaker tested on staging |
| Auto-cancellation refund | Two-person approval workflow + folio reconciliation review |
| Multi-property switcher | Audit every `Booking` / `Invoice` / `Folio` query for property scoping |
| Night Audit auto-advance | Manual run on staging at 23:00 + 03:00 windows for at least 7 days |
| Channel availability push | Rate-plan / inventory mapping audit per property |

## What must be fixed before new modules continue

- **Time-zone flake in `tests/test_front_office.py`** — uses
  `date.today()` (OS-local) but routes use `hotel_date()` (MVT-locked).
  4 tests fail when local OS calendar day is ahead of MVT calendar day.
  See `docs/handoff/known_bugs.md`. Fix is a one-line swap in the test
  fixture — could be done in any sprint.
- **Production-merge gap** (above). New sprints become harder to
  back-port the longer this waits.
- **Stay-segment renderer parity audit** — segments work for 1-room
  stays; verify multi-segment rendering matches the legacy
  single-bar layout in every responsive breakpoint.

Beyond those three, no hard blockers exist for the next feature
sprint.
