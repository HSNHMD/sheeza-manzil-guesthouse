# External integrations

> **Last updated:** 2026-05-03.
> Anything that talks to the outside world. For each integration:
> what it does, where it's wired, what's safe in production vs
> staging, and the credentials / kill switches.

## WhatsApp Cloud API

### What it does
- **Inbound:** `/webhooks/whatsapp` receives Meta callbacks; messages
  are persisted to `WhatsAppMessage`. Inbox UI at `/admin/whatsapp`.
- **Outbound:** sends template + free-form messages via
  `app/services/whatsapp.py`.

### Where it's wired
- Routes: `app/routes/whatsapp_webhook.py`
- Service: `app/services/whatsapp.py`
- Models: `WhatsAppMessage`
- Template UI: `app/templates/whatsapp/`

### Production vs staging safety
- Production has live tokens in `.env`:
  - `WHATSAPP_TOKEN`
  - `WHATSAPP_APP_SECRET`
  - `WHATSAPP_PHONE_ID`
  - `WHATSAPP_WEBHOOK_VERIFY_TOKEN`
  - `WHATSAPP_ENABLED=1`
- Staging strips `WHATSAPP_ENABLED` (or sets it to `0`). When the
  service helper sees that var as falsy, all outbound network calls
  are skipped — `app/services/whatsapp.py` returns early with a
  no-op `Result`.
- **Test guard:** `tests/test_whatsapp_inbound.py` verifies the
  webhook path doesn't fire any HTTP when `WHATSAPP_ENABLED=0`.

### Kill switch
Set `WHATSAPP_ENABLED=0` in `.env` and restart the service. Inbound
webhooks still parse + persist to the inbox; outbound is silent.

### Webhook verification
Meta calls `/webhooks/whatsapp` with a `hub.verify_token` query
parameter. The route compares against `WHATSAPP_WEBHOOK_VERIFY_TOKEN`
in `.env`. If the token is wrong, the handler returns 403 (Meta
treats this as "endpoint not ready").

## AI draft assistant (Gemini today; provider-pluggable)

### What it does
When an inbound WhatsApp message arrives, the assistant drafts a
suggested reply for the operator to review. The operator can edit,
approve, or discard. Only approved drafts are sent.

### Where it's wired
- Service: `app/services/ai_drafts.py`
- Routes: `app/routes/whatsapp_webhook.py` (draft creation),
  `app/routes/whatsapp_webhook.py` (review + send)

### Provider abstraction
Configured via env:
- `AI_DRAFT_PROVIDER` — `gemini` (current production) or `mock`
  (returns a canned response; used in tests + staging).
- `AI_DRAFT_MODEL` — model identifier (e.g.
  `gemini-1.5-flash-latest`).
- `GEMINI_API_KEY` — API key. Absent on staging by default.

### Production vs staging safety
- Staging defaults to `AI_DRAFT_PROVIDER=mock` (or unsets
  `GEMINI_API_KEY`) so test inbound messages don't burn API quota.
- **Test guard:** the AST-based isolation tests in
  `tests/test_channel_import.py::IsolationTests` (and similar) refuse
  to let new code reach for `gemini` symbols by accident.

### Kill switch
Unset `GEMINI_API_KEY` or set `AI_DRAFT_PROVIDER=mock` and restart.
The assistant returns mock drafts so the inbox UI keeps working.

## OTA / Channel manager (current status)

### What it does
**V1 ships a complete schema + admin UI + sandbox forms** for the
booking_com pilot, but **makes zero outbound HTTP**. The "test sync"
button writes a `test_noop` `ChannelSyncJob` row + matching
`ChannelSyncLog` row.

### What's implemented (staging, no production yet)
| Pipeline | Service | Form | Status |
|---|---|---|---|
| Inbound import | `services.channel_import.import_reservation` | `/admin/channels/<id>/sandbox-import` (yellow card) | Working end-to-end on staging |
| Inbound modification | `services.channel_import.apply_modification` | `/admin/channels/<id>/sandbox-modify` (purple card) | Working end-to-end on staging |
| Inbound cancellation | `services.channel_import.apply_cancellation` | `/admin/channels/<id>/sandbox-cancel` (red card) | Working end-to-end on staging |
| Outbound availability push | (not implemented) | (n/a) | Deferred to next sprint |
| Outbound rate push | (not implemented) | (n/a) | Deferred to next sprint |
| Real OTA HTTP client | (not implemented) | (n/a) | Deferred — see `docs/handoff/next_steps.md` |

