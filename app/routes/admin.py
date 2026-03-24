import io
import os
import tempfile
import traceback

from flask import Blueprint, jsonify
from flask_login import login_required

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')


@admin_bp.route('/test-gdrive')
@login_required
def test_gdrive():
    result = {}

    # ── Step 1: Check all required env vars ───────────────────────────────────
    env_vars = {
        'GOOGLE_OAUTH_CLIENT_ID':       os.environ.get('GOOGLE_OAUTH_CLIENT_ID', '').strip(),
        'GOOGLE_OAUTH_CLIENT_SECRET':   os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET', '').strip(),
        'GOOGLE_OAUTH_REFRESH_TOKEN':   os.environ.get('GOOGLE_OAUTH_REFRESH_TOKEN', '').strip(),
        'GOOGLE_DRIVE_ID_FOLDER_ID':    os.environ.get('GOOGLE_DRIVE_ID_FOLDER_ID', '').strip(),
        'GOOGLE_DRIVE_PAYMENT_FOLDER_ID': os.environ.get('GOOGLE_DRIVE_PAYMENT_FOLDER_ID', '').strip(),
    }
    result['env_vars'] = {k: ('SET' if v else 'MISSING') for k, v in env_vars.items()}

    missing = [k for k, v in env_vars.items() if not v]
    if missing:
        result['error'] = f'Missing required env vars: {", ".join(missing)}'
        return jsonify(result), 200

    # ── Step 2: Build Drive service (OAuth token refresh) ────────────────────
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = Credentials(
            token=None,
            refresh_token=env_vars['GOOGLE_OAUTH_REFRESH_TOKEN'],
            client_id=env_vars['GOOGLE_OAUTH_CLIENT_ID'],
            client_secret=env_vars['GOOGLE_OAUTH_CLIENT_SECRET'],
            token_uri='https://oauth2.googleapis.com/token',
            scopes=['https://www.googleapis.com/auth/drive'],
        )
        creds.refresh(Request())
        svc = build('drive', 'v3', credentials=creds)
        result['drive_connection'] = 'OK'
        result['token_valid'] = creds.valid
    except ImportError as exc:
        result['drive_connection'] = 'FAILED'
        result['error'] = f'Import error: {exc}'
        return jsonify(result), 200
    except Exception as exc:
        result['drive_connection'] = 'FAILED'
        result['drive_error'] = str(exc)
        result['drive_traceback'] = traceback.format_exc()
        return jsonify(result), 200

    # ── Step 3: Verify both target folders exist and are accessible ───────────
    for label, folder_id in [
        ('id_folder', env_vars['GOOGLE_DRIVE_ID_FOLDER_ID']),
        ('payment_folder', env_vars['GOOGLE_DRIVE_PAYMENT_FOLDER_ID']),
    ]:
        try:
            f = svc.files().get(fileId=folder_id, fields='id,name,mimeType').execute()
            result[label] = {'status': 'FOUND', 'id': f['id'], 'name': f['name']}
        except Exception as exc:
            result[label] = {'status': 'ERROR', 'error': str(exc)}

    # ── Step 4: Test actual file upload into ID folder ────────────────────────
    tmp_path = None
    uploaded_file_id = None
    try:
        from googleapiclient.http import MediaIoBaseUpload

        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as tmp:
            tmp.write('Drive connectivity test — safe to delete.')
            tmp_path = tmp.name

        with open(tmp_path, 'rb') as fh:
            file_bytes = fh.read()

        media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype='text/plain', resumable=False)
        f = svc.files().create(
            body={'name': 'TEST-connectivity-check.txt',
                  'parents': [env_vars['GOOGLE_DRIVE_ID_FOLDER_ID']]},
            media_body=media,
            fields='id,webViewLink',
        ).execute()
        uploaded_file_id = f.get('id')
        result['test_upload'] = 'OK'
        result['test_web_view_link'] = f.get('webViewLink')

        # Test public permission
        try:
            svc.permissions().create(
                fileId=uploaded_file_id,
                body={'role': 'reader', 'type': 'anyone'},
                fields='id',
            ).execute()
            result['test_permission'] = 'OK — link is publicly viewable'
        except Exception as exc:
            result['test_permission'] = f'FAILED: {exc}'

    except Exception as exc:
        result['test_upload'] = 'FAILED'
        result['test_upload_error'] = str(exc)
        result['test_upload_traceback'] = traceback.format_exc()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        if uploaded_file_id:
            try:
                svc.files().delete(fileId=uploaded_file_id).execute()
                result['test_cleanup'] = 'Test file deleted from Drive'
            except Exception:
                result['test_cleanup'] = f'Note: test file {uploaded_file_id} left in Drive'

    return jsonify(result), 200
