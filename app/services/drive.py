"""Upload files to Google Drive using a service account."""
import io
import json
import os

FOLDER_ID_ENV = 'GOOGLE_DRIVE_FOLDER_ID'
CREDS_ENV = 'GOOGLE_SERVICE_ACCOUNT_JSON'
FOLDER_NAME = 'Sheeza Manzil Guest IDs'


def _service():
    creds_json = os.environ.get(CREDS_ENV)
    if not creds_json:
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=['https://www.googleapis.com/auth/drive.file'])
        return build('drive', 'v3', credentials=creds)
    except Exception:
        return None


def _get_or_create_folder(svc):
    folder_id = os.environ.get(FOLDER_ID_ENV)
    if folder_id:
        return folder_id
    # Search for folder by name
    res = svc.files().list(
        q=f"name='{FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields='files(id)'
    ).execute()
    files = res.get('files', [])
    if files:
        return files[0]['id']
    # Create it
    meta = {'name': FOLDER_NAME, 'mimeType': 'application/vnd.google-apps.folder'}
    folder = svc.files().create(body=meta, fields='id').execute()
    return folder['id']


def upload_id_card(file_path: str, filename: str):
    """Upload file to Drive. Returns (file_id, web_view_link) or (None, None)."""
    svc = _service()
    if not svc:
        return None, None
    try:
        from googleapiclient.http import MediaFileUpload
        ext = filename.rsplit('.', 1)[-1].lower()
        mime = {'pdf': 'application/pdf', 'png': 'image/png',
                'jpg': 'image/jpeg', 'jpeg': 'image/jpeg'}.get(ext, 'application/octet-stream')
        folder_id = _get_or_create_folder(svc)
        meta = {'name': filename, 'parents': [folder_id]}
        media = MediaFileUpload(file_path, mimetype=mime)
        f = svc.files().create(body=meta, media_body=media, fields='id,webViewLink').execute()
        return f.get('id'), f.get('webViewLink')
    except Exception as e:
        print(f'[Drive] upload error: {e}')
        return None, None