### Idempotency
`ChannelInboundEvent` has UNIQUE `(channel_connection_id,
external_event_id)`. Replaying the same OTA event short-circuits
with `result_status='duplicate_skipped'`.

### Safety guards
- AST isolation test in `tests/test_channel_import.py` refuses any
  reference to `requests`, `httpx`, `urllib`, `whatsapp`, `gemini`,
  or `anthropic` in `app/services/channel_import.py` /
  `app/routes/channel_exceptions.py`.
- `ChannelImportException.payload_summary` is sanitized — drops keys
  matching `password`, `secret`, `token`, `api_key`, `authorization`,
  `*_token`.
- Cancellation handler refuses to flip a booking with
  `Invoice.amount_paid > 0` — refunds are operator-only.

### Path to real OTA calls
See `docs/channel_manager_build_phases.md` for the 4-phase plan.
Booking.com sandbox is the pilot. `docs/handoff/next_steps.md` has
the priority queue and the hard prerequisites.

### Pilot channel
`booking_com`. The `CHANNEL_NAMES` whitelist in
`app/services/channels.py` allows `booking_com / expedia / agoda /
airbnb / other`, but only `booking_com` is exercised in V1 sandbox.

## Cloudflare R2 (object storage)

### What it does
Off-site storage for uploads (logos, room photos, attachments).

### Status
**Active for new uploads as of 2026-04-27** (per
`project_sheeza_r2_gap.md`):
- New uploads dual-write (local + R2).
- 9 historical files still need backfill.
- DB dumps to R2 are **not** yet automated.

### Where it's wired
- Service: a small uploads helper that calls `boto3` with
  `endpoint_url=<R2 endpoint>`. Credentials in `.env`:
  - `CLOUDFLARE_ACCOUNT_ID`
  - `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` (placeholder names —
    confirm against `app/services/uploads.py` if you're touching this)
  - `R2_BUCKET`

### Production vs staging safety
- Production points at the live bucket.
- Staging should point at a separate bucket. **If staging is silently
  writing to the production bucket, that's a misconfiguration.**

## Postgres (DB)

### What it does
Primary data store. SQLAlchemy ORM + Alembic migrations.

### Where it's wired
- Engine config in `config.py` from `.env DATABASE_URL`.
- Models in `app/models.py`.

### Production vs staging safety
- Two separate databases on the same Postgres server.
- The `guesthouse_staging` user is `REVOKE`'d from production
  database `sheeza_db`. (Per `reference_sheeza_vps.md`.)
- A staging migration cannot affect production data.

### Backups
See `docs/runbooks/backup_restore.md`.

## SMTP (email)

**Not currently wired.** Email is a TODO:
- Booking confirmations to guests — manual today.
- Operator alerts on `cancel_unsafe_state` / failed deploys —
  not implemented.

When email is added, document the provider here and the `.env` keys.

## Anything else external + sensitive

| Thing | Handled by | Sensitive? | Notes |
|---|---|---|---|
| Operator passwords | `User.set_password()` (Werkzeug PBKDF2) | Yes | Never logged; never stored plaintext. CLI: `flask admin reset-password`. |
| Card numbers | (none) | n/a | App is **not PCI scope**. All card payment is operator-recorded; no card numbers ever touch this app. |
| Guest IDs (passport / national_id) | `Guest.id_type` + `Guest.id_number` | Yes | Stored in DB. Never written to ActivityLog metadata. Rendered only on the booking detail page. |
| OTA tokens (future) | `.env` | Yes | NEVER in `ChannelConnection.config_json` (V1 enforces by convention; future sprint will hash + rotate). |

## How to add a new integration safely

1. Read `docs/decisions/0004-staging-first-workflow.md`.
2. Add the new service to `app/services/`.
3. Add a kill-switch env var (e.g. `NEW_INTEGRATION_ENABLED`).
4. Default the kill switch to `0` (off) in `.env.example`.
5. Add an AST isolation test that proves no other module imports the
   new client transitively (pattern: `tests/test_channel_import.py::
   IsolationTests`).
6. Add a sandbox / mock provider so tests + staging can exercise the
   pipeline without burning quota.
7. Document here.
