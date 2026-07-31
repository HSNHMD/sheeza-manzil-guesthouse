"""Pepper — Sheeza PMS Telegram bot (Phase 1 skeleton).

A SEPARATE process from the Flask PMS. It holds NO database credentials and never
imports the PMS code — all data access is via the internal API over the unix
socket (bearer). Telegram privacy mode stays ON: the bot works purely from
commands, force-reply threads, and inline-button callbacks; it never relies on
reading arbitrary group messages.
"""

__version__ = "0.1.0"
