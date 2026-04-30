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


# ── Registration ────────────────────────────────────────────────────────────
def register_cli(app):
    """Attach the `admin` and `brand` command groups to the Flask app's CLI."""
    app.cli.add_command(admin_cli)
    app.cli.add_command(brand_cli)
