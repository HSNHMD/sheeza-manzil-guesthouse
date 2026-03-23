"""Upload files to Google Drive using a service account."""
import json
import logging
import os

log = logging.getLogger(__name__)

CREDS_ENV = 'GOOGLE_CREDENTIALS'
ID_FOLDER = 'Sheeza Manzil Guest IDs'
PAYMENT_FOLDER = 'Sheeza Manzil Payment Slips'


def _service():
    creds_json = os.environ.get(CREDS_ENV)
    if not creds_json:
        log.warning('[Drive] %s env var is not set — uploads disabled', CREDS_ENV)
        return None

    log.info('[Drive] %s env var found (%d chars)', CREDS_ENV, len(creds_json))

    try:
        info = json.loads(creds_json)
        log.info('[Drive] credentials JSON parsed OK — type=%s, project=%s, client_email=%s',
                 info.get('type'), info.get('project_id'), info.get('client_email'))
    except json.JSONDecodeError as exc:
        log.error('[Drive] Failed to parse %s as JSON: %s', CREDS_ENV, exc)
        return None

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:
        log.error('[Drive] Google library import failed: %s', exc)
        return None

    try:
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=['https://www.googleapis.com/auth/drive.file'])
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


def _upload_to_folder(file_path: str, filename: str, folder_name: str):
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
        folder_id = _get_or_create_folder(svc, folder_name)
        meta = {'name': filename, 'parents': [folder_id]}
        log.info('[Drive] Uploading %s (mime=%s) to folder_id=%s', filename, mime, folder_id)
        media = MediaFileUpload(file_path, mimetype=mime)
        f = svc.files().create(body=meta, media_body=media, fields='id,webViewLink').execute()
        file_id = f.get('id')
        web_link = f.get('webViewLink')
        log.info('[Drive] Upload success: file_id=%s link=%s', file_id, web_link)
        return file_id, web_link
    except Exception as exc:
        log.error('[Drive] Upload failed for %s: %s', filename, exc, exc_info=True)
        return None, None


def upload_id_card(file_path: str, filename: str):
    """Upload an ID card to 'Sheeza Manzil Guest IDs'. Returns (file_id, web_view_link) or (None, None)."""
    return _upload_to_folder(file_path, filename, ID_FOLDER)


def upload_payment_slip(file_path: str, filename: str):
    """Upload a payment slip to 'Sheeza Manzil Payment Slips'. Returns (file_id, web_view_link) or (None, None)."""
    return _upload_to_folder(file_path, filename, PAYMENT_FOLDER)
