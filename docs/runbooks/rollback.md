# Rollback runbook

> **Last updated:** 2026-05-03.
> Read this BEFORE you deploy, not after. Rollbacks are calmer when
> you've rehearsed them.

## Decision tree (60 seconds in)

```
Is the new prod build returning 5xx?
├── Yes ── Code rollback (Section A) → restart → confirm
└── No  ── Is data wrong / corrupted?
          ├── Yes ── DB rollback (Section B) — STOP and read carefully
          └── No  ── Is it visual / minor? Patch forward in next sprint.
```

## A. Code rollback (no schema changes involved)

The fastest, safest path. Only valid if the deploy did NOT change
the DB schema (or if the schema change is backward-compatible —
i.e. the previous code can run against the new schema).

```bash
ssh root@187.127.112.36
cd /var/www/sheeza-manzil

# 1. Find the previous sha — either from your deploy notes or:
git log --oneline -10

# 2. Reset the working tree (NEVER force-push the upstream branch):
git fetch origin
git reset --hard <previous-sha>

# 3. Restart the service:
sudo systemctl restart sheeza.service
sleep 3
sudo systemctl is-active sheeza.service

# 4. Verify:
curl -sk -o /dev/null -w "%{http_code}\n" https://sheezamanzil.com/
curl -sk https://sheezamanzil.com/admin/diag | grep -oE 'build [a-f0-9]+'
```

After confirming production is back up:
- File a bug in `docs/handoff/known_bugs.md`.
- Open a fix branch from `main` (NOT from the bad sha).
- Re-deploy the fix following `production_deploy.md`.

## B. DB rollback (schema migration involved)

**Read this entire section before doing anything.**

Alembic supports `flask db downgrade <target>`, but downgrades are
data-destructive when the new migration added a column or table that
already has live writes. Do not run `flask db downgrade` blindly.

### B.1 — Decide what to restore

| Scenario | Action |
|---|---|
| New migration added an empty table | Safe to `flask db downgrade -1`; the new table has no data. |
| New migration added a nullable column | Safe to downgrade — column drops cleanly. |
| New migration added a NOT NULL column with a default | Safe — downgrade drops the column. |
| New migration backfilled existing rows | UNSAFE — downgrade reverts the column drop, but the backfilled values are LOST. |
| New migration deleted a column or table | NEVER auto-downgrade. Restore from `pg_dump`. |
| New migration changed a constraint that is now violated | NEVER auto-downgrade. Restore from `pg_dump`. |

### B.2 — Restore from backup (the safe path)

This loses every write since the backup. Inform operators FIRST.

```bash
# On VPS, in /var/www/sheeza-manzil:
set -a && source .env && set +a

# 1. Stop the app so writes can't race:
sudo systemctl stop sheeza.service

# 2. Pick the backup created in pre-flight:
ts=20260503T120000Z   # ← replace with your backup timestamp
ls /root/backups/$ts/

# 3. Drop and recreate the database (CAUTION):
psql "$DATABASE_URL" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"

# 4. Restore:
gunzip -c /root/backups/$ts/db.sql.gz | psql "$DATABASE_URL"

# 5. Restore uploads:
tar -xzf /root/backups/$ts/uploads.tgz -C /var/www/sheeza-manzil/

# 6. Roll the code back to the matching sha:
git reset --hard $(cat /root/backups/$ts/code.sha)

# 7. Restart:
sudo systemctl start sheeza.service
sudo systemctl is-active sheeza.service
```

### B.3 — Auto-downgrade (only when section B.1 says "safe")

```bash
# Show migration history:
flask db history -r current:-3

# Downgrade one step:
flask db downgrade -1

# Verify:
flask db current

# Restart:
sudo systemctl restart sheeza.service
```

## Service restart shortcuts

```bash
# Production:
sudo systemctl restart sheeza.service
sudo systemctl status  sheeza.service
sudo journalctl -u sheeza.service -n 200 --no-pager

# Staging:
sudo systemctl restart guesthouse-staging.service
sudo systemctl status  guesthouse-staging.service
sudo journalctl -u guesthouse-staging.service -n 200 --no-pager
```

## Recovering from a bad staging deploy

Same playbook as production but lower stakes:

```bash
ssh root@187.127.112.36
cd /var/www/guesthouse-staging
git log --oneline -10
git reset --hard <previous-sha>

set -a && source .env && set +a
source venv/bin/activate
export FLASK_APP=run.py
flask db downgrade <target-revision>   # only if schema changed

sudo systemctl restart guesthouse-staging.service
```

If staging is so broken you can't reason about it, drop it and clone
fresh from `feature/reservation-board` per `staging_setup.md`.
Staging data is not precious — that's the point.

## What to verify after rollback

1. `/admin/diag` shows the **rolled-back** sha (not the bad one).
2. `flask db current` returns the migration head you expect.
3. `journalctl -u <service> -n 100` shows no tracebacks since the restart.
4. A small read query in psql returns sane row counts:
   ```sql
   SELECT COUNT(*) FROM bookings;
   SELECT COUNT(*) FROM users;
   SELECT COUNT(*) FROM invoices;
   ```
5. An operator logs in and confirms a recent booking still exists.
6. **Update `docs/handoff/known_bugs.md`** — what failed, why, and
   the sha that's running now.
