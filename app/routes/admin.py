import io
import json
import os
import tempfile
import traceback

from flask import Blueprint, jsonify
from flask_login import login_required

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

CREDS_ENV = 'GOOGLE_CREDENTIALS'
ID_FOLDER = 'Sheeza Manzil Guest IDs'
PAYMENT_FOLDER = 'Sheeza Manzil Payment Slips'
ID_FOLDER_ID_ENV = 'GOOGLE_DRIVE_ID_FOLDER_ID'
PAYMENT_FOLDER_ID_ENV = 'GOOGLE_DRIVE_PAYMENT_FOLDER_ID'


@admin_bp.route('/test-gdrive')
@login_required
def test_gdrive():
    result = {}

    # ── Step 1: env var + JSON parse ──────────────────────────────────────────
    creds_json = os.environ.get(CREDS_ENV)
    result['env_var_set'] = bool(creds_json)
    result['env_var_length'] = len(creds_json) if creds_json else 0

    if not creds_json:
        result['error'] = f'{CREDS_ENV} environment variable is not set'
        return jsonify(result), 200

    try:
        info = json.loads(creds_json.strip())
        result['json_parse'] = 'OK'
        result['credential_type'] = info.get('type')
        result['project_id'] = info.get('project_id')
        result['client_email'] = info.get('client_email')
        result['private_key_present'] = bool(info.get('private_key'))
        result['private_key_starts_with'] = (info.get('private_key', '')[:30] + '...') if info.get('private_key') else None
    except json.JSONDecodeError as exc:
        result['json_parse'] = 'FAILED'
        result['json_error'] = str(exc)
        return jsonify(result), 200

    # ── Step 2: Build Drive service with WRITE scope (same as production) ─────
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=['https://www.googleapis.com/auth/drive']
        )
        svc = build('drive', 'v3', credentials=creds)
        result['drive_connection'] = 'OK'
        result['drive_scope'] = 'drive (read+write — same as production)'
    except ImportError as exc:
        result['drive_connection'] = 'FAILED'
        result['drive_error'] = f'Import error: {exc}'
        return jsonify(result), 200
    except Exception as exc:
        result['drive_connection'] = 'FAILED'
        result['drive_error'] = str(exc)
        result['drive_traceback'] = traceback.format_exc()
        return jsonify(result), 200

    # ── Step 3: List root files ────────────────────────────────────────────────
    try:
        resp = svc.files().list(
            q="'root' in parents and trashed=false",
            pageSize=10,
            fields='files(id, name, mimeType)'
        ).execute()
        files = resp.get('files', [])
        result['root_listing'] = 'OK'
        result['root_file_count'] = len(files)
        result['root_files'] = [{'name': f['name'], 'type': f['mimeType']} for f in files]
    except Exception as exc:
        result['root_listing'] = 'FAILED'
        result['root_listing_error'] = str(exc)
        result['root_listing_traceback'] = traceback.format_exc()

    # ── Step 4: Find both target folders ──────────────────────────────────────
    folder_ids = {}
    for folder_name, key in [(ID_FOLDER, 'id_folder'), (PAYMENT_FOLDER, 'payment_folder')]:
        try:
            resp = svc.files().list(
                q=f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
                fields='files(id, name)'
            ).execute()
            folders = resp.get('files', [])
            if folders:
                folder_ids[folder_name] = folders[0]['id']
                result[key] = {'status': 'FOUND', 'id': folders[0]['id']}
            else:
                result[key] = {'status': 'NOT FOUND', 'note': 'Will be created on first real upload'}
        except Exception as exc:
            result[key] = {'status': 'ERROR', 'error': str(exc), 'traceback': traceback.format_exc()}

    # ── Step 4b: Check folder ID env vars ─────────────────────────────────────
    id_folder_id_var = os.environ.get(ID_FOLDER_ID_ENV, '').strip()
    payment_folder_id_var = os.environ.get(PAYMENT_FOLDER_ID_ENV, '').strip()
    result['env_id_folder_id'] = id_folder_id_var or 'NOT SET — required! See setup instructions.'
    result['env_payment_folder_id'] = payment_folder_id_var or 'NOT SET — required! See setup instructions.'

    # ── Step 5: Test actual file upload to ID folder ───────────────────────────
    # Prefer the user-owned folder ID (has quota); fall back to discovered folder
    upload_folder_id = id_folder_id_var or folder_ids.get(ID_FOLDER)
    if upload_folder_id:
        tmp_path = None
        uploaded_file_id = None
        try:
            from googleapiclient.http import MediaFileUpload

            # Create a small temp text file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as tmp:
                tmp.write('Drive connectivity test — safe to delete.')
                tmp_path = tmp.name

            meta = {'name': 'TEST-connectivity-check.txt', 'parents': [upload_folder_id]}
            media = MediaFileUpload(tmp_path, mimetype='text/plain')
            f = svc.files().create(body=meta, media_body=media, fields='id,webViewLink').execute()
            uploaded_file_id = f.get('id')
            web_link = f.get('webViewLink')
            result['test_upload'] = 'OK'
            result['test_file_id'] = uploaded_file_id
            result['test_web_view_link'] = web_link

            # Test setting public permission (same as production does after upload)
            try:
                svc.permissions().create(
                    fileId=uploaded_file_id,
                    body={'role': 'reader', 'type': 'anyone'},
                    fields='id'
                ).execute()
                result['test_permission'] = 'OK — file is publicly viewable via link'
            except Exception as exc:
                result['test_permission'] = f'FAILED: {exc}'

        except Exception as exc:
            result['test_upload'] = 'FAILED'
            result['test_upload_error'] = str(exc)
            result['test_upload_traceback'] = traceback.format_exc()
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            # Clean up the test file from Drive
            if uploaded_file_id:
                try:
                    svc.files().delete(fileId=uploaded_file_id).execute()
                    result['test_cleanup'] = 'Test file deleted from Drive'
                except Exception:
                    result['test_cleanup'] = f'Note: test file {uploaded_file_id} left in Drive — delete manually'
    else:
        result['test_upload'] = 'SKIPPED — ID folder not found'

    return jsonify(result), 200
