"""WSGI entry for the Pepper internal API (Phase 0).

Runs the internal API as a SEPARATE process from the public site, bound to a unix
domain socket owned by a dedicated UID — the internal routes are therefore never
exposed on the public TCP port. Deploy (gated) example:

    gunicorn --workers 1 --bind unix:/run/pepper/pepper.sock internal_wsgi:app

The bot connects with `curl --unix-socket /run/pepper/pepper.sock ...` + bearer.
Shares the same models/services/DB as the public app (own db.init_app binding).
"""

from __future__ import annotations

from flask import Flask

from config import Config
from app.models import db
from app.routes.internal_api import internal_api_bp


def create_internal_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    db.init_app(app)
    app.register_blueprint(internal_api_bp)
    return app


app = create_internal_app()
