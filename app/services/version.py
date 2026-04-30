"""Deployed-version probe.

Lightweight helpers that tell the live app which git commit it's running
from. Used by the /admin/diag page so operators can verify exactly what
landed on staging without SSH'ing into the box.

Resolution order (first match wins):
  1. APP_GIT_SHA env var          — set by the CI / deploy step
  2. .git/HEAD symbolic ref        — read at request time, no shell exec
  3. 'unknown'

We deliberately do NOT shell out to `git rev-parse`. That:
  - requires git installed at runtime
  - costs a fork per request
  - breaks in container builds where /.git is stripped
"""

from __future__ import annotations

import os
import pathlib


def deployed_sha(short: bool = True) -> str:
    """Return the SHA of the deployed commit, or 'unknown'."""
    env = (os.environ.get('APP_GIT_SHA') or '').strip()
    if env:
        return env[:12] if short else env

    sha = _read_git_head()
    if sha:
        return sha[:12] if short else sha

    return 'unknown'


def _read_git_head() -> str | None:
    """Read .git/HEAD without shelling out."""
    here = pathlib.Path(__file__).resolve()
    # Walk up looking for a .git directory (handles dev runs from
    # checked-out repo). Stops after 6 levels to avoid wandering up
    # the filesystem.
    for parent in [here, *here.parents][:6]:
        gitdir = parent / '.git'
        if not gitdir.exists():
            continue
        head = gitdir / 'HEAD'
        if not head.exists():
            return None
        try:
            ref = head.read_text(encoding='utf-8').strip()
        except OSError:
            return None
        if ref.startswith('ref: '):
            ref_path = gitdir / ref[5:].strip()
            if ref_path.exists():
                try:
                    return ref_path.read_text(encoding='utf-8').strip()
                except OSError:
                    return None
            return None
        # Detached HEAD — the file content is itself a SHA.
        return ref
    return None


def is_staging() -> bool:
    """True when the STAGING env var is set to a truthy value."""
    return (os.environ.get('STAGING') or '').lower() in ('1', 'true', 'yes', 'on')
