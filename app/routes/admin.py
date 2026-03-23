import json
import os
import traceback

from flask import Blueprint, jsonify
from flask_login import login_required

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

CREDS_ENV = 'GOOGLE_CREDENTIALS'
ID_FOLDER = 'Sheeza Manzil Guest IDs'
PAYMENT_FOLDER = 'Sheeza Manzil Payment Slips'


@admin_bp.route('/test-gdrive')
@login_required
def test_gdrive():
    result = {}

    # Step 1: Check env var presence and JSON parsing
    creds_json = os.environ.get(CREDS_ENV)
    result['env_var_set'] = bool(creds_json)
    result['env_var_length'] = len(creds_json) if creds_json else 0

    if not creds_json:
        result['error'] = f'{CREDS_ENV} environment variable is not set'
        return jsonify(result), 200

    try:
        info = json.loads(creds_json)
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

    # Step 2: Build Drive service
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=['https://www.googleapis.com/auth/drive.readonly']
        )
        svc = build('drive', 'v3', credentials=creds)
        result['drive_connection'] = 'OK'
    except ImportError as exc:
        result['drive_connection'] = 'FAILED'
        result['drive_error'] = f'Import error: {exc}'
        return jsonify(result), 200
    except Exception as exc:
        result['drive_connection'] = 'FAILED'
        result['drive_error'] = str(exc)
        result['drive_traceback'] = traceback.format_exc()
        return jsonify(result), 200

    # Step 3: List files in root to confirm connection works
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

    # Step 4: Find both target folders
    for folder_name, key in [(ID_FOLDER, 'id_folder'), (PAYMENT_FOLDER, 'payment_folder')]:
        try:
            resp = svc.files().list(
                q=f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
                fields='files(id, name)'
            ).execute()
            folders = resp.get('files', [])
            if folders:
                result[key] = {'status': 'FOUND', 'id': folders[0]['id']}
            else:
                result[key] = {'status': 'NOT FOUND', 'note': 'Folder does not exist yet — will be created on first upload'}
        except Exception as exc:
            result[key] = {'status': 'ERROR', 'error': str(exc), 'traceback': traceback.format_exc()}

    return jsonify(result), 200
