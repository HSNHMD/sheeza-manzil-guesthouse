# Current state of the Guesthouse PMS

> **Last updated:** 2026-05-03 by recovery/handoff sprint.
> Treat anything in this file as authoritative for the date stamped above.
> Anything older may have drifted — `git log` and `flask db heads` are the
> ultimate fallback.

## TL;DR (read this in 30 seconds)

| Aspect | Truth |
|---|---|
| Active feature branch | `feature/reservation-board` |
| `main` is at | `3372dcf` — "feature: receive inbound WhatsApp messages" |
| `feature/reservation-board` is | **48 commits ahead of main, 0 behind** |
| Production deployed sha | `3372dcf` (matches main) |
| Staging deployed sha | `2c60810` — "feat(channels): OTA modification + cancellation handling V1" |
| Production domain | https://sheezamanzil.com |
| Staging domain | https://staging.husn.cloud |
| Production VPS path | `/var/www/sheeza-manzil` |
| Staging VPS path | `/var/www/guesthouse-staging` |
| Production service | `sheeza.service` |
| Staging service | `guesthouse-staging.service` |
| VPS host | Hostinger (root@187.127.112.36) |
| Latest migration head | `d6a2f59b8e34` (`add_channel_inbound_events`) |
| Test count | 1020 (1016 reliably green; 4 timezone flakes documented in `known_bugs.md`) |

The 48-commit gap means **the entire feature/reservation-board surface
(reservation board, mid-stay segments, business date / night audit,
cashiering, role-based landing, maintenance, channel manager, OTA import +
modification + cancellation) is staging-only and has never been deployed
to production.** Production is intentionally older / quieter.

## Branches in play

- `main` — production. Treated as immutable in normal operation.
  Pushes to `main` trigger a manual prod deploy via the runbook in
  `docs/runbooks/production_deploy.md`. Currently at `3372dcf`.
- `feature/reservation-board` — the rolling feature branch. Every
  staging-only sprint commits here. Long-lived by design.
- Older feature branches may exist on the remote — leave them alone.

## What is deployed where

### Production (`https://sheezamanzil.com`, sha `3372dcf`)
- Front office, bookings, guests, invoices, housekeeping, calendar
- WhatsApp inbound webhook + AI draft assistant
- Older accounting / reporting surfaces

### Staging (`https://staging.husn.cloud`, sha `2c60810`)
Everything in production PLUS the entire `feature/reservation-board`
stack:
- Reservation Board (drag/drop room rail)
- Mid-stay room change (StaySegment foundation)
- Role-based landing dispatcher + department dashboards
- Business Date + Night Audit V1
- Cashiering Polish + Payment Reconciliation V1
- Maintenance / Work Orders V1
- Channel Manager Foundation V1 (booking_com pilot)
- OTA Reservation Import + Exception Queue V1
- OTA Modification + Cancellation Handling V1

## Key services / modules — completion status

| Module | Status | Where it lives |
|---|---|---|
| Reservation Board | **Shipped to staging** | `app/routes/reservation_board.py` + `templates/board/` |
| Stay Segments | **Foundation shipped** (renderer landed; advanced split UX deferred) | `app/models.StaySegment`, `app/services/board.py` |
| Business Date / Night Audit | **Shipped to staging** | `app/services/night_audit.py`, `app/routes/night_audit.py` |
| Front Office (arrivals/departures/in-house) | **Shipped to staging** | `app/routes/front_office.py` |
| Cashiering + Payment Reconciliation | **Shipped to staging** | `app/services/cashiering.py`, `app/routes/cashiering.py` |
| Folio / Invoices | **Shipped to staging** | `app/models.Invoice`, `app/models.FolioItem` |
| POS (terminal + catalog) | **Shipped to staging** | `app/routes/pos.py` |
| Online Menu (QR ordering) | **Shipped to staging** | `app/routes/menu_orders.py` |
| Booking Engine (public) | **Shipped to staging** | `app/routes/booking_engine.py` |
| Housekeeping Board | **Shipped to staging** | `app/routes/housekeeping.py` |
| Maintenance / Work Orders | **Shipped to staging** | `app/services/maintenance.py`, `app/routes/maintenance.py` |
| Channel Manager Foundation | **Shipped to staging** (5 channel models, admin UI) | `app/services/channels.py`, `app/routes/channels.py` |
| OTA Reservation Import | **Shipped to staging** | `app/services/channel_import.import_reservation` |
| OTA Modify + Cancel | **Shipped to staging** | `app/services/channel_import.apply_modification` / `apply_cancellation` |
| WhatsApp inbound | **Shipped to PROD** | `app/routes/whatsapp_webhook.py` |
| AI draft assistant | **Shipped to PROD** | `app/services/ai_drafts.py` |
| Multi-property | **Foundation only** — `property_id` columns exist on wave-1 models, no UI | `app/models.Property`, `app/services/property.py` |

