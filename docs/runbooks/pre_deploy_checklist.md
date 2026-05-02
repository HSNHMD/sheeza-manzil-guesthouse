# Pre-deploy checklist (production)

> Print this. Check the boxes. If you can't check one, stop and fix
> it before continuing.

```
[ ] I have read docs/handoff/current_state.md.
[ ] I know what sha is currently in production.
[ ] I know what sha I'm about to deploy and have read every commit
    message between them.
[ ] CI / `python -m unittest discover tests` is green locally.
[ ] No untracked files in my working tree (`git status` clean).
[ ] No uncommitted .env / secrets in the diff.
[ ] A fresh DB backup exists on the VPS at /root/backups/<ts>/.
[ ] The backup includes db.sql.gz, uploads.tgz, and code.sha.
[ ] `gunzip -t /root/backups/<ts>/db.sql.gz` exits 0 (the dump is
    not corrupt).
[ ] If the deploy includes a schema migration that locks any table:
    a maintenance window is scheduled and announced.
[ ] If the deploy touches WhatsApp, AI drafts, or OTAs: I have
    confirmed the staging-vs-prod env split is intact.
[ ] I have docs/runbooks/rollback.md open in another tab.
[ ] I know the previous-sha rollback target.
[ ] An operator or co-developer is reachable in case I need a
    second pair of eyes.
[ ] (Optional) I have tagged the previous prod sha as
    `prod-<date>-pre-<feature>` for easy reference.
```

When all boxes are checked, follow `docs/runbooks/production_deploy.md`.
