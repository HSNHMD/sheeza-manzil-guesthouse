"""Flask CLI commands for safe admin user management.

Replaces the previously-hardcoded `_seed_admin()` auto-seed in `app/__init__.py`.
There is NO default admin password anywhere in the codebase. Initial admin
must be created explicitly via:

    flask --app run.py admin create

To rotate an existing admin's password without touching app code or the DB
directly, use:

    flask --app run.py admin reset-password --username <name>

Both commands prompt for the password via getpass (hidden input), require
confirmation, refuse weak passwords, and never echo the password back or
log it.
"""

from __future__ import annotations  # PEP 563 — enables `str | None` on Py 3.7+

from getpass import getpass

import click
from flask.cli import AppGroup

from .models import db, User


admin_cli = AppGroup('admin', help='Admin user management (create, reset-password).')


# ── Password validation policy ──────────────────────────────────────────────
_MIN_PASSWORD_LEN = 12
_BANNED_PASSWORDS = {
    'admin123', 'password', 'password123', 'changeme',
    'admin', 'sheeza', 'sheezamanzil', '12345678', '123456789', '1234567890',
    'qwerty', 'qwerty123', 'letmein',
}


def _validate_password(pw: str) -> str | None:
    """Return None if pw is acceptable, else a short reason string."""
    if pw is None or pw == '':
        return 'password cannot be empty'
    if pw != pw.strip():
        return 'password must not have leading/trailing whitespace'
    if len(pw) < _MIN_PASSWORD_LEN:
        return f'password must be at least {_MIN_PASSWORD_LEN} characters'
    if pw.lower() in _BANNED_PASSWORDS:
        return 'password is on the common-passwords blocklist'
    return None


def _read_password_with_confirm() -> str | None:
    """Prompt for password twice via getpass; return the password or None."""
    pw = getpass('Password: ')
    err = _validate_password(pw)
    if err is not None:
        click.echo(f'  ✗ {err}', err=True)
        return None
    pw_confirm = getpass('Confirm password: ')
    if pw != pw_confirm:
        click.echo('  ✗ passwords do not match', err=True)
        return None
    return pw


# ── flask admin create ──────────────────────────────────────────────────────
@admin_cli.command('create')
def admin_create():
    """Create the initial admin user.

    Refuses to run if any admin user already exists. Use `reset-password`
    to change an existing admin's password.
    """
    existing = User.query.filter_by(role='admin').first()
    if existing is not None:
        click.echo(
            '  ✗ refusing to create — an admin user already exists. '
            "Use 'flask admin reset-password --username <name>' to change a password.",
            err=True,
        )
        raise click.Abort()

    username = click.prompt('Username', type=str, default='admin').strip()
    if not username or len(username) > 64:
        click.echo('  ✗ username must be 1–64 characters', err=True)
        raise click.Abort()

    if User.query.filter_by(username=username).first() is not None:
        click.echo(f'  ✗ a user with username={username!r} already exists', err=True)
        raise click.Abort()

    email = click.prompt('Email', type=str).strip()
    if not email or '@' not in email or len(email) > 120:
        click.echo('  ✗ email looks invalid', err=True)
        raise click.Abort()

    if User.query.filter_by(email=email).first() is not None:
        click.echo(f'  ✗ a user with email={email!r} already exists', err=True)
        raise click.Abort()

    pw = _read_password_with_confirm()
    if pw is None:
        raise click.Abort()

    u = User(username=username, email=email, role='admin')
    u.set_password(pw)
    db.session.add(u)
    db.session.commit()

    click.echo('  ✓ admin created')


