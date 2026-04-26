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


# ── Registration ────────────────────────────────────────────────────────────
def register_cli(app):
    """Attach the `admin` command group to the Flask app's CLI."""
    app.cli.add_command(admin_cli)
