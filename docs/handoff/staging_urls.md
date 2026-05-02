# Staging URLs — what to test

> **Last updated:** 2026-05-03.
> Production URLs intentionally not listed here — production is for
> live operations, not exploratory testing. See
> `docs/runbooks/production_deploy.md` for the prod entry points.

## Base + auth

| What | URL |
|---|---|
| Staging base | https://staging.husn.cloud |
| Admin login | https://staging.husn.cloud/appadmin |
| Staff login | https://staging.husn.cloud/console |
| Logout | https://staging.husn.cloud/logout |
| Public privacy page | https://staging.husn.cloud/privacy |
| Build/version diag | https://staging.husn.cloud/admin/diag |

The orange `STAGING · build {SHA} · production untouched` ribbon should
appear on every authenticated page. If it doesn't, the `STAGING=1` env
var is not set on the staging deployment — fix before continuing.

## Front office

| What | URL |
|---|---|
| Dashboard (post-login landing for admins) | /dashboard/ |
| Front office overview | /front-office/ |
| Arrivals (today) | /front-office/arrivals |
| Departures (today) | /front-office/departures |
| In house | /front-office/in-house |
| Reservation Board (admin) | /board/ |
| Bookings list | /bookings/ |
| Guests | /guests/ |

## Housekeeping

| What | URL |
|---|---|
| Housekeeping board | /housekeeping/ |
| Rooms | /rooms/ |
| Maintenance / work orders | /maintenance/ |

## Restaurant / POS

| What | URL |
|---|---|
| POS terminal | /pos/ |
| POS catalog admin | /pos/admin/ |
| Online menu queue | /menu/admin/ |
| Public guest menu (QR) | /menu/ |

## Accounting

| What | URL |
|---|---|
| Accounting overview | /accounting/ |
| Invoices | /invoices/ |
| Expenses | /accounting/expenses |
| P&L | /accounting/pl |
| Bank reconciliation | /accounting/reconciliation/ |
| Payment reconciliation | /accounting/reconciliation/payments |
| Tax | /accounting/tax |
| Reports | /reports/ |
| Night Audit | /night-audit/ |

## Channel Manager (admin-only)

| What | URL |
|---|---|
| Connections list | /admin/channels/ |
| New connection | /admin/channels/new |
| Channel detail | /admin/channels/<id> |
| Channel exception queue | /admin/channel-exceptions/ |
| Exception detail | /admin/channel-exceptions/<id> |

The booking_com sandbox lives at `/admin/channels/<id>` and includes:
- "Test sync" (V1 no-op — writes a `test_noop` ChannelSyncJob)
- "Sandbox reservation import" form (yellow card — drives
  `import_reservation`)
- "Sandbox modification" form (purple card — drives
  `apply_modification`)
- "Sandbox cancellation" form (red card — drives
  `apply_cancellation`)
- "Linked bookings" reference table

None of those forms make outbound HTTP. They all run the local
pipeline and either create / update a Booking, hit the
`channel_inbound_events` dedup, or queue a
`channel_import_exceptions` row.

## Admin / settings

| What | URL |
|---|---|
| Property identity | /property/ |
| Property settings | /property-settings/ |
| Inventory + rates | /inventory/ |
| Audit log | /admin/activity |
| Users / Roles | /admin/users |
| WhatsApp inbox | /admin/whatsapp |

## Cross-checks an operator should run after any deploy

1. `/admin/diag` — version stamp matches the SHA you just deployed.
2. `/dashboard/` — admin login completes without a 500.
3. `/board/` — drag a booking to a clean room, drop it, refresh,
   verify it stuck.
4. `/front-office/arrivals` — count matches the booking list filtered
   on today's check_in_date.
5. `/maintenance/` — KPI tiles render; create a probe work order,
   resolve it, verify the activity log entry.
6. `/admin/channels/` — sandbox import → booking → sandbox modify →
   sandbox cancel → exception queue (the `docs/runbooks/post_deploy.md`
   checklist walks through this).

If any of those fail, see `docs/runbooks/rollback.md`.