## Modules in progress / planned next

See `docs/handoff/next_steps.md` for the exact priority queue. Short
version: the open work is mostly in OTA territory (real outbound HTTP
clients, scheduled inbound poll, no-show events) and the
**production-merge spike** to bring main forward.

## Environment split: production vs staging

| Concern | Production | Staging |
|---|---|---|
| Path | `/var/www/sheeza-manzil` | `/var/www/guesthouse-staging` |
| systemd unit | `sheeza.service` | `guesthouse-staging.service` |
| Branch deployed | `main` | `feature/reservation-board` |
| DB | Postgres on Hostinger; `DATABASE_URL` in `.env` | Separate Postgres database; `DATABASE_URL` in `.env` |
| Domain | `sheezamanzil.com` (Certbot) | `staging.husn.cloud` (Certbot) |
| `STAGING=1` env var | unset | set → orange ribbon on every page |
| WhatsApp / Gemini | **Live tokens** in `.env` | Tokens stripped or sandbox-only |
| R2 / cloud uploads | Live bucket | Separate bucket |

The two databases are completely separate. Migrating staging never
touches production data. See `docs/runbooks/staging_setup.md` for the
authoritative setup notes.

## Current branding state

- **Production**: serves the original "Sheeza Manzil" identity (logo,
  short name, primary color) baked into `app/services/branding.py`.
- **Staging**: identity is fully overridable via `BRAND_*` env vars
  (`BRAND_NAME_OVERRIDE`, `BRAND_SHORT_NAME_OVERRIDE`, etc.). Currently
  staging displays a separate brand to prevent operators from mistaking
  staging for production at a glance.
- The orange "STAGING · build {SHA} · production untouched" ribbon at
  the top of every staging page is added when `STAGING=1` is set.

## Current architectural direction

1. **One repo, one Flask app, two deployments.** No microservices.
2. **Reservation Board is the front-office spine** — see
   `docs/decisions/0001-reservation-board-as-front-office-spine.md`.
3. **Folio is the money spine** — see
   `docs/decisions/0002-folio-as-money-spine.md`.
4. **Business date is separate from server time** — see
   `docs/decisions/0003-business-date-separate-from-server-time.md`.
5. **Staging-first workflow** — every sprint lands on staging before
   anything sees production. See
   `docs/decisions/0004-staging-first-workflow.md`.
6. **Mid-stay room change uses one Booking + N StaySegments** — see
   `docs/decisions/0005-stay-segments-for-mid-stay-room-change.md`.
7. **Property-aware schema before multi-property UI** — see
   `docs/decisions/0006-property-aware-before-multi-property.md` and
   `docs/multi_property_foundation_plan.md`.

## How to verify any of this for yourself

```bash
# What sha is in production right now?
ssh root@187.127.112.36 'cd /var/www/sheeza-manzil && git log -1 --oneline'

# What sha is on staging?
ssh root@187.127.112.36 'cd /var/www/guesthouse-staging && git log -1 --oneline'

# What's the latest migration?
flask --app run.py db heads

# Are services up?
ssh root@187.127.112.36 'systemctl is-active sheeza.service guesthouse-staging.service'
```

If any of those answers diverge from this file, **trust the live answer
and update this file**.
