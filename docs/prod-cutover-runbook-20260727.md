# Prod Cutover Runbook — 2026-07-27 00:30 window

**Rehearsed:** 2026-07-26 (see rehearsal-report.md — GO). **Executor:** on the Sheeza VPS as root.
**Pinned deploy commit:** `8434eb426d6816c5546d82724695c50f78ce3568` (`feature/reservation-board` tip, includes upload-authz fix + `/admin/seed` env-gate).
**Rollback commit:** `3372dcf314fff01c2a6207e32a6f2081ee95573a` (current prod `main`).
**Prod facts:** app `/var/www/sheeza-manzil`; service `sheeza.service` (gunicorn `127.0.0.1:8000`); DB `sheeza_db` (owner/role `sheeza`); nginx vhost `sheeza-manzil`; public portal at `/book`.

> Migration is 17 additive migrations (rehearsed total **0.38s**; DDL on ≤12-row tables). No destructive/non-reversible step. Two idempotent backfills (property_id, bookings.source). Expect a **3–5 minute** window end-to-end, dominated by service stop/start + verification, not the migration.

---

## 0. Pre-flight (before 00:30)
- Confirm on the box: `git -C /var/www/sheeza-manzil rev-parse HEAD` == `3372dcf314…` (still on old prod).
- Confirm the rehearsal pre-cutover dump exists OR take a fresh one in step 2.
- Have this runbook open; no live debugging after step 5 except per the rollback rule (§8).

## 1. Announce / lockout
- Staff told: **hands-off 00:15–01:30**.
- (Optional) Public portal maintenance page:
  ```bash
  # enable maintenance (503) for the public /book portal
  sudo tee /etc/nginx/snippets/sheeza-maint.conf >/dev/null <<'EOF'
  return 503;
  EOF
  # add `include snippets/sheeza-maint.conf;` inside the `location /book` block, then:
  sudo nginx -t && sudo systemctl reload nginx
  # DISABLE after cutover: remove the include line (or the snippet), nginx -t && reload.
  ```
  (Optional — the app is admin-facing; the public portal is `/book` only.)

## 2. Backup — the restore point (REQUIRED)
```bash
TS=$(date -u +%Y%m%d_%H%M%S)
sudo -u postgres pg_dump -Fc sheeza_db -f /var/lib/postgresql/cutover_sheeza_db_${TS}.dump
sudo -u postgres pg_restore --list /var/lib/postgresql/cutover_sheeza_db_${TS}.dump | head   # must list tables
echo "RESTORE POINT: /var/lib/postgresql/cutover_sheeza_db_${TS}.dump"   # write this path down
```

## 3. Stop service + checkout the pinned commit
```bash
sudo systemctl stop sheeza.service
cd /var/www/sheeza-manzil
git fetch origin
git checkout 8434eb426d6816c5546d82724695c50f78ce3568        # exact pinned hash (detached HEAD)
git rev-parse HEAD    # must echo 8434eb426d68...
```

## 4. Env additions (from rehearsal Task 1f)
No new **required** env var. Add one recommended explicit line to `/var/www/sheeza-manzil/.env`:
```bash
grep -q '^ENABLE_SEED_ROUTES=' /var/www/sheeza-manzil/.env || echo 'ENABLE_SEED_ROUTES=false' | sudo tee -a /var/www/sheeza-manzil/.env
```
Do NOT add `BRAND_*_OVERRIDE` or `STAGING` (staging-only; absence = default Sheeza branding). `ANTHROPIC_*` only if switching AI provider (prod uses gemini) — leave unset.

## 5. Migrate (against sheeza_db) — teed to a log
```bash
cd /var/www/sheeza-manzil
export FLASK_APP=run.py
source venv/bin/activate        # prod venv
set -a; . /var/www/sheeza-manzil/.env; set +a    # load DATABASE_URL etc.
flask db current                                  # expect c2b9f4d83a51
flask db upgrade 2>&1 | tee /var/lib/postgresql/cutover_migrate_${TS}.log
flask db current                                  # expect d6a2f59b8e34 (head)
```

## 6. Restart + APP_GIT_SHA
```bash
sudo sed -i 's/^APP_GIT_SHA=.*/APP_GIT_SHA=8434eb426d6816c5546d82724695c50f78ce3568/' /var/www/sheeza-manzil/.env \
  && grep -q '^APP_GIT_SHA=' /var/www/sheeza-manzil/.env || echo 'APP_GIT_SHA=8434eb426d6816c5546d82724695c50f78ce3568' | sudo tee -a /var/www/sheeza-manzil/.env
sudo systemctl start sheeza.service
sleep 3; systemctl is-active sheeza.service
```

## 7. Verification checklist (all must pass)
```bash
# a. healthz sha == pinned hash
curl -s http://127.0.0.1:8000/healthz | grep -oE '[a-f0-9]{40}'      # == 8434eb426d68...5568
# b. login page renders
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/console   # 200
# c. /admin/seed absent (env-gate) 
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/admin/seed # 404
```
Then in a browser (admin login):
- Open an existing booking → detail renders; **staff must NOT see ID/slip download links** (admin only).
- Tape chart `/board` loads.
- **Create a test booking marked TEST, then immediately cancel it** (lifecycle transition, audited) — confirm it appears in the activity log; do not leave it active.
- Night-audit page `/admin/night-audit` **loads but is NOT run**.

## 8. Rollback (if any step fails)
**Decision rule: any verification step fails and is not diagnosed within 20 minutes → roll back. No live debugging at 1 AM.**
```bash
sudo systemctl stop sheeza.service
# restore the pre-cutover DB (exact restore point from step 2)
sudo -u postgres dropdb sheeza_db
sudo -u postgres createdb -O sheeza sheeza_db
sudo -u postgres pg_restore --no-owner --role=sheeza -d sheeza_db /var/lib/postgresql/cutover_sheeza_db_${TS}.dump
# revert code
cd /var/www/sheeza-manzil && git checkout 3372dcf314fff01c2a6207e32a6f2081ee95573a
sudo sed -i 's/^APP_GIT_SHA=.*/APP_GIT_SHA=3372dcf314fff01c2a6207e32a6f2081ee95573a/' /var/www/sheeza-manzil/.env
sudo systemctl start sheeza.service
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/          # app back
# if public maintenance page was enabled (§1), disable it now.
```
Rollback is fully safe: prod pre-cutover dump + the old commit `3372dcf` are both known-good.

## 9. Expected timings (from rehearsal)
| Step | Rehearsed / expected |
|------|----------------------|
| pg_dump sheeza_db | < 1s (dump 47 KB) |
| alembic upgrade (17 migrations) | **0.38s** total (each ≤ 0.06s) |
| service stop + start | ~5–10s |
| verification | 2–4 min (manual browser checks) |
| **Total window** | **~3–5 min** (well within 00:15–01:30) |

Post-cutover: prod = 36 tables, `alembic_version = d6a2f59b8e34`, `/healthz` sha `8434eb42…`. Data preserved (rehearsal: 0 row drift across all pre-existing tables). Remaining owner item (separate): rotate the two prod account passwords (predate hardening).
