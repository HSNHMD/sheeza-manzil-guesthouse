"""Cloudflare R2 upload service.

Uploads booking documents (ID cards, payment slips) and expense receipts
to a private Cloudflare R2 bucket, organised into subfolders by type:

    <bucket>/
        id-cards/
        payment-slips/
        receipts/

File links are pre-signed URLs that expire after 1 hour, so only staff
logged into the app can generate them — files are never publicly accessible.

Requires these environment variables:
    CLOUDFLARE_ACCOUNT_ID   — Cloudflare account ID
    R2_ACCESS_KEY_ID        — R2 API token access key ID
    R2_SECRET_ACCESS_KEY    — R2 API token secret access key
    R2_BUCKET_NAME          — R2 bucket name

If any variable is absent the module silently no-ops so the app degrades
gracefully to local-only storage.
"""

import logging
import os

logger = logging.getLogger(__name__)

_SUBFOLDER = {
    'id_card': 'id-cards',
    'payment_slip': 'payment-slips',
    'receipt': 'receipts',
}

_MIME = {
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
    'png': 'image/png',
    'pdf': 'application/pdf',
}

_PRESIGN_EXPIRY = 3600  # seconds (1 hour)

_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    account_id = os.environ.get('CLOUDFLARE_ACCOUNT_ID')
    access_key = os.environ.get('R2_ACCESS_KEY_ID')
    secret_key = os.environ.get('R2_SECRET_ACCESS_KEY')
    if not all([account_id, access_key, secret_key]):
        return None
    try:
        import boto3
        _client = boto3.client(
            's3',
            endpoint_url=f'https://{account_id}.r2.cloudflarestorage.com',
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name='auto',
        )
        logger.info('R2: client initialised for account %s', account_id)
        return _client
    except Exception:
        logger.warning('R2: failed to initialise client', exc_info=True)
        return None


def mime_for_filename(filename):
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    return _MIME.get(ext, 'application/octet-stream')


def upload_file(file_bytes: bytes, filename: str, folder_type: str) -> str | None:
    """Upload *file_bytes* to the appropriate R2 subfolder.

    Returns the object key on success, or None if R2 is not configured /
    the upload fails (caller falls back to local-only storage).
    """
    client = _get_client()
    if client is None:
        return None
    bucket = os.environ.get('R2_BUCKET_NAME')
    if not bucket:
        logger.warning('R2: R2_BUCKET_NAME not set')
        return None
    subfolder = _SUBFOLDER.get(folder_type, folder_type)
    key = f'{subfolder}/{filename}'
    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=file_bytes,
            ContentType=mime_for_filename(filename),
        )
        logger.info('R2: uploaded %s', key)
        return key
    except Exception:
        logger.warning('R2: upload failed for %s', filename, exc_info=True)
        return None


def view_url(key: str) -> str:
    """Return a pre-signed URL for *key* that expires in 1 hour."""
    client = _get_client()
    bucket = os.environ.get('R2_BUCKET_NAME', '')
    if not client or not bucket:
        return ''
    try:
        url = client.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket, 'Key': key},
            ExpiresIn=_PRESIGN_EXPIRY,
        )
        return url
    except Exception:
        logger.warning('R2: failed to generate pre-signed URL for %s', key, exc_info=True)
        return ''
