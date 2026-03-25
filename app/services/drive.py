"""Google Drive upload service.

Uploads booking documents (ID cards, payment slips) and expense receipts to a
shared Google Drive folder tree:

    SheezaManzil/
        ID Cards/
        Payment Slips/
        Receipts/

Requires the GOOGLE_CREDENTIALS environment variable to contain the full JSON
content of a Google service account key with Drive API access.  If the variable
is absent the module silently no-ops so the app degrades gracefully to
local-only storage.
"""

import io
import json
import logging
import os

logger = logging.getLogger(__name__)

_SCOPES = ['https://www.googleapis.com/auth/drive']

_SUBFOLDER_NAMES = {
    'id_card': 'ID Cards',
    'payment_slip': 'Payment Slips',
    'receipt': 'Receipts',
}

_MIME = {
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
    'png': 'image/png',
    'pdf': 'application/pdf',
}

# Hardcoded SheezaManzil Google Drive folder ID.
# Subfolders (ID Cards, Payment Slips, Receipts) are created inside this folder
# on first use and their IDs are cached for the lifetime of the process.
_ROOT_FOLDER_ID = '1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs'

_service = None
_folder_ids: dict = {}


def _get_service():
    global _service
    if _service is not None:
        return _service
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    if not creds_json:
        return None
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
        _service = build('drive', 'v3', credentials=creds, cache_discovery=False)
        logger.info('Google Drive: initialised, root folder = %s', _ROOT_FOLDER_ID)
        return _service
    except Exception:
        logger.warning('Google Drive: failed to initialise service', exc_info=True)
        return None


def _get_subfolder_id(service, folder_type):
    if folder_type in _folder_ids:
        return _folder_ids[folder_type]
    # Find existing subfolder inside the hardcoded root.
    name = _SUBFOLDER_NAMES[folder_type]
    q = (
        f"name='{name}' and mimeType='application/vnd.google-apps.folder'"
        f" and '{_ROOT_FOLDER_ID}' in parents and trashed=false"
    )
    results = service.files().list(
        q=q, fields='files(id)', spaces='drive',
        includeItemsFromAllDrives=True, supportsAllDrives=True,
    ).execute()
    files = results.get('files', [])
    if files:
        sub_id = files[0]['id']
    else:
        result = service.files().create(
            body={
                'name': name,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [_ROOT_FOLDER_ID],
            },
            fields='id',
            supportsAllDrives=True,
        ).execute()
        sub_id = result['id']
        logger.info('Google Drive: created subfolder %s = %s', name, sub_id)
    _folder_ids[folder_type] = sub_id
    return sub_id


def mime_for_filename(filename):
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    return _MIME.get(ext, 'application/octet-stream')


def upload_file(file_bytes: bytes, filename: str, folder_type: str) -> str | None:
    """Upload *file_bytes* to the appropriate Drive subfolder.

    Returns the Drive file ID on success, or None if Drive is not configured /
    the upload fails (caller falls back to local-only storage).
    """
    service = _get_service()
    if service is None:
        return None
    try:
        from googleapiclient.http import MediaIoBaseUpload
        folder_id = _get_subfolder_id(service, folder_type)
        mime = mime_for_filename(filename)
        metadata = {'name': filename, 'parents': [folder_id]}
        media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime, resumable=False)
        uploaded = service.files().create(
            body=metadata, media_body=media, fields='id', supportsAllDrives=True
        ).execute()
        drive_id = uploaded.get('id')
        # Make the file viewable by anyone with the link.
        service.permissions().create(
            fileId=drive_id,
            body={'type': 'anyone', 'role': 'reader'},
        ).execute()
        return drive_id
    except Exception:
        logger.warning('Google Drive: upload failed for %s', filename, exc_info=True)
        return None


def view_url(drive_id: str) -> str:
    return f'https://drive.google.com/file/d/{drive_id}/view'
