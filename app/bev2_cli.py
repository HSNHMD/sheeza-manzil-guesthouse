"""Booking Engine V2 — CLI commands for the sweep + invariant (cron/timer).

  flask hold-sweep            # expire due holds (status transition + audit)
  flask inventory-invariant   # assert holds+pending+assigned <= sellable

Both write a JSON status file (the digest/status-file pathway used by the
nightly backup — reachable even where the Telegram bot isn't provisioned on
this box). The invariant command exits NON-ZERO and logs a HIGH-severity audit
entry on any violation, so a systemd timer surfaces it.

Wiring (documented in the report): a systemd timer runs
  ExecStart=/var/www/.../venv/bin/flask inventory-invariant
nightly and the status file / non-zero exit reaches the digest.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

import click
from flask.cli import with_appcontext

_STATUS_DIR = os.environ.get('BEV2_STATUS_DIR', '/var/backups')


def _write_status(name, payload):
    try:
        os.makedirs(_STATUS_DIR, exist_ok=True)
        with open(os.path.join(_STATUS_DIR, name), 'w') as fh:
            json.dump(payload, fh)
    except OSError:
        pass  # status file is best-effort; never crash the command on it


@click.command('hold-sweep')
@with_appcontext
def hold_sweep_cmd():
    """Expire holds past their TTL (state transition + audit; never deletes)."""
    from .services.holds import sweep_expired
    out = sweep_expired()
    _write_status('bev2-hold-sweep-status.json',
                  {'ok': True, 'expired': out['expired'],
                   'ran_at': out['ran_at'].isoformat()})
    click.echo(f"hold-sweep: {out['expired']} hold(s) expired at "
               f"{out['ran_at'].isoformat()}")


@click.command('inventory-invariant')
@click.option('--horizon-days', default=180, show_default=True)
@with_appcontext
def invariant_cmd(horizon_days):
    """Assert the inventory invariant; exit 1 + HIGH audit on any violation."""
    from .services.inventory import invariant_violations
    from .services.audit import log_activity
    from .models import db

    violations = invariant_violations(horizon_days=horizon_days)
    ran_at = datetime.utcnow().isoformat()
    _write_status('bev2-invariant-status.json',
                  {'ok': not violations, 'violations': len(violations),
                   'detail': violations[:20], 'ran_at': ran_at})

    if violations:
        log_activity('inventory.invariant_violation', actor_type='system',
                     description=(f'HIGH: inventory invariant violated on '
                                  f'{len(violations)} (type, night) cell(s).'),
                     metadata={'violation_count': len(violations),
                               'first': json.dumps(violations[0])[:200]})
        db.session.commit()
        click.echo(f"INVARIANT VIOLATION: {len(violations)} cell(s) oversold "
                   f"(HIGH). See bev2-invariant-status.json.", err=True)
        raise SystemExit(1)

    click.echo(f"inventory-invariant OK: 0 violations over {horizon_days}d "
               f"at {ran_at}")


def register_bev2_cli(app):
    app.cli.add_command(hold_sweep_cmd)
    app.cli.add_command(invariant_cmd)