# ── flask admin reset-password ──────────────────────────────────────────────
@admin_cli.command('reset-password')
@click.option('--username', required=True, help='Username of the admin whose password to reset.')
def admin_reset_password(username: str):
    """Reset an existing admin user's password.

    Targets users with role='admin' only. Will not create new users; will
    not touch staff users; will not modify any column other than password_hash.
    """
    u = User.query.filter_by(username=username, role='admin').first()
    if u is None:
        click.echo(f'  ✗ no admin user found with username={username!r}', err=True)
        raise click.Abort()

    if not u.is_active:
        click.echo(f'  ✗ admin user {username!r} is deactivated; reactivate it first', err=True)
        raise click.Abort()

    pw = _read_password_with_confirm()
    if pw is None:
        raise click.Abort()

    u.set_password(pw)
    db.session.commit()

    click.echo('  ✓ password reset')


# ── flask brand ─────────────────────────────────────────────────────────────
brand_cli = AppGroup(
    'brand',
    help='Property branding (display name, short name, color, logo).'
)


@brand_cli.command('show')
def brand_show():
    """Print the active PropertySettings row in human-readable form."""
    from .services.property_settings import get_settings
    s = get_settings()
    click.echo(f'  PropertySettings row id={s.id}')
    click.echo(f'    property_name : {s.property_name!r}')
    click.echo(f'    short_name    : {s.short_name!r}')
    click.echo(f'    tagline       : {s.tagline!r}')
    click.echo(f'    primary_color : {s.primary_color!r}')
    click.echo(f'    logo_path     : {s.logo_path!r}')
    click.echo(f'    currency_code : {s.currency_code!r}')
    click.echo(f'    timezone      : {s.timezone!r}')
    click.echo(f'    updated_at    : {s.updated_at}')


@brand_cli.command('set')
@click.option('--name',          help='Full property name (e.g. "Maakanaa Village Hotel").')
@click.option('--short',         help='Short name shown in the header (e.g. "Maakanaa").')
@click.option('--tagline',       help='Optional tagline.')
@click.option('--color',         help='Primary color (hex, e.g. #0d9488).')
@click.option('--logo-path',     help='Static URL path to the logo image.')
@click.option('--invoice-name',  help='Display name on invoices (defaults to --name).')
def brand_set(name, short, tagline, color, logo_path, invoice_name):
    """Update the singleton PropertySettings row.

    Only fields you pass on the command line are touched. Values are
    written to the DB (safe upsert via get_settings autoseed). Use
    `flask brand show` first to see the current values.

        flask --app run.py brand set \\
            --name "Maakanaa Village Hotel" --short "Maakanaa"
    """
    from .services.property_settings import get_settings
    s = get_settings()

    fields = {
        'property_name':        name,
        'short_name':           short,
        'tagline':              tagline,
        'primary_color':        color,
        'logo_path':            logo_path,
        'invoice_display_name': invoice_name,
    }
    changed = []
    for col, new_val in fields.items():
        if new_val is None:
            continue
        new_val = new_val.strip()
        if col == 'primary_color' and new_val and not new_val.startswith('#'):
            new_val = '#' + new_val
        if getattr(s, col) != new_val:
            setattr(s, col, new_val)
            changed.append(col)

    if not changed:
        click.echo('  · no changes (no flags supplied or values already match)')
        return

    db.session.commit()
    click.echo(f'  ✓ updated {len(changed)} field(s): {", ".join(changed)}')
    click.echo('    Run `flask brand show` to verify, then restart the app:')
    click.echo('      sudo systemctl restart sheeza.service')


# ── flask staging ──────────────────────────────────────────────────────────
#
# Idempotent staging-only data seeding. Every command in this group refuses
# to run unless STAGING=1 is set in the environment — production (which
# never sets STAGING) is structurally protected even if someone copy-paste-
# runs a command in the wrong shell.
import os
import json

staging_cli = AppGroup(
    'staging',
    help='Staging-only data seeding (rooms, room types, POS catalog).'
)


def _require_staging():
    """Refuse to run unless STAGING=1. Production safety lock."""
    if os.environ.get('STAGING') != '1':
        click.echo(
            '  ✗ refusing to run — STAGING=1 not set in environment.\n'
            '    These commands mutate seed data and must NEVER touch '
            'production.',
            err=True,
        )
        raise click.Abort()


