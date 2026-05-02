# Repeatable operator prompts

> **Last updated:** 2026-05-03.
> Reusable prompt skeletons for common operator tasks. Fill in the
> placeholders. Each is paired with the runbook that contains the
> actual procedure.

## Backup before risky change

```
Take a snapshot of <production | staging> before <RISKY THING>.

Steps:
1. SSH to root@<VPS>.
2. cd /var/www/<deployment-dir>.
3. set -a && source .env && set +a (do not echo .env values).
4. ts=$(date -u +%Y%m%dT%H%M%SZ); mkdir -p /root/backups/$ts
5. pg_dump "$DATABASE_URL" | gzip > /root/backups/$ts/db.sql.gz
6. tar -czf /root/backups/$ts/uploads.tgz uploads/
7. git rev-parse HEAD > /root/backups/$ts/code.sha
8. ls -la /root/backups/$ts/  — confirm db.sql.gz is non-zero.

Report:
- timestamp directory
- db dump size
- code sha captured
- production untouched timestamp
```

Runbook: `docs/runbooks/backup_restore.md`.

## Production deploy

```
Deploy <FEATURE / SHA> to production.

Pre-flight:
- Confirm I have read docs/handoff/current_state.md.
- Confirm a fresh backup exists (from the Backup prompt above).
- Confirm CI / local tests are green.
- Confirm a maintenance window if migrations lock tables.

Steps (each must complete before the next):
1. Locally: git checkout main; git merge --no-ff <branch>; git push.
2. SSH; cd /var/www/sheeza-manzil; git pull --ff-only origin main.
3. set -a && source .env && set +a; source venv/bin/activate.
4. flask db current; flask db heads; flask db upgrade.
5. systemctl restart sheeza.service; sleep 3; is-active.
6. journalctl -u sheeza.service -n 100 — no tracebacks.
7. curl /admin/diag — sha matches deployed sha.
8. Click through dashboard, board, arrivals, invoices.
9. Update docs/handoff/current_state.md with the new sha.

Final report:
1. branch / sha
2. migration head before / after
3. service active timestamp before / after
4. health-check codes
5. anything anomalous in the log tail
```

Runbook: `docs/runbooks/production_deploy.md`.

## Rollback

```
Roll back production from <BAD SHA> to <PREVIOUS SHA>.

Decision tree:
- 5xx errors but no schema change → code-only rollback (Section A).
- Schema change involved → STOP, read rollback.md Section B
  before continuing.

Code-only rollback:
1. SSH; cd /var/www/sheeza-manzil.
2. git fetch origin.
3. git reset --hard <previous-sha>.
4. systemctl restart sheeza.service; is-active.
5. curl /admin/diag — sha matches the rollback target.

After rollback:
- File a bug in docs/handoff/known_bugs.md.
- Open a fix branch from main (NOT from the bad sha).

Report:
- rolled-back sha
- service health
- whether DB rollback was needed (yes/no/partial)
```

Runbook: `docs/runbooks/rollback.md`.

## Read-only verification (no writes)

```
Read-only verification of <ENVIRONMENT>.

Steps:
1. SSH (read-only intent — do not modify anything).
2. systemctl is-active <service>.
3. systemctl show <service> -p ActiveEnterTimestamp --value.
4. cd /var/www/<dir>; git log -1 --oneline.
5. flask db current.
6. psql "$DATABASE_URL" -c "SELECT
       'bookings' AS t, COUNT(*) FROM bookings
       UNION ALL SELECT 'invoices', COUNT(*) FROM invoices
       UNION ALL SELECT 'users', COUNT(*) FROM users;"
7. journalctl -u <service> -n 50 — any tracebacks?

Report:
- service status + uptime
- deployed sha
- migration head
- table row counts
- any error tail
- production untouched: confirmed (with timestamp)
```

## Staging verification after a sprint

```
Verify the <SPRINT NAME> sprint on staging.

Pre-flight:
- I have the staging URL list from docs/handoff/staging_urls.md.

Steps:
1. Open https://staging.husn.cloud/admin/diag — sha matches the
   sprint's commit hash.
2. Orange staging ribbon visible.
3. Click through every URL the sprint touched (use the staging URLs
   list to find them).
4. Run the sprint-specific live probe (see the sprint's final report).
5. SSH and confirm the relevant ActivityLog actions fired:
       psql "$DATABASE_URL" -c "
         SELECT action, COUNT(*)
         FROM activity_logs
         WHERE created_at > NOW() - INTERVAL '1 hour'
           AND action LIKE '<prefix>.%'
         GROUP BY action;
       "

Report:
- which URLs rendered cleanly
- which URLs 5xx'd (if any)
- ActivityLog row counts
- production untouched: confirmed
```

## Shell / UI correction request

```
Fix <SPECIFIC UI ISSUE> on <ENVIRONMENT> page <PATH>.

Constraints:
- The fix is UI-only; no schema changes.
- Visual regression must not affect adjacent components.
- If the fix touches design-system.css, document the change in the
  same commit message.

Acceptance:
- Page renders without console errors at desktop + mobile breakpoints.
- All other tests still green.
```

## Module sprint kickoff (use this verbatim)

The pattern in `docs/prompts/pai_build_prompts.md` is the source of
truth for sprint kickoff prompts. To start a new sprint, copy the
template from there.

## "Tell me what state we're in" (cold-start prompt)

When you've lost context (new session, new developer, new assistant)
and need to get oriented in 60 seconds:

```
Read these files in order and give me a 5-bullet summary of where
the project stands today:

1. docs/handoff/current_state.md
2. docs/handoff/next_steps.md
3. docs/handoff/known_bugs.md

Then run:
- git log --oneline -5
- ssh root@<vps> 'cd /var/www/sheeza-manzil && git log -1'
- ssh root@<vps> 'cd /var/www/guesthouse-staging && git log -1'

Tell me:
- what sha is in production right now
- what sha is on staging right now
- what's the immediate next sprint
- what's blocking it
- what bugs are known
```

This prompt is the recovery story. If it fails to produce a useful
answer, the docs in `docs/handoff/` need an update — update them
before continuing.
