# Production Migration Rehearsal Plan

**Goal:** fast-forward production `sheezamanzil.com` from `main` (Alembic
`c2b9f4d83a51`) to the `feature/reservation-board` tip (`88af2f5`, Alembic head
`d6a2f59b8e34`) safely. This document is the **rehearsal procedure only** — no
execution. Run the rehearsal against a throwaway database first; only schedule
the real run after the rehearsal passes every check below.

> Status: PLAN ONLY. Prepared 2026-07-26. Reviewer: Kairos + Hussain.

## 0. Facts this plan is built on

- Prod DB `sheeza_db` is at Alembic `c2b9f4d83a51` (11 tables). Engine PostgreSQL
  16.14, loopback-only on the same VPS.
- Head is `d6a2f59b8e34` (staging runs this; 36 tables).
- **17 migrations** sit between prod and head. All were scanned:
  **every one is additive in the forward (`upgrade`) path** — zero forward
  `DROP TABLE` / `DROP COLUMN` / `DELETE` / `TRUNCATE`. Every migration defines a
  real `downgrade()` (reversible).
- Two migrations run **idempotent data backfills** in `upgrade()` (see §5).
- `main` is a strict ancestor of `feature/reservation-board` → the git side is a
  clean fast-forward; this plan concerns the **database** side.

## 1. Recommended rehearsal host — **this VPS (srv1601676)**, not Mentis

Restore into a throwaway database **on the same Postgres cluster as prod**:

- The prod dump contains **guest PII**; keeping the restore local means PII never
  crosses a host boundary. Shipping a dump to Mentis would export ID/passport-linked
  data off-box — avoid.
- Same engine (PG 16.14) → migration behavior is identical; a cross-host rehearsal
  on a different minor version could mask/introduce differences.
- The restore + upgrade is small and fast (prod is tiny — see §6), so local resource
  contention is negligible. Run it off-hours regardless.

Throwaway DB name: `sheeza_rehearsal`. Dropped at the end (§7). Never point the app at it.

## 2. Rehearsal procedure

All commands run as a DB admin on the VPS. Do **not** touch `sheeza_db` (prod) during rehearsal.

```bash
# 2.1 Snapshot prod (also serves as the real-run backup later)
runuser -u postgres -- pg_dump -Fc sheeza_db > /var/lib/hermes-snapshots/rehearsal_$(date +%Y%m%d_%H%M%S).dump

# 2.2 Create + restore into throwaway DB
runuser -u postgres -- createdb sheeza_rehearsal
runuser -u postgres -- pg_restore -d sheeza_rehearsal --no-owner /var/lib/hermes-snapshots/rehearsal_*.dump

# 2.3 Confirm the restored DB starts at prod's revision
runuser -u postgres -- psql -d sheeza_rehearsal -tAc "SELECT version_num FROM alembic_version;"
#   expect: c2b9f4d83a51

# 2.4 Run the migration chain against the throwaway DB ONLY (throwaway venv at 88af2f5)
#   Use a scratch checkout of feature/reservation-board tip, its own venv, and a
#   DATABASE_URL pointing at sheeza_rehearsal — NEVER prod.
export DATABASE_URL='postgresql://<rehearsal-role>@127.0.0.1:5432/sheeza_rehearsal'
flask db upgrade 2>&1 | tee /tmp/rehearsal_upgrade.log
#   expect: 17 migrations apply cleanly, ending at d6a2f59b8e34
```

## 3. Expected new tables after upgrade (enumerate + assert present)

From the 17 migrations (create order): `folio_items`, `room_blocks`,
`cashier_transactions`, `business_date_state`, `night_audit_runs`, `room_types`,
`rate_plans`, `rate_overrides`, `rate_restrictions`, `pos_categories`, `pos_items`,
`guest_orders`, `guest_order_items`, `booking_groups`, `property_settings`,
`properties`, `channel_connections`, `channel_room_maps`, `channel_rate_plan_maps`,
`channel_sync_jobs`, `channel_sync_logs`, `stay_segments`, `work_orders`,
`channel_import_exceptions`, `channel_inbound_events`.