# ── Room layout (35 rooms, 5 types) ─────────────────────────────────────────
#
# Type tuple: (number, type_code, name, floor, capacity, price_mvr)
# Floors:
#   1 → 101-110 (10 Standard)
#   2 → 201-212 (10 Deluxe + 2 Twin)
#   3 → 301-311 (6 Family + 4 Deluxe + 1 Twin)
#   4 → 401-402 (2 Suite)
_ROOM_LAYOUT = (
    # Floor 1 — Standard
    *[(f'{100+i}', 'STD', 'Standard Room', 1, 2, 800.0) for i in range(1, 11)],
    # Floor 2 — Deluxe (10) + Twin (2)
    *[(f'{200+i}', 'DEL', 'Deluxe Room',   2, 2, 1200.0) for i in range(1, 11)],
    *[(f'{200+i}', 'TWI', 'Twin Room',     2, 2, 1100.0) for i in range(11, 13)],
    # Floor 3 — Family (6) + Deluxe (4) + Twin (1)
    *[(f'{300+i}', 'FAM', 'Family Room',   3, 4, 1800.0) for i in range(1, 7)],
    *[(f'{300+i}', 'DEL', 'Deluxe Room',   3, 2, 1200.0) for i in range(7, 11)],
    ('311',       'TWI', 'Twin Room',     3, 2, 1100.0),
    # Floor 4 — Suite
    ('401',       'STE', 'Suite',         4, 4, 3500.0),
    ('402',       'STE', 'Suite',         4, 4, 3500.0),
)


# Room types catalog. (code, name, base_capacity, max_occupancy)
_ROOM_TYPES = (
    ('STD', 'Standard',  2, 2),
    ('DEL', 'Deluxe',    2, 2),
    ('TWI', 'Twin',      2, 2),
    ('FAM', 'Family',    4, 4),
    ('STE', 'Suite',     4, 4),
)


@staging_cli.command('seed-rooms')
def staging_seed_rooms():
    """Reseed staging rooms to 35 across 4 floors with 5 room types.

    Renumbers existing rooms 1-8 to 101-108 (preserves any booking
    FKs since FK is via rooms.id, not the number string). Adds 27
    more rooms to reach 35. Idempotent: safe to re-run.
    """
    _require_staging()

    from .models import db, Room, RoomType

    # 1. Ensure RoomType catalog has all 5 types.
    type_ids = {}
    for code, name, base_cap, max_cap in _ROOM_TYPES:
        rt = RoomType.query.filter_by(code=code).first()
        if rt is None:
            rt = RoomType(code=code, name=name,
                          base_capacity=base_cap, max_occupancy=max_cap,
                          is_active=True)
            db.session.add(rt)
            db.session.flush()
            click.echo(f'  + room_type {code} {name!r}')
        else:
            # Refresh metadata in case the names drifted.
            rt.name = name
            rt.base_capacity = base_cap
            rt.max_occupancy = max_cap
            rt.is_active = True
        type_ids[code] = rt.id
    db.session.commit()

    # 2. Renumber legacy rooms 1-8 to 101-108. Maps to the first 8 of
    #    the Floor-1 Standard layout, so existing bookings on the
    #    "Deluxe" rooms 1-6 will now show as Standard rooms 101-106.
    #    This is an intentional staging rebrand — historical bookings
    #    keep working but the type/floor catches up to the new layout.
    renumber = {str(i): f'10{i}' for i in range(1, 9)}  # 1→101 … 8→108
    for old, new in renumber.items():
        r = Room.query.filter_by(number=old).first()
        if r is None:
            continue
        # Don't collide with a room already at the new number.
        if Room.query.filter_by(number=new).first():
            continue
        r.number = new
        click.echo(f'  ↻ room {old} → {new}')
    db.session.flush()

    # 3. Apply the canonical layout to every target row (insert or
    #    update).
    for number, code, name, floor, cap, price in _ROOM_LAYOUT:
        r = Room.query.filter_by(number=number).first()
        if r is None:
            r = Room(
                number=number, name=name, room_type=name,
                room_type_id=type_ids[code],
                floor=floor, capacity=cap, price_per_night=price,
                amenities='WiFi, AC, TV, En-suite Bathroom',
                is_active=True,
            )
            db.session.add(r)
            click.echo(f'  + room {number} {name} (floor {floor})')
        else:
            r.name = name
            r.room_type = name
            r.room_type_id = type_ids[code]
            r.floor = floor
            r.capacity = cap
            r.price_per_night = price
            r.is_active = True
    db.session.commit()

    total = Room.query.filter_by(is_active=True).count()
    click.echo(f'  ✓ {total} active rooms on staging '
               f'({len(_ROOM_LAYOUT)} in canonical layout)')


