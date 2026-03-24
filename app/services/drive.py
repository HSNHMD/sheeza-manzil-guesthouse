"""Upload files to Google Drive using a service account."""
import json
import logging
import os

log = logging.getLogger(__name__)

CREDS_ENV = 'GOOGLE_CREDENTIALS'
ID_FOLDER = 'Sheeza Manzil Guest IDs'
PAYMENT_FOLDER = 'Sheeza Manzil Payment Slips'

# Optional: hardcode folder IDs owned by a real Google user (shared with the service account).
# Service accounts have no storage quota, so they MUST upload into user-owned folders.
# Set these in Railway env vars after sharing the folders with the service account email.
ID_FOLDER_ID_ENV = 'GOOGLE_DRIVE_ID_FOLDER_ID'
PAYMENT_FOLDER_ID_ENV = 'GOOGLE_DRIVE_PAYMENT_FOLDER_ID'


def _parse_credentials(creds_json: str) -> dict | None:
    """Parse GOOGLE_CREDENTIALS JSON with fallback for Railway shell-interpolation edge case.

    Railway sometimes stores env vars where the JSON content has real newlines
    (not \\n escapes) if the value was set via shell expansion. This breaks
    json.loads because JSON strings cannot contain literal newlines.
    """
    # Strategy 1: parse as-is (correct for properly stored JSON)
    try:
        info = json.loads(creds_json)
        log.info('[Drive] Credentials parsed OK (strategy 1) — type=%s client_email=%s',
                 info.get('type'), info.get('client_email'))
        return info
    except json.JSONDecodeError as exc:
        log.warning('[Drive] Strategy 1 JSON parse failed: %s — trying strategy 2', exc)

    # Strategy 2: escape real newlines inside the string before parsing
    # Covers the case where shell interpolation turned \n escapes into real newlines
    try:
        cleaned = creds_json.replace('\r\n', '\\n').replace('\n', '\\n').replace('\r', '\\n')
        info = json.loads(cleaned)
        log.info('[Drive] Credentials parsed OK (strategy 2 — newline cleanup) — client_email=%s',
                 info.get('client_email'))
        return info
    except json.JSONDecodeError as exc2:
        log.error('[Drive] Both JSON parse strategies failed. Strategy 2 error: %s', exc2)
        log.error('[Drive] Raw value starts with: %r', creds_json[:80])
        return None


def _service():
    creds_json = os.environ.get(CREDS_ENV)
    if not creds_json:
        log.warning('[Drive] %s env var is not set — uploads disabled', CREDS_ENV)
        return None

    log.info('[Drive] %s env var found (%d chars)', CREDS_ENV, len(creds_json))

    info = _parse_credentials(creds_json.strip())
    if info is None:
        return None

    # Normalize private_key: after parsing, check if literal \n slipped through
    if 'private_key' in info and '\\n' in info['private_key']:
        info['private_key'] = info['private_key'].replace('\\n', '\n')
        log.info('[Drive] Normalized private_key literal \\n sequences')

    log.info('[Drive] Credential fields — type=%s project=%s email=%s private_key_len=%d',
             info.get('type'), info.get('project_id'), info.get('client_email'),
             len(info.get('private_key', '')))

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:
        log.error('[Drive] Google library import failed: %s', exc)
        return None

    try:
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=['https://www.googleapis.com/auth/drive'])
        log.info('[Drive] Service account credentials created for %s', info.get('client_email'))
    except Exception as exc:
        log.error('[Drive] Failed to create service account credentials: %s', exc)
        return None

    try:
        log.info('[Drive] Building Drive v3 API service...')
        svc = build('drive', 'v3', credentials=creds)
        log.info('[Drive] Drive API service built successfully')
        return svc
    except Exception as exc:
        log.error('[Drive] Failed to build Drive API service: %s', exc)
        return None


