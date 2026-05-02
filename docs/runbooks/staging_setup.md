# Staging setup runbook

> **Last updated:** 2026-05-03.
> Snapshot of the current staging environment as it actually exists on
> the VPS. If you stand up a new staging from scratch, follow these
> notes; if you're just connecting, jump to "Quick connect".

## Quick connect

```bash
# SSH (key-based — never paste a password)
ssh root@187.127.112.36

# App lives at:
cd /var/www/guesthouse-staging

# Logs:
journalctl -u guesthouse-staging.service -n 200 --no-pager
journalctl -u guesthouse-staging.service -f       # follow

# DB (Postgres connection string is in /var/www/guesthouse-staging/.env;
# DO NOT echo .env values here — load them into env via `source`)
set -a && source .env && set +a
psql "$DATABASE_URL"
```

## What lives where

| Path / unit | Purpose |
|---|---|
| `/var/www/guesthouse-staging/` | app working dir; `git pull` here to deploy |
| `/var/www/guesthouse-staging/.env` | secrets file; mode 600; root-owned; never commit |
| `/var/www/guesthouse-staging/venv/` | Python 3.12 venv; `pip install -r requirements.txt` |
| `/var/www/guesthouse-staging/uploads/` | per-instance uploads (kept separate from prod) |
| `/etc/systemd/system/guesthouse-staging.service` | systemd unit |
| `/etc/nginx/sites-enabled/guesthouse-staging` | nginx vhost (TLS via Certbot) |
| Postgres database | separate database from production; URL is in `.env` `DATABASE_URL` |

## systemd unit (current)

```
WorkingDirectory=/var/www/guesthouse-staging
EnvironmentFile=/var/www/guesthouse-staging/.env
ExecStart=/var/www/guesthouse-staging/venv/bin/gunicorn ...
```

Restart safely:
```bash
sudo systemctl restart guesthouse-staging.service
sudo systemctl is-active guesthouse-staging.service
```

## Domain

- **Hostname:** `staging.husn.cloud`
- **TLS:** Certbot-managed via the nginx vhost
  `/etc/nginx/sites-enabled/guesthouse-staging`.
- See `docs/runbooks/domain_dns_ssl.md` for renewal + DNS notes.

## Required environment differences vs production

The staging `.env` ADDS or OVERRIDES these vars compared to prod:

| Var | Why |
|---|---|
| `STAGING=1` | Flips the orange ribbon on every page + the diag page |
| `APP_ENV=staging` | Branding / behavior flags can fork on this |
| `APP_GIT_SHA` | Stamped into HTML footer; refreshed by deploy script |
| `BRAND_NAME_OVERRIDE` | Prevents staging from showing "Sheeza Manzil" wordmark |
| `BRAND_SHORT_NAME_OVERRIDE` | Same — short variant |
| `BRAND_PRIMARY_COLOR_OVERRIDE` | Same — distinct accent |
| `BRAND_LOGO_PATH_OVERRIDE` | Same — distinct logo |
| `DATABASE_URL` | Points at the staging Postgres, NOT prod |
| `WHATSAPP_*` tokens | Either absent or sandbox-only — staging must not message real guests |
| Cloud R2 bucket | Separate from prod — see `project_sheeza_r2_gap.md` |

## Branding behavior on staging

`app/services/branding.py` reads the `BRAND_*_OVERRIDE` env vars at
context-processor time (per-request). When set:
- `BRAND_NAME_OVERRIDE` displaces "Sheeza Manzil" everywhere it appears
  (wordmark, page titles, login screen).
- `BRAND_SHORT_NAME_OVERRIDE` displaces the abbreviated form.
- `BRAND_PRIMARY_COLOR_OVERRIDE` overrides the default `#7B3F00` accent.
- `BRAND_LOGO_PATH_OVERRIDE` swaps the logo asset.

If staging starts displaying the production brand, check that the
overrides are still in `.env` and that the service was restarted
after editing.

## What is disabled on staging

| Feature | How it's disabled |
|---|---|
| Real WhatsApp send | `WHATSAPP_ENABLED=0` in staging `.env`, OR sandbox tokens, OR no token at all. The `whatsapp` service skips network calls when the env var is falsy. |
| Real OTA HTTP | The channel manager has zero outbound HTTP code in V1. The "Test sync" button writes a no-op `ChannelSyncJob` — this is enforced by the test in `tests/test_channels.py::NoExternalCouplingTests`. |
| Real R2 production bucket | Staging uploads go to a separate bucket (or local disk if R2 isn't wired). |
| AI draft live model calls | Set `AI_DRAFT_PROVIDER` to `mock` (or unset GEMINI_API_KEY) to stop network calls. |

## Standing up a fresh staging from zero

```bash
# 1. Create the Postgres database
sudo -u postgres createuser guesthouse_staging
sudo -u postgres createdb -O guesthouse_staging guesthouse_staging
# Set a password; revoke from production user. Update .env DATABASE_URL.

# 2. Clone the repo + venv
sudo mkdir -p /var/www/guesthouse-staging
sudo chown -R deploy:deploy /var/www/guesthouse-staging
cd /var/www/guesthouse-staging
git clone https://github.com/HSNHMD/sheeza-manzil-guesthouse.git .
git checkout feature/reservation-board

python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. .env (copy from .env.example, fill in placeholders)
cp .env.example .env
# Edit .env — set DATABASE_URL, SECRET_KEY, STAGING=1, BRAND_*_OVERRIDE
# Mode 600 + root-owned:
sudo chown root:root .env
sudo chmod 600 .env

# 4. Migrate
set -a && source .env && set +a
export FLASK_APP=run.py
flask db upgrade

# 5. Seed admin
flask admin create   # follow prompts; never paste production passwords

# 6. systemd unit
sudo cp /var/www/guesthouse-staging/scripts/staging-systemd.template \
        /etc/systemd/system/guesthouse-staging.service
sudo systemctl daemon-reload
sudo systemctl enable --now guesthouse-staging.service

# 7. nginx + Certbot
# See docs/runbooks/domain_dns_ssl.md
```

## Smoke-test checklist after any staging deploy

1. https://staging.husn.cloud/admin/diag — sha matches what you pushed.
2. Orange staging ribbon visible.
3. Admin login works.
4. `/board/` renders.
5. `/front-office/arrivals` renders without 500.
6. `/maintenance/` renders KPI tiles.
7. `/admin/channels/` renders.

If anything is red, see `docs/runbooks/rollback.md`.