Plus **column additions** on existing tables: `property_id` on the 13 property-scoped
tables (bookings, rooms, room_types, rate_plans, rate_restrictions, rate_overrides,
room_blocks, folio_items, invoices, cashier_transactions, booking_groups,
channel_connections, whatsapp_messages), room housekeeping fields on `rooms`,
`department` on `users`, `source` on `bookings`.

Target: prod goes from **11 → 36 tables**.

## 4. Pass/fail checks (all must pass before scheduling the real run)

- [ ] `flask db upgrade` exits 0; log shows all 17 revisions applied, no traceback.
- [ ] `alembic_version.version_num = d6a2f59b8e34`.
- [ ] All 25 expected new tables exist (`\dt` count == 36).
- [ ] **Pre-existing row counts unchanged** vs the prod snapshot: bookings, guests,
      invoices, rooms, users, activity_logs, whatsapp_messages identical before/after
      (the upgrade must not lose or duplicate rows).
- [ ] **Backfill correctness:** zero NULL `property_id` on property-scoped tables that
      had rows; zero NULL/empty `bookings.source`.
- [ ] App boots against `sheeza_rehearsal`: `/healthz` 200, and a spot-render of
      `/bookings/`, `/board`, `/inventory/`, `/reports/` returns 200 with no template error.
- [ ] Full test suite passes against the migrated throwaway DB (parity with staging).
- [ ] `flask db downgrade -1` then `flask db upgrade` round-trips cleanly (reversibility spot-check).

## 5. Migrations flagged for attention (none destructive)

No migration is destructive or non-reversible in the forward path. Two touch existing data:

| Rev | File | Forward data op | Risk |
|-----|------|-----------------|------|
| `1d9b6a4f5e72` | add_property_foundation | `UPDATE <table> SET property_id = 1 WHERE property_id IS NULL` on each newly property-scoped table | Idempotent backfill to default property (id 1). Safe; re-runnable. Verify property id 1 row is created first in the same migration. |
| `2e8c4d7a3f51` | add_channel_foundation | `UPDATE bookings SET source = 'direct' WHERE source IS NULL OR source = ''` | Idempotent backfill of booking source. Safe; re-runnable. |

Everything else is pure `create_table` / `add_column` (additive). All 17 have a
populated `downgrade()`.

## 6. Downtime estimate for the real run

Prod is tiny (bookings ≈ 9, guests ≈ 12, invoices ≈ 9). The work is 25 `CREATE TABLE`
+ column adds (DDL on empty/near-empty tables is sub-second each) plus 2 `UPDATE`s over
< 50 rows.

- **Raw migration time:** a few seconds.
- **Real maintenance window (recommended):** **~3–5 minutes** end to end —
  stop app → `pg_dump` prod safety backup → `flask db upgrade` → smoke `/healthz`
  + key pages → start app. The rehearsal (§2) will produce the exact measured time;
  use that figure to size the announced window.

## 7. Real-run cutover (after rehearsal passes — separate approved change)

1. Announce the maintenance window.
2. `systemctl stop sheeza.service`.
3. `pg_dump -Fc sheeza_db` → dated backup (the rollback artifact).
4. `git -C /var/www/sheeza-manzil pull --ff-only origin main` **after** `main` is
   fast-forwarded to `88af2f5` (or deploy the reviewed tip per the deploy runbook).
5. `flask db upgrade` against `sheeza_db`.
6. Run §4 checks against prod.
7. `systemctl start sheeza.service`; smoke `/healthz` + key pages.
8. **Rollback if any check fails:** `systemctl stop`, `dropdb sheeza_db`, `createdb sheeza_db`,
   `pg_restore` the step-3 backup, redeploy the prior commit, `systemctl start`.

## 8. Cleanup after rehearsal

```bash
runuser -u postgres -- dropdb sheeza_rehearsal
rm -f /tmp/rehearsal_upgrade.log
# keep the pg_dump; it doubles as a fresh prod backup
```
