# Production deploy runbook

> **Last updated:** 2026-05-03.
> A production deploy is **never one-click**. Follow this top-to-bottom.
> If anything looks wrong, STOP and read `rollback.md`.

## Pre-flight (before you touch anything)

- [ ] You have an authoritative reason to deploy. Random redeploys are
      not a feature.
- [ ] You have read `docs/handoff/current_state.md` so you know what
      sha is currently in production.
- [ ] You have informed the operator(s) — production downtime is
      typically 30-60 seconds during the gunicorn restart.
- [ ] Local working tree is clean (`git status` shows nothing).
- [ ] `git fetch origin && git rev-list --count origin/main..HEAD`
      returns the number of commits you're about to deploy. Read each
      commit message before continuing.
- [ ] CI / local test suite is green: `python -m unittest discover tests`.
- [ ] You have a backup. See `docs/runbooks/backup_restore.md` first.
- [ ] A maintenance window is scheduled if the deploy includes a
      schema migration that locks any table.

## Deploy prerequisites

| Item | How to verify |
|---|---|
| You can SSH as root to the VPS | `ssh root@187.127.112.36 'echo ok'` |
| `git` on the VPS knows the remote | `git remote -v` shows the github URL |
| Production `.env` is intact | `wc -l /var/www/sheeza-manzil/.env` matches your records |
| Production DB credentials work | `psql "$DATABASE_URL" -c '\dt' | head` |
| Latest backup is recent | see `backup_restore.md` |

## Backup first (always)

This is non-negotiable. See `docs/runbooks/backup_restore.md` for the
full backup procedure. Quick summary:

```bash
# On the VPS:
cd /var/www/sheeza-manzil
set -a && source .env && set +a
ts=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p /root/backups/$ts
pg_dump "$DATABASE_URL" \
    | gzip > /root/backups/$ts/db.sql.gz
tar -czf /root/backups/$ts/uploads.tgz uploads/
git rev-parse HEAD > /root/backups/$ts/code.sha
ls -la /root/backups/$ts/
```

Verify the dump file is non-zero before continuing.

## Merge strategy (local)

```bash
# On your laptop, in a clean working tree:
git checkout main
git pull --ff-only

# Decide which commits go in. For the production-merge spike (sprint
# 1 in next_steps.md) this is the entire feature/reservation-board:
git merge --no-ff feature/reservation-board \
          -m "merge: feature/reservation-board → main (production cut N)"

# Or, for a single targeted hotfix:
git cherry-pick <sha>

# Push to GitHub but DO NOT touch production yet:
git push origin main
```

Never force-push `main`.

## VPS pull strategy

```bash
ssh root@187.127.112.36
cd /var/www/sheeza-manzil

# Show what's about to land:
git fetch origin
git log --oneline HEAD..origin/main

# If correct, fast-forward:
git pull --ff-only origin main

# Confirm:
git rev-parse --short HEAD
```

## Migration step

```bash
# Still on VPS, in /var/www/sheeza-manzil:
set -a && source .env && set +a
source venv/bin/activate
export FLASK_APP=run.py

# What's the current head?
flask db current

# What heads exist in the migration files?
flask db heads

# Dry-run by listing what will run:
flask db history -r current:head | head

# Apply (in a maintenance window if any migration locks a table):
flask db upgrade

# Confirm:
flask db current
```

If `flask db upgrade` errors **stop and read the error**. Do not
re-run blindly. The error message names the failing migration; check
its SQL against the production schema. If it's a table-already-exists
error, the migration may need `IF NOT EXISTS` or a manual stamp.

## Restart step

```bash
sudo systemctl restart sheeza.service
sleep 3
sudo systemctl is-active sheeza.service
sudo journalctl -u sheeza.service -n 100 --no-pager
```

The first 100 lines of the new boot should show no tracebacks. If
they do, **stop and run rollback.md**.

## Health checks (immediate)

```bash
# Production should respond:
curl -sk -o /dev/null -w "GET / = %{http_code}\n" https://sheezamanzil.com/

# Login should respond:
curl -sk -o /dev/null -w "GET /appadmin = %{http_code}\n" https://sheezamanzil.com/appadmin

# Diag page should match the sha you just deployed:
curl -sk https://sheezamanzil.com/admin/diag | grep -oE 'build [a-f0-9]+' | head -1
```

Expect `200` on `/` and `/appadmin` and a sha matching `git rev-parse
--short HEAD`.

## Post-deploy checks (within 5 minutes)

Log in as an operator account and click through:
1. **Dashboard** — no 500.
2. **Bookings list** — recent bookings render.
3. **Reservation board** (if shipped to prod) — drag a booking,
   verify it persisted on refresh.
4. **Front office arrivals/departures** — counts look sane.
5. **Invoices list** — money totals match yesterday's report.
6. **WhatsApp inbox** — if WhatsApp is in the deploy, send a test
   message FROM a known sandbox number to confirm webhook is alive.
7. **Admin → Activity** — recent activity rows appear.

Watch `journalctl -u sheeza.service -f` for 5 minutes. Any traceback
or 500 → run rollback.

## Post-deploy hygiene

- Update `docs/handoff/current_state.md` with the new production sha.
- Tag the deploy: `git tag -a prod-YYYY-MM-DD -m "summary"` and push.
- Note any quirks observed in `docs/handoff/known_bugs.md`.

## Things that should NEVER happen

- A production deploy without a backup.
- A production deploy outside an announced window.
- A production deploy from a branch other than `main`.
- A force-push to `main`.
- A `flask db upgrade` without first confirming `flask db current`.
- Editing `.env` in-place without committing the new keys to
  `.env.example` (with placeholder values).
- Removing tests to make a deploy "go through".