def _get_or_create_folder(svc, folder_name):
    log.info('[Drive] Searching for folder: "%s"', folder_name)
    try:
        res = svc.files().list(
            q=f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields='files(id, name)'
        ).execute()
        files = res.get('files', [])
        if files:
            folder_id = files[0]['id']
            log.info('[Drive] Found existing folder "%s" with id=%s', folder_name, folder_id)
            return folder_id
        log.info('[Drive] Folder "%s" not found — creating it', folder_name)
        meta = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder'}
        folder = svc.files().create(body=meta, fields='id').execute()
        folder_id = folder['id']
        log.info('[Drive] Created folder "%s" with id=%s', folder_name, folder_id)
        return folder_id
    except Exception as exc:
        log.error('[Drive] Error in _get_or_create_folder("%s"): %s', folder_name, exc)
        raise


def _set_public_readable(svc, file_id: str):
    """Grant anyone-with-link read access so the webViewLink actually works."""
    try:
        svc.permissions().create(
            fileId=file_id,
            body={'role': 'reader', 'type': 'anyone'},
            fields='id'
        ).execute()
        log.info('[Drive] Set public-readable permission on file_id=%s', file_id)
    except Exception as exc:
        log.warning('[Drive] Could not set public permission on %s: %s', file_id, exc)


def _resolve_folder_id(svc, folder_name: str, env_var: str) -> str:
    """Return folder ID from env var (user-owned, has quota) or fall back to name lookup."""
    hardcoded = os.environ.get(env_var, '').strip()
    if hardcoded:
        log.info('[Drive] Using env-var folder_id=%s for "%s"', hardcoded, folder_name)
        return hardcoded
    log.warning('[Drive] %s not set — falling back to name-based lookup (may hit quota error)', env_var)
    return _get_or_create_folder(svc, folder_name)


def _upload_to_folder(file_path: str, filename: str, folder_name: str, folder_id_env: str):
    """Upload a file to a named Drive folder. Returns (file_id, web_view_link) or (None, None)."""
    log.info('[Drive] Upload requested: file=%s folder="%s"', filename, folder_name)
    svc = _service()
    if not svc:
        log.warning('[Drive] No Drive service available — skipping upload of %s', filename)
        return None, None

    try:
        from googleapiclient.http import MediaFileUpload
        ext = filename.rsplit('.', 1)[-1].lower()
        mime = {'pdf': 'application/pdf', 'png': 'image/png',
                'jpg': 'image/jpeg', 'jpeg': 'image/jpeg'}.get(ext, 'application/octet-stream')
        folder_id = _resolve_folder_id(svc, folder_name, folder_id_env)
        meta = {'name': filename, 'parents': [folder_id]}
        log.info('[Drive] Uploading %s (mime=%s) to folder_id=%s', filename, mime, folder_id)
        media = MediaFileUpload(file_path, mimetype=mime)
        f = svc.files().create(body=meta, media_body=media, fields='id,webViewLink').execute()
        file_id = f.get('id')
        web_link = f.get('webViewLink')
        log.info('[Drive] Upload success: file_id=%s link=%s', file_id, web_link)
        _set_public_readable(svc, file_id)
        return file_id, web_link
    except Exception as exc:
        log.error('[Drive] Upload failed for %s: %s', filename, exc, exc_info=True)
        return None, None


def upload_id_card(file_path: str, filename: str):
    """Upload an ID card to 'Sheeza Manzil Guest IDs'. Returns (file_id, web_view_link) or (None, None)."""
    return _upload_to_folder(file_path, filename, ID_FOLDER, ID_FOLDER_ID_ENV)


def upload_payment_slip(file_path: str, filename: str):
    """Upload a payment slip to 'Sheeza Manzil Payment Slips'. Returns (file_id, web_view_link) or (None, None)."""
    return _upload_to_folder(file_path, filename, PAYMENT_FOLDER, PAYMENT_FOLDER_ID_ENV)
