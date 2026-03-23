"""Upload files to Google Drive using a service account."""
import json
import os

CREDS_ENV = 'GOOGLE_CREDENTIALS'
ID_FOLDER = 'Sheeza Manzil Guest IDs'
PAYMENT_FOLDER = 'Sheeza Manzil Payment Slips'


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


def _get_or_create_folder(svc, folder_name):
    res = svc.files().list(
        q=f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields='files(id)'
    ).execute()
    files = res.get('files', [])
    if files:
        return files[0]['id']
    meta = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder'}
    folder = svc.files().create(body=meta, fields='id').execute()
    return folder['id']


def _upload_to_folder(file_path: str, filename: str, folder_name: str):
    """Upload a file to a named Drive folder. Returns (file_id, web_view_link) or (None, None)."""
    svc = _service()
    if not svc:
        return None, None
    from googleapiclient.http import MediaFileUpload
    ext = filename.rsplit('.', 1)[-1].lower()
    mime = {'pdf': 'application/pdf', 'png': 'image/png',
            'jpg': 'image/jpeg', 'jpeg': 'image/jpeg'}.get(ext, 'application/octet-stream')
    folder_id = _get_or_create_folder(svc, folder_name)
    meta = {'name': filename, 'parents': [folder_id]}
    media = MediaFileUpload(file_path, mimetype=mime)
    f = svc.files().create(body=meta, media_body=media, fields='id,webViewLink').execute()
    return f.get('id'), f.get('webViewLink')


def upload_id_card(file_path: str, filename: str):
    """Upload an ID card to 'Sheeza Manzil Guest IDs'. Returns (file_id, web_view_link) or (None, None)."""
    return _upload_to_folder(file_path, filename, ID_FOLDER)


def upload_payment_slip(file_path: str, filename: str):
    """Upload a payment slip to 'Sheeza Manzil Payment Slips'. Returns (file_id, web_view_link) or (None, None)."""
    return _upload_to_folder(file_path, filename, PAYMENT_FOLDER)
