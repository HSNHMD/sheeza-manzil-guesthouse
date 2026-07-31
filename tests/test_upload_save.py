"""Tests for `public._save_file` upload hardening (Pepper Phase 0).

`_save_file` is the single choke point every uploaded file passes through
(payment slips today; the Pepper bot's photo path later). Before this it was
unguarded: `filename.rsplit('.',1)[1]` raised IndexError on an extensionless
name, and any file type / size was written to disk. These tests lock in the
allowlist, size cap, extensionless-safety, and an unchanged happy path.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                        # noqa: E402
from app import create_app                                       # noqa: E402
from app.routes import public                                    # noqa: E402
from app.routes.public import _save_file, UploadRejected         # noqa: E402


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


class _FakeUpload:
    """Minimal stand-in for a Werkzeug FileStorage: `.filename` + `.read()`."""

    def __init__(self, filename, data=b'\x89PNG\r\n\x1a\n'):
        self.filename = filename
        self._data = data

    def read(self):
        return self._data


class SaveFileHardeningTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        # Keep tests offline + deterministic: R2 dual-write returns no drive_id.
        self._patcher = mock.patch('app.services.drive.upload_file',
                                   return_value=None)
        self._patcher.start()
        self._written = []

    def tearDown(self):
        # Remove any file the happy-path test wrote into app/uploads.
        upload_dir = os.path.join(self.app.root_path, 'uploads')
        for name in self._written:
            p = os.path.join(upload_dir, name)
            if os.path.exists(p):
                os.remove(p)
        self._patcher.stop()
        self.ctx.pop()

    # --- happy path unchanged ---
    def test_valid_png_saved_and_returned(self):
        name, drive_id = _save_file(_FakeUpload('slip.png'), 'holdslip',
                                    'payment_slip')
        self._written.append(name)
        self.assertTrue(name.startswith('holdslip_'))
        self.assertTrue(name.endswith('.png'))
        self.assertIsNone(drive_id)  # R2 unconfigured -> local fallback
        self.assertTrue(os.path.exists(
            os.path.join(self.app.root_path, 'uploads', name)))

    def test_valid_pdf_and_uppercase_ext_allowed(self):
        name, _ = _save_file(_FakeUpload('receipt.PDF', b'%PDF-1.4'),
                             'holdslip', 'payment_slip')
        self._written.append(name)
        self.assertTrue(name.endswith('.pdf'))  # normalized lower-case

    # --- rejections ---
    def test_disallowed_extension_rejected(self):
        with self.assertRaises(UploadRejected):
            _save_file(_FakeUpload('malware.exe', b'MZ'), 'holdslip',
                       'payment_slip')

    def test_extensionless_filename_rejected_no_indexerror(self):
        # The exact old-code IndexError case.
        with self.assertRaises(UploadRejected):
            _save_file(_FakeUpload('slip', b'data'), 'holdslip', 'payment_slip')

    def test_empty_filename_rejected(self):
        with self.assertRaises(UploadRejected):
            _save_file(_FakeUpload('', b'data'), 'holdslip', 'payment_slip')

    def test_oversized_file_rejected(self):
        big = b'x' * (public.MAX_UPLOAD_BYTES + 1)
        with self.assertRaises(UploadRejected):
            _save_file(_FakeUpload('slip.png', big), 'holdslip', 'payment_slip')

    def test_empty_file_body_rejected(self):
        with self.assertRaises(UploadRejected):
            _save_file(_FakeUpload('slip.png', b''), 'holdslip', 'payment_slip')

    def test_rejection_writes_nothing(self):
        before = set(os.listdir(
            os.path.join(self.app.root_path, 'uploads'))) \
            if os.path.isdir(os.path.join(self.app.root_path, 'uploads')) else set()
        with self.assertRaises(UploadRejected):
            _save_file(_FakeUpload('x.exe', b'MZ'), 'holdslip', 'payment_slip')
        after = set(os.listdir(
            os.path.join(self.app.root_path, 'uploads'))) \
            if os.path.isdir(os.path.join(self.app.root_path, 'uploads')) else set()
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
