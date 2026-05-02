# Backup + restore runbook

> **Last updated:** 2026-05-03.
> Three things must be backed up: source, DB, uploads. Each has its
> own procedure.

## Backup hierarchy (least to most expensive)

| Tier | Frequency | Used when |
|---|---|---|
| Git push to GitHub | Every commit | Source code is automatically safe — the repo is the source of truth. |
| `pg_dump` snapshot | Before every risky deploy + nightly cron (recommended) | Schema migrations, large data backfills, maintenance windows. |
| Uploads tarball | Before every deploy that touches `uploads/` or auth | Logo / brand asset changes, room photos. |
| R2 / off-site DB copy | Daily (recommended; status: see `project_sheeza_r2_gap.md`) | Disaster recovery (VPS lost). |

## Source backup

The source is GitHub. As long as `git push` succeeded, source is safe.
Verify recent pushes:

```bash
# What's the latest pushed sha?
git ls-remote origin HEAD

# Local matches?
git rev-parse HEAD
```

If the two diverge, push (or pull) before doing anything else.

## DB backup — pre-deploy snapshot

Run this on the VPS as root, in the production directory:

```bash
ssh root@187.127.112.36
cd /var/www/sheeza-manzil

# Load .env so DATABASE_URL is in scope; do NOT echo the file:
set -a && source .env && set +a

ts=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p /root/backups/$ts

# DB dump — gzipped:
pg_dump "$DATABASE_URL" | gzip > /root/backups/$ts/db.sql.gz

# Sanity check — dump should be at least a few MB:
ls -lh /root/backups/$ts/db.sql.gz

# Capture the running sha so a future restore knows which code to pair:
git rev-parse HEAD > /root/backups/$ts/code.sha
```

## Uploads backup

```bash
cd /var/www/sheeza-manzil
tar -czf /root/backups/$ts/uploads.tgz uploads/
ls -lh /root/backups/$ts/uploads.tgz
```

## What gets backed up before risky changes

A "risky change" is any of:
- a schema migration that drops or renames a column;
- a data backfill that mutates `> 1000` rows;
- a restore-from-backup itself (always snapshot before overwriting);
- a brand / logo change (uploads).

Before any of those, the deployer **must** create the snapshot above
and confirm `db.sql.gz` is non-zero in size.

## DB restore

This wipes the current database. Inform operators first.

```bash
# On VPS:
set -a && source /var/www/sheeza-manzil/.env && set +a

# 1. Stop the app so writes can't race the restore:
sudo systemctl stop sheeza.service

# 2. Pick the backup:
ts=20260503T120000Z   # ← replace with the snapshot you want
ls -la /root/backups/$ts/

# 3. WIPE schema (verify the URL points at the right DB FIRST):
psql "$DATABASE_URL" -c '\conninfo'
psql "$DATABASE_URL" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"

# 4. Restore:
gunzip -c /root/backups/$ts/db.sql.gz | psql "$DATABASE_URL"

# 5. Verify table counts:
psql "$DATABASE_URL" -c "
  SELECT 'bookings' AS t, COUNT(*) FROM bookings
  UNION ALL SELECT 'invoices', COUNT(*) FROM invoices
  UNION ALL SELECT 'users',    COUNT(*) FROM users
  UNION ALL SELECT 'rooms',    COUNT(*) FROM rooms;
"

# 6. Restart:
sudo systemctl start sheeza.service
```

## Uploads restore

```bash
cd /var/www/sheeza-manzil

# Backup the existing uploads first (in case the restore is wrong):
mv uploads uploads.broken-$(date -u +%Y%m%dT%H%M%SZ)

tar -xzf /root/backups/$ts/uploads.tgz -C /var/www/sheeza-manzil/
chown -R www-data:www-data uploads/   # or whatever the gunicorn user is
```

## Source restore

If `main` is corrupted or you need to pin to a specific known-good
sha:

```bash
ssh root@187.127.112.36
cd /var/www/sheeza-manzil

# Get the sha that paired with the last good DB snapshot:
cat /root/backups/$ts/code.sha
# → e.g. 3372dcf

git fetch origin
git reset --hard <that-sha>
```

Restart the service after restoring.

## Backup retention

Currently manual. Recommended policy (not yet automated):
- Keep daily backups for 14 days.
- Keep weekly backups for 8 weeks.
- Keep monthly backups for 12 months.
- Keep "before risky deploy" backups indefinitely (small).

A nightly `cron` should be added. See `next_steps.md` for the open
TODO.

## Off-site backup (R2)

Per `project_sheeza_r2_gap.md`:
- Cloudflare R2 is now wired for new uploads (dual-write).
- 9 historical files still need backfill.
- DB dumps to R2 are NOT yet automated — add to `next_steps.md` if
  it isn't there already.

## Verifying a backup

A backup that hasn't been test-restored is hope, not a backup. At
least once per quarter:

1. Spin up a temp Postgres database on a developer laptop.
2. Restore the latest production dump into it:
   ```bash
   gunzip -c /tmp/db.sql.gz | psql postgres://localhost/restore_test
   ```
3. Confirm `SELECT COUNT(*) FROM bookings` returns a sane number.
4. Drop the temp DB.

## Where backups live

| Tier | Location |
|---|---|
| Git source | https://github.com/HSNHMD/sheeza-manzil-guesthouse |
| DB dumps | `/root/backups/<UTC-timestamp>/db.sql.gz` on VPS |
| Uploads tarballs | `/root/backups/<UTC-timestamp>/uploads.tgz` on VPS |
| R2 (uploads only, partial) | Cloudflare R2 bucket (see `.env CLOUDFLARE_*` keys) |