# ── POS catalog ─────────────────────────────────────────────────────────────

# 13 categories — sort_order spaced by 10 so the operator can insert
# new categories between existing ones without renumbering.
_POS_CATEGORIES = (
    ( 10, 'Soups & Starters'),
    ( 20, 'Salads'),
    ( 30, 'Chicken Mains'),
    ( 40, 'Beef & Lamb Mains'),
    ( 50, 'Fish & Seafood'),
    ( 60, 'Pasta, Rice & Pizza'),
    ( 70, 'Breakfast'),
    ( 80, 'Desserts'),
    ( 90, 'Hot Beverages'),
    (100, 'Cold Coffee & Shakes'),
    (110, 'Juices & Smoothies'),
    (120, 'Sodas & Water'),
    (130, 'Specialty'),
)


@staging_cli.command('seed-pos-categories')
def staging_seed_pos_categories():
    """Create the 13 staging POS categories.

    Idempotent: existing rows with the same name are updated in
    place (sort_order, is_active). New rows are inserted.
    """
    _require_staging()

    from .models import db, PosCategory

    for sort_order, name in _POS_CATEGORIES:
        c = PosCategory.query.filter_by(name=name).first()
        if c is None:
            c = PosCategory(name=name, sort_order=sort_order, is_active=True)
            db.session.add(c)
            click.echo(f'  + category {name!r}')
        else:
            c.sort_order = sort_order
            c.is_active = True
    db.session.commit()

    total = PosCategory.query.filter_by(is_active=True).count()
    click.echo(f'  ✓ {total} active POS categories')


@staging_cli.command('seed-scenarios')
@click.option('--clean/--no-clean', default=True,
              help='Wipe existing bookings/folios/orders before reseeding '
                   '(default: --clean). Use --no-clean to add on top of '
                   'existing demo data.')
def staging_seed_scenarios(clean):
    """Seed realistic boutique-island-property demo scenarios.

    Creates ~30 guests and 33 bookings spanning every status (in-house,
    arrivals/departures today, future confirmed, pending payment,
    cancelled, checked-out), with folios, partial payments, a discount,
    a void, housekeeping states, two out-of-order rooms, and ~9 POS
    orders. Anchored on date.today() so the scenario stays realistic
    on whatever day the seeder runs.
    """
    _require_staging()

    from .services.staging_scenarios import run
    summary = run(clean=clean)

    click.echo(f'  ✓ scenario seeded for {summary["today"]}')
    if summary.get('wiped'):
        wiped_str = ', '.join(f'{k}={v}' for k, v in summary['wiped'].items() if v)
        click.echo(f'  · wiped: {wiped_str}' if wiped_str else '  · wiped: nothing')
    click.echo(f'  · guests created:    {summary["guests_created"]}')
    click.echo(f'  · bookings total:    {summary["bookings_total"]}')
    for status, count in sorted(summary['bookings_by_status'].items()):
        click.echo(f'      {status:<24} {count}')
    click.echo(f'  · in-house now:      {summary["in_house_now"]}')
    click.echo(f'  · arrivals today:    {summary["arrivals_today"]}')
    click.echo(f'  · departures today:  {summary["departures_today"]}')
    click.echo(f'  · rooms dirty:       {summary["rooms_dirty"]}')
    click.echo(f'  · rooms inspected:   {summary["rooms_inspected"]}')
    click.echo(f'  · rooms out of order:{summary["rooms_out_of_order"]}')
    click.echo(f'  · invoices paid/partial/unpaid: '
               f'{summary["invoices_paid"]}/{summary["invoices_partial"]}/'
               f'{summary["invoices_unpaid"]}')
    click.echo(f'  · folio items total: {summary["folio_items_total"]} '
               f'(voided: {summary["folio_items_voided"]})')
    click.echo(f'  · POS orders:        {summary["pos_orders_total"]}')


