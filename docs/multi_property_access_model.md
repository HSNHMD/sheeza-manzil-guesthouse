# Multi-Property — Access Model (planning only)

> **Status:** PLANNING DOCUMENT.
> Companion to `docs/multi_property_foundation_plan.md` (the data
> design) and `docs/multi_property_migration_strategy.md` (the
> rollout phases).
>
> **Purpose:** define how users authenticate, what properties they can
> reach, what they can do once inside, and how the active-property
> context is resolved on every request.
>
> **Hard rules:**
> 1. A user may have access to **zero, one, or many** properties.
> 2. A user's role is **per-property**, not global.
> 3. **Super-admin** is the only platform-level role and is a
>    deliberate, separately-flagged thing.
> 4. The active-property resolver is the **only** place that decides
>    which property a request operates on. Routes never decide.


## 1. Current state (today, single-property)

The current `User` model has just two roles:

```
User
├─ id, username, email, password_hash, role
├─ role ∈ {'admin', 'staff'}
└─ is_admin (computed from role == 'admin')
```

Permissions today are coarse:

- `admin` → can reach `/admin/*` URLs, manage users, run Night
  Audit, edit catalog, etc.
- `staff` → bounced from `/admin/*` by `_staff_guard`; can use
  Reservation Board, Front Office, Housekeeping (whitelisted),
  POS terminal, and the public-facing Online Menu admin queue
  (no — that's admin too).

This is fine for one property. For multi-property it does not
scale: there is no "this admin user runs property A but only
read-only on property B."


## 2. Future role taxonomy

Two layers: **platform role** + **per-property role**. Most users
have only the per-property role layer.

### Platform-level roles

| Role | Granted via | What it can do |
|---|---|---|
| `super_admin` | `User.is_platform_admin = True` | Create / disable / delete properties; manage all users; view fleet rollup reports; view ANY property by switching context. **No write actions in cross-property context** — see §6. |
| (none) | every other user | Cannot reach `/admin/fleet/*` or `/admin/properties/*`. Their world is per-property. |

### Per-property roles

Set on the `UserPropertyMembership` row (see §4).

| Role | Use case | Permitted areas (per-property) |
|---|---|---|
| `property_admin` | Owner / GM | Everything inside that property: settings, users, Night Audit, catalog, reports, reservation board, folio admin. |
| `front_office` | Reception staff | Reservation Board, Front Office (Arrivals / Departures / In-house), Bookings (read + create + edit), Folios (post charges), Cashiering (post payments), Online Menu admin queue, Housekeeping (read only). |
| `housekeeping` | Cleaning / maintenance staff | Housekeeping board only. Read-only on Reservation Board. No money actions. |
| `restaurant` | F&B staff | POS terminal + Online Menu admin queue + their own POS catalog (read). No room / booking writes. |
| `accounting` | Bookkeeper | Reports, Invoices, Folios (read), Cashiering (read), Expenses, Bank Transactions, Reconciliation, Tax. **No** booking creation / cancellation. |
| `read_only` | Auditor / external accountant / read-only staff | Every page in read mode. No POSTs. Useful for handing temporary access without granting write privileges. |

V1's `admin` becomes `property_admin`; V1's `staff` becomes
`front_office`. The migration backfills these mappings.

### Why per-property and not global

A property owner who runs two properties wants to grant the same
person `property_admin` on property A but only `accounting` on
property B. A global role can't represent that. Per-property roles
are the simplest design that captures real-world reality without
inventing a "permissions matrix" object.


## 3. The single super-admin role

`super_admin` is a sharp tool. Defaults:

- **Read-only across properties by default.** A super-admin viewing
  property B that they're not also a `property_admin` of can SEE
  but not WRITE. To take destructive actions they must explicitly
  switch into the property's context AND have a property-admin
  membership for it.
- **No magic god-mode.** The query layer still scopes by
  `current_property()`. The super-admin's only superpower is the
  fleet-rollup page + the ability to create / configure new
  properties.
- **Explicit confirmation for cross-property destructive actions.**
  E.g. promoting a property to channel-manager production requires
  the super-admin to type the property's `slug` to confirm — same
  pattern as Night Audit's "type the closing date" guard.
- **Audit trail flagged.** Every action a super-admin takes carries
  `actor_role='super_admin'` in the ActivityLog metadata so
  property owners can see "who outside our property touched our
  data."


## 4. Models

### `UserPropertyMembership`

```
UserPropertyMembership
├─ id
├─ user_id        FK users.id    (CASCADE)
├─ property_id    FK properties.id (CASCADE)
├─ role           string in PER_PROPERTY_ROLES
├─ is_primary     bool — exactly one TRUE per user (their default
│                  "home" property when they log in without a
│                  session preference)
├─ is_active      bool — soft-revocation
├─ created_at, updated_at
├─ created_by_user_id FK users.id (NULL on auto-seed)
└─ Unique (user_id, property_id)
```

Notes:

- A user with zero memberships and `is_platform_admin=False` cannot
  reach any property. The login page tells them to contact an admin.
- A user can be `property_admin` on property A and `read_only` on
  property B. The role is read **per request** based on the active
  property.
- Removing a user's last membership does NOT delete the User row.
  Reactivating later restores access without rebuilding history.

### `User` (extensions)

```
User (existing)
├─ ...everything we have today
├─ is_platform_admin   bool, default False  (NEW — replaces nothing,
│                       additive)
├─ default_property_id FK properties.id, nullable (NEW — used when
│                       session has no active property; falls back to
│                       primary membership)
└─ deactivated_at      datetime, nullable (NEW — for full-account
                        soft-disable, separate from per-membership)
```


## 5. Active-property resolution

`services.property_context.current_property()` is the **only**
function that returns the active Property row. It checks, in order:

1. **Test override.** If `flask.g._property_override` is set (used
   by integration tests), return it.
2. **URL prefix.** If the route is mounted under
   `/p/<slug>/...`, look up by slug. Cache on `g._property` for the
   request. (V1: this prefix is not yet used; reserved for V3+.)
3. **Subdomain.** If `request.host` matches a known property's
   slug-as-subdomain (`<slug>.<base_domain>`), return that
   property. (V4+ only; not enabled by default.)
4. **Session.** If `session['active_property_id']` is set AND the
   user has an active membership for that property OR is
   super-admin, use it.
5. **User default.** If logged in, return
   `User.default_property_id` (or the user's primary membership).
6. **Single-property fallback.** If exactly one Property row
   exists in the DB, return it. This is V1–V2's normal path.
7. **Raise.** No active property could be resolved. The middleware
   handler shows a "no property selected" page.

The resolver is called by:

- A `before_request` middleware that stashes the result on `g`.
- Background jobs explicitly via `current_property(property_id=...)`.

Direct `Property.query.get(...)` calls in route handlers are
forbidden by lint check (Phase 4 mentions adding it).


## 6. Permission rules per route group

### Read rules
- A user may **read** a property if AND ONLY IF:
  - they have an active `UserPropertyMembership` for that property,
    OR
  - `User.is_platform_admin = True`.
- The blanket "is_admin?" check that today gates `/admin/*` becomes
  `user_role_in(current_property()) == 'property_admin'` OR
  `is_platform_admin`.

### Write rules
- A user may **write** to a property if their per-property role
  permits the specific action.
- Super-admins do NOT get implicit write access. They must have a
  property_admin membership on the target property (or switch into
  the context after explicit grant).

### Settings rules
- `/admin/property-settings/` (the form for editing the active
  property) requires `property_admin` on the active property.
  Super-admin without property_admin sees read-only.
- `/admin/properties/` (the list of all properties; create / disable
  / configure SAS-level metadata) requires `is_platform_admin`.

### Fleet rollup rules
- `/admin/fleet/` requires `is_platform_admin`. Returns aggregated
  reports across explicitly-passed property IDs only — never an
  un-scoped "all properties" sum unless the super-admin explicitly
  ticks every property.

### Background jobs
- Night Audit, sync workers, scheduled reports run **per property**.
  They take `property_id` as required argument. Scope-less queries
  are forbidden.
- A cron entry that closes Night Audit at midnight enumerates active
  properties and runs the worker once per property — explicit loop,
  not implicit broadcast.


## 7. The decorator surface (proposed)

```
@login_required                          (Flask-Login built-in)

@property_member_required                 (any role on active property)
@property_role_required('front_office',
                        'property_admin') (any of the named roles)
@property_admin_required                  (just property_admin)
@super_admin_required                     (is_platform_admin=True)

@property_member_or_super_admin           (covers super-admin viewing
                                            another property read-only)
```

Most routes today use `@admin_required` — that becomes
`@property_admin_required` after Phase 4 of the migration. The
decorator function lives in `app/decorators.py` (already exists)
and reads `current_property()` to decide.


## 8. Login + session flow

### Today (single-property)
1. POST `/auth/login` with username + password.
2. On success, Flask-Login stores `_user_id` in session.
3. `_staff_guard` enforces "staff cannot reach /admin/*."

### Future (multi-property)
1. POST `/auth/login` (unchanged).
2. After credentials check, look up user's memberships:
   - Zero memberships AND not platform_admin → reject login with
     "no properties assigned, contact an admin."
   - Exactly one membership → set `session['active_property_id']`
     to that property; redirect to property dashboard.
   - Multiple memberships → redirect to `/auth/select-property`
     with a list. User picks; session is set.
   - Platform admin without memberships → redirect to
     `/admin/fleet/`.
3. Property switcher in the top bar:
   - Visible only when the user has access to ≥ 2 properties.
   - Clicking sets `session['active_property_id']` to the new
     property AND redirects to that property's dashboard. NOT
     just changing context silently — a redirect makes the URL
     reflect reality.

### Single-property fallback
If the env var `MULTI_PROPERTY_ENABLED=False` (or the DB has only
one Property), the entire flow degenerates to today's behaviour.
The login page does NOT show a property selector; the active
property is implicit.


## 9. Boundary cases

### What if a super-admin removes themselves from every property?
They can still reach `/admin/fleet/` and `/admin/properties/*` (the
platform routes). They cannot edit any single property's data
without re-granting themselves a membership.

### What if a property is disabled (`is_active=False`)?
- Existing memberships are kept but read-only.
- The property is hidden from non-super-admin users' switcher.
- Existing data remains intact.
- Booking creation, folio writes, cashier transactions, OTA syncs
  are blocked on the disabled property.
- Re-enabling restores write access. Soft-delete only — no actual
  rows are removed.

### What if a User row is deleted?
- All memberships cascade-delete.
- `Booking.created_by` etc. become NULL (FK is `ondelete='SET NULL'`).
- Audit log rows keep `actor_user_id` but the user lookup will fail
  — UI shows "(deleted user)".
- Recommendation: prefer **deactivation** over deletion. The
  `User.deactivated_at` column exists for this; deletion is an
  emergency-only operation.

### What if a property has no `property_admin`?
- Loud error in admin dashboards: "property has no admin — assign
  one before [X feature]." Channel manager work would refuse to
  run, for instance.
- Super-admin can always assign a new property_admin.

### How does WhatsApp inbox routing work in multi-property?
- Each property has its own `whatsapp_number`. Inbound messages
  arriving at that number land in that property's inbox.
- The webhook handler matches the destination number against
  active properties; sends it to a "manual triage" inbox if no
  match. This is a Channel-Manager-adjacent design problem;
  fully resolved when the inbound webhook is rebuilt for
  multi-tenant routing.


## 10. Backfill plan (Phase 6 of the migration)

When `UserPropertyMembership` is introduced, the migration:

1. Creates the table.
2. For every existing `User` with `role='admin'`, inserts a
   membership row: `property_id=1`, `role='property_admin'`,
   `is_primary=True`, `is_active=True`.
3. For every existing `User` with `role='staff'`, inserts:
   `property_id=1`, `role='front_office'`, `is_primary=True`,
   `is_active=True`.
4. Sets `User.default_property_id = 1` for every user.
5. **Does NOT** set `is_platform_admin` automatically. That flag
   must be granted manually via a one-shot `flask admin
   set-platform-admin <username>` CLI command — same caution as
   the existing `flask admin create` ceremony.

After this migration, every existing user keeps the access they had
before; they just have it via a `UserPropertyMembership` row instead
of via the legacy `User.role` column. The `User.role` column itself
stays, populated for backwards-compat and human readability, but
the per-request authoritative role lookup goes through
`UserPropertyMembership`.


## 11. Audit / observability

Every action writes ActivityLog with these whitelist keys:

- `actor_user_id` — who did it.
- `actor_role` — `super_admin` | per-property role string.
- `property_id` — the property the action was performed on (NULL
  only for platform-level events such as creating a new Property).
- existing per-action keys (booking_ref, etc.).

The activity-log filter UI gains a `property_id` filter so a
property owner can see only their property's events.


## 12. Things this access model does NOT solve

Acknowledged gaps; intentionally deferred:

- **Property-scoped API tokens** (per-property service accounts,
  e.g. for a third-party reporting tool that connects to one
  property only). Future work.
- **Two-factor / SSO.** Out of scope; layered on top.
- **Approval workflows** (e.g. cancellation requires manager
  approval). Out of scope.
- **Time-bounded memberships** (a contractor with admin access for
  the next 30 days). Could be added as `expires_at` on
  `UserPropertyMembership`; deferred until needed.
- **Audit log retention per property.** The platform already keeps
  audit rows; per-property retention policies are a Phase 8 topic.
- **Per-property branding for emails.** Will need to read from the
  active property; CRM sprint will own this.


## 13. Compatibility checklist

Before any code lands for the access-model migration phase, every
existing route handler and template must be reviewed against this
table:

| Today | Future |
|---|---|
| `current_user.is_admin` | `is_property_admin(current_user, current_property())` OR `current_user.is_platform_admin` |
| `@admin_required` | `@property_admin_required` (most cases) OR `@super_admin_required` (platform-level only) |
| `_staff_guard` whitelist | replaced by per-route role decorators |
| `User.role` reads | `user_role_in(user, property)` |
| Background jobs without context | `(property_id, ...)` parameter explicit |
| Cross-property reads anywhere | refactored through `services.fleet_reports.aggregate_for(properties=[...])` |

---

*Document owner: PMS architecture. Last updated: 2026-04-30.*
