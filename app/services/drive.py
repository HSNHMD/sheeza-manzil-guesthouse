"""Upload files to Google Drive using OAuth user credentials.

Requires these Railway env vars:
  GOOGLE_OAUTH_CLIENT_ID       — OAuth app client ID
  GOOGLE_OAUTH_CLIENT_SECRET   — OAuth app client secret
  GOOGLE_OAUTH_REFRESH_TOKEN   — long-lived refresh token for the Drive account owner

Folder IDs (copy from Drive URL after sharing with your OAuth account):
  GOOGLE_DRIVE_ID_FOLDER_ID       — folder ID for 'Sheeza Manzil Guest IDs'
  GOOGLE_DRIVE_PAYMENT_FOLDER_ID  — folder ID for 'Sheeza Manzil Payment Slips'
"""
import io
import logging
import os

log = logging.getLogger(__name__)

ID_FOLDER_NAME = 'Sheeza Manzil Guest IDs'
PAYMENT_FOLDER_NAME = 'Sheeza Manzil Payment Slips'


def _service():
    """Build an authenticated Drive v3 service using OAuth user credentials."""
    client_id     = os.environ.get('GOOGLE_OAUTH_CLIENT_ID', '').strip()
    client_secret = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET', '').strip()
    refresh_token = os.environ.get('GOOGLE_OAUTH_REFRESH_TOKEN', '').strip()

    missing = [k for k, v in [
        ('GOOGLE_OAUTH_CLIENT_ID', client_id),
        ('GOOGLE_OAUTH_CLIENT_SECRET', client_secret),
        ('GOOGLE_OAUTH_REFRESH_TOKEN', refresh_token),
    ] if not v]

    if missing:
        log.warning('[Drive] Missing env vars: %s — uploads disabled', ', '.join(missing))
        return None

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as exc:
        log.error('[Drive] Google library import failed: %s', exc)
        return None

    try:
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri='https://oauth2.googleapis.com/token',
            scopes=['https://www.googleapis.com/auth/drive'],
        )
        creds.refresh(Request())
        log.info('[Drive] OAuth credentials refreshed OK')
        svc = build('drive', 'v3', credentials=creds)
        log.info('[Drive] Drive v3 service built successfully')
        return svc
    except Exception as exc:
        log.error('[Drive] Failed to build Drive service: %s', exc, exc_info=True)
        return None


def _set_public_readable(svc, file_id: str):
    """Make uploaded file viewable by anyone with the link."""
    try:
        svc.permissions().create(
            fileId=file_id,
            body={'role': 'reader', 'type': 'anyone'},
            fields='id',
        ).execute()
        log.info('[Drive] Set public-readable on file_id=%s', file_id)
    except Exception as exc:
        log.warning('[Drive] Could not set public permission on %s: %s', file_id, exc)


def _upload(file_path: str, filename: str, folder_id: str) -> tuple[str | None, str | None]:
    """Upload file_path into folder_id. Returns (file_id, web_view_link) or (None, None)."""
    svc = _service()
    if not svc:
        return None, None

    try:
        from googleapiclient.http import MediaIoBaseUpload

        ext = filename.rsplit('.', 1)[-1].lower()
        mime = {'pdf': 'application/pdf', 'png': 'image/png',
                'jpg': 'image/jpeg', 'jpeg': 'image/jpeg'}.get(ext, 'application/octet-stream')

        with open(file_path, 'rb') as fh:
            file_bytes = fh.read()

        log.info('[Drive] Uploading %s (%d bytes, mime=%s) to folder_id=%s',
                 filename, len(file_bytes), mime, folder_id)

        media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime, resumable=False)
        f = svc.files().create(
            body={'name': filename, 'parents': [folder_id]},
            media_body=media,
            fields='id,webViewLink',
        ).execute()

        file_id = f.get('id')
        web_link = f.get('webViewLink')
        log.info('[Drive] Upload success: file_id=%s link=%s', file_id, web_link)
        _set_public_readable(svc, file_id)
        return file_id, web_link

    except Exception as exc:
        log.error('[Drive] Upload failed for %s: %s', filename, exc, exc_info=True)
        return None, None


def upload_id_card(file_path: str, filename: str) -> tuple[str | None, str | None]:
    """Upload an ID card. Returns (file_id, web_view_link) or (None, None)."""
    folder_id = os.environ.get('GOOGLE_DRIVE_ID_FOLDER_ID', '').strip()
    if not folder_id:
        log.warning('[Drive] GOOGLE_DRIVE_ID_FOLDER_ID not set — cannot upload ID card')
        return None, None
    return _upload(file_path, filename, folder_id)


def upload_payment_slip(file_path: str, filename: str) -> tuple[str | None, str | None]:
    """Upload a payment slip. Returns (file_id, web_view_link) or (None, None)."""
    folder_id = os.environ.get('GOOGLE_DRIVE_PAYMENT_FOLDER_ID', '').strip()
    if not folder_id:
        log.warning('[Drive] GOOGLE_DRIVE_PAYMENT_FOLDER_ID not set — cannot upload payment slip')
        return None, None
    return _upload(file_path, filename, folder_id)