@staging_cli.command('import-pos-items')
@click.argument('json_path', type=click.Path(exists=True, dir_okay=False))
@click.option('--deactivate-missing', is_flag=True,
              help='Mark items not in the JSON as is_active=False '
                   '(for clean reseeding).')
def staging_import_pos_items(json_path, deactivate_missing):
    """Import POS menu items from a JSON file.

    JSON shape:

        {
          "Soups & Starters": [
            {"name": "Chicken Soup", "price": 95.0, "description": "..."},
            ...
          ],
          "Hot Beverages": [
            {"name": "Espresso",     "price": 35.0}
          ]
        }

    Categories must already exist (run `seed-pos-categories` first).
    Items are upserted by (category_id, name) pair: existing rows
    have their price/description/sort_order updated, missing rows
    are inserted.
    """
    _require_staging()

    from .models import db, PosCategory, PosItem

    with open(json_path, 'r', encoding='utf-8') as fh:
        data = json.load(fh)

    if not isinstance(data, dict):
        click.echo('  ✗ JSON must be an object {category: [items]}', err=True)
        raise click.Abort()

    inserted = 0
    updated = 0
    skipped_missing_cat = []
    seen_pairs = set()

    for cat_name, items in data.items():
        cat = PosCategory.query.filter_by(name=cat_name).first()
        if cat is None:
            skipped_missing_cat.append(cat_name)
            continue
        for sort_idx, raw in enumerate(items, start=1):
            name = (raw.get('name') or '').strip()
            if not name:
                continue
            try:
                price = float(raw.get('price', 0))
            except (TypeError, ValueError):
                click.echo(f'  ! {cat_name} / {name}: bad price '
                           f'{raw.get("price")!r}', err=True)
                continue
            description = (raw.get('description') or '').strip() or None
            seen_pairs.add((cat.id, name))

            existing = PosItem.query.filter_by(
                category_id=cat.id, name=name
            ).first()
            if existing is None:
                db.session.add(PosItem(
                    category_id=cat.id, name=name, price=price,
                    description=description,
                    sort_order=sort_idx * 10,
                    is_active=True,
                ))
                inserted += 1
            else:
                existing.price = price
                existing.description = description
                existing.sort_order = sort_idx * 10
                existing.is_active = True
                updated += 1

    deactivated = 0
    if deactivate_missing:
        for it in PosItem.query.all():
            if (it.category_id, it.name) not in seen_pairs and it.is_active:
                it.is_active = False
                deactivated += 1

    db.session.commit()

    click.echo(f'  ✓ inserted {inserted}, updated {updated}, '
               f'deactivated {deactivated}')
    if skipped_missing_cat:
        click.echo(f'  ! categories not found (run seed-pos-categories '
                   f'first): {sorted(set(skipped_missing_cat))}', err=True)


# ── Registration ────────────────────────────────────────────────────────────
def register_cli(app):
    """Attach the `admin`, `brand`, and `staging` command groups."""
    app.cli.add_command(admin_cli)
    app.cli.add_command(brand_cli)
    app.cli.add_command(staging_cli)
