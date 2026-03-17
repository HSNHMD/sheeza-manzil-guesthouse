import os
from datetime import timedelta

basedir = os.path.abspath(os.path.dirname(__file__))


def _fix_db_url(url: str) -> str:
    """
    Railway (and older Heroku) supply DATABASE_URL as 'postgres://...'
    but SQLAlchemy 1.4+ only accepts 'postgresql://...'.
    """
    if url and url.startswith('postgres://'):
        return url.replace('postgres://', 'postgresql://', 1)
    return url


class Config:
    # ── Security ───────────────────────────────────────────────────────────────
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-change-in-production'

    # ── Database ───────────────────────────────────────────────────────────────
    # Railway injects DATABASE_URL automatically when a Postgres plugin is added.
    # Falls back to local SQLite for development.
    _raw_db_url = os.environ.get('DATABASE_URL', '')
    SQLALCHEMY_DATABASE_URI = (
        _fix_db_url(_raw_db_url)
        if _raw_db_url
        else 'sqlite:///' + os.path.join(basedir, 'guesthouse.db')
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,       # reconnect on stale connections
        'pool_recycle': 300,         # recycle connections every 5 min
    }

    # ── Sessions ───────────────────────────────────────────────────────────────
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
    SESSION_COOKIE_SECURE = os.environ.get('FLASK_ENV') == 'production'
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'

    # ── WhatsApp ───────────────────────────────────────────────────────────────
    WHATSAPP_TOKEN = os.environ.get('WHATSAPP_TOKEN', '')
    WHATSAPP_PHONE_ID = os.environ.get('WHATSAPP_PHONE_ID', '')
    WHATSAPP_ENABLED = os.environ.get('WHATSAPP_ENABLED', 'false').lower() == 'true'

    # ── General ────────────────────────────────────────────────────────────────
    DEBUG = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
