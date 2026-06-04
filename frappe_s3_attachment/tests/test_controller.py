"""Tests for S3 upload controller — verifies ACL handling."""

import unittest
from unittest.mock import patch, MagicMock


class TestFileUploadToS3SkipsExisting(unittest.TestCase):
    """Verify file_upload_to_s3 does not re-upload files already on S3.

    Amending a document copies its attachments into new File records whose
    file_url already points to S3 (generate_file URL). Re-uploading those
    treats the URL as a local path and crashes with FileNotFoundError.
    """

    def _run_hook(self, file_url):
        mock_frappe = MagicMock()
        mock_frappe.local.conf.get.return_value = None

        captured = {"s3_constructed": False}

        with (
            patch("frappe_s3_attachment.controller.frappe", mock_frappe),
            patch("frappe_s3_attachment.controller.S3Operations") as mock_ops,
        ):

            def _mark(*a, **k):
                captured["s3_constructed"] = True
                return MagicMock()

            mock_ops.side_effect = _mark

            from frappe_s3_attachment.controller import file_upload_to_s3

            doc = MagicMock()
            doc.file_url = file_url
            doc.is_private = 1
            doc.attached_to_doctype = "Journal Entry"
            doc.attached_to_name = "JE-001"

            file_upload_to_s3(doc, "after_insert")

        return captured["s3_constructed"]

    def test_skips_already_uploaded_private_file(self):
        """A copied attachment already on S3 must not be re-uploaded."""
        url = (
            "/api/method/frappe_s3_attachment.controller.generate_file"
            "?key=2026/02/12/Journal Entry/ABC_Report.pdf&file_name=Report.pdf"
        )
        self.assertFalse(self._run_hook(url))

    def test_skips_already_uploaded_public_file(self):
        """A public file already on S3 (https URL) must not be re-uploaded."""
        url = "https://test-bucket.s3.amazonaws.com/2026/02/12/x_Report.pdf"
        self.assertFalse(self._run_hook(url))


class TestS3UploadACL(unittest.TestCase):
    """Verify ACL parameter behavior in upload_files_to_s3_with_key."""

    def _run_upload(self, is_private, s3_use_acl=False):
        """Create mocked S3Operations and run an upload, returning ExtraArgs."""
        mock_settings = MagicMock()
        mock_settings.aws_key = "test-key"
        mock_settings.aws_secret = "test-secret"
        mock_settings.region_name = "us-east-1"
        mock_settings.bucket_name = "test-bucket"
        mock_settings.folder_name = "test-folder"

        mock_frappe = MagicMock()
        mock_frappe.get_doc.return_value = mock_settings
        mock_frappe.local.conf.get.side_effect = lambda k: (
            s3_use_acl if k == "s3_use_acl" else None
        )

        mock_magic = MagicMock()
        mock_magic.from_file.return_value = "application/pdf"

        mock_client = MagicMock()
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client

        with patch.dict("sys.modules", {}):
            with (
                patch("frappe_s3_attachment.controller.frappe", mock_frappe),
                patch("frappe_s3_attachment.controller.boto3", mock_boto3),
                patch("frappe_s3_attachment.controller.magic", mock_magic),
            ):
                from frappe_s3_attachment.controller import S3Operations

                ops = S3Operations()
                ops.S3_CLIENT = mock_client
                ops.key_generator = MagicMock(
                    return_value="test-folder/test-key_test.pdf"
                )
                ops.upload_files_to_s3_with_key(
                    "/tmp/test.pdf",
                    "test.pdf",
                    is_private=is_private,
                    parent_doctype="Sales Invoice",
                    parent_name="SI-001",
                )

        return mock_client.upload_file.call_args[1]["ExtraArgs"]

    def test_public_upload_no_acl_by_default(self):
        """Public upload must NOT include ACL when s3_use_acl is not set."""
        extra_args = self._run_upload(is_private=False)
        self.assertNotIn("ACL", extra_args)

    def test_private_upload_no_acl_by_default(self):
        """Private upload must NOT include ACL when s3_use_acl is not set."""
        extra_args = self._run_upload(is_private=True)
        self.assertNotIn("ACL", extra_args)

    def test_public_upload_includes_acl_when_configured(self):
        """Public upload must include ACL when s3_use_acl is enabled."""
        extra_args = self._run_upload(is_private=False, s3_use_acl=True)
        self.assertEqual(extra_args.get("ACL"), "public-read")

    def test_private_upload_no_acl_even_when_configured(self):
        """Private upload must NOT include ACL even when s3_use_acl is enabled."""
        extra_args = self._run_upload(is_private=True, s3_use_acl=True)
        self.assertNotIn("ACL", extra_args)


class TestFileUploadToS3Guards(unittest.TestCase):
    """Verify file_upload_to_s3 skips records that have no local file to upload.

    Folders, File records without a file_url, and files whose local copy is
    missing must not reach magic.from_file / S3 upload (which would raise
    TypeError or FileNotFoundError).
    """

    def _run_hook(
        self,
        *,
        file_url,
        is_folder=0,
        is_private=1,
        local_exists=True,
        other_local_refs=0,
        attached_to_doctype="Journal Entry",
    ):
        mock_frappe = MagicMock()
        mock_frappe.local.conf.get.return_value = None
        mock_frappe.local._s3_operations = None  # force get_s3_client to build one
        mock_frappe.utils.get_site_path.return_value = "/site"
        mock_frappe.db.count.return_value = other_local_refs

        with (
            patch("frappe_s3_attachment.controller.frappe", mock_frappe),
            patch("frappe_s3_attachment.controller.S3Operations") as mock_ops,
            patch("frappe_s3_attachment.controller.os") as mock_os,
        ):
            mock_os.path.exists.return_value = local_exists
            mock_ops.return_value.upload_files_to_s3_with_key.return_value = "k"

            from frappe_s3_attachment.controller import file_upload_to_s3

            doc = MagicMock()
            doc.file_url = file_url
            doc.is_folder = is_folder
            doc.is_private = is_private
            doc.attached_to_doctype = attached_to_doctype
            doc.attached_to_name = "X-1"
            doc.file_name = "f.pdf"

            file_upload_to_s3(doc, "after_insert")

            return {
                "uploads": mock_ops.return_value.upload_files_to_s3_with_key.call_count,
                "removes": mock_os.remove.call_count,
            }

    def test_skips_folder(self):
        """Folder records (is_folder=1, no file_url) must be skipped."""
        self.assertEqual(self._run_hook(file_url=None, is_folder=1)["uploads"], 0)

    def test_skips_missing_file_url(self):
        """File records without a file_url must be skipped (no TypeError)."""
        self.assertEqual(self._run_hook(file_url=None)["uploads"], 0)

    def test_skips_when_local_file_missing(self):
        """A file whose local copy is absent must be skipped (no FileNotFound)."""
        self.assertEqual(
            self._run_hook(file_url="/private/files/f.pdf", local_exists=False)[
                "uploads"
            ],
            0,
        )

    def test_uploads_when_local_file_present(self):
        """A genuine local file must still be uploaded to S3."""
        self.assertEqual(
            self._run_hook(file_url="/private/files/f.pdf", local_exists=True)[
                "uploads"
            ],
            1,
        )

    def test_removes_local_file_when_last_reference(self):
        """The local file is removed after upload when nothing else references it."""
        r = self._run_hook(file_url="/private/files/f.pdf", other_local_refs=0)
        self.assertEqual(r["uploads"], 1)
        self.assertEqual(r["removes"], 1)

    def test_keeps_shared_local_file(self):
        """The local file must NOT be removed if other File records share it (M3)."""
        r = self._run_hook(file_url="/private/files/f.pdf", other_local_refs=2)
        self.assertEqual(r["uploads"], 1)
        self.assertEqual(r["removes"], 0)


class TestDeleteFromCloud(unittest.TestCase):
    """Verify delete_from_cloud only deletes files actually stored on S3."""

    def _run(self, *, file_url, content_hash, shared_count=0):
        mock_frappe = MagicMock()
        mock_frappe.local._s3_operations = None  # force get_s3_client to build one
        mock_frappe.db.count.return_value = shared_count
        captured = {"n": 0, "deleted_key": None}

        with (
            patch("frappe_s3_attachment.controller.frappe", mock_frappe),
            patch("frappe_s3_attachment.controller.S3Operations") as mock_ops,
        ):
            inst = MagicMock()

            def _del(key):
                captured["deleted_key"] = key

            inst.delete_from_s3.side_effect = _del

            def _mark(*a, **k):
                captured["n"] += 1
                return inst

            mock_ops.side_effect = _mark

            from frappe_s3_attachment.controller import delete_from_cloud

            doc = MagicMock()
            doc.file_url = file_url
            doc.content_hash = content_hash

            delete_from_cloud(doc, "on_trash")

        return captured

    def test_skips_local_file(self):
        """A local (non-S3) file must not trigger an S3 delete."""
        r = self._run(file_url="/private/files/f.pdf", content_hash="abc123localhash")
        self.assertEqual(r["n"], 0)

    def test_skips_when_no_content_hash(self):
        """A file without a content_hash (S3 key) must not trigger a delete."""
        url = (
            "/api/method/frappe_s3_attachment.controller.generate_file?key=2026/x_f.pdf"
        )
        r = self._run(file_url=url, content_hash=None)
        self.assertEqual(r["n"], 0)

    def test_deletes_s3_file(self):
        """A genuine S3 file must be deleted using its S3 key."""
        url = (
            "/api/method/frappe_s3_attachment.controller.generate_file?key=2026/x_f.pdf"
        )
        r = self._run(file_url=url, content_hash="2026/x_f.pdf")
        self.assertEqual(r["n"], 1)
        self.assertEqual(r["deleted_key"], "2026/x_f.pdf")

    def test_skips_when_key_shared(self):
        """An S3 object shared by other File records must NOT be deleted (H1)."""
        url = (
            "/api/method/frappe_s3_attachment.controller.generate_file?key=2026/x_f.pdf"
        )
        r = self._run(file_url=url, content_hash="2026/x_f.pdf", shared_count=2)
        self.assertEqual(r["n"], 0)


class TestGenerateFileAuth(unittest.TestCase):
    """Verify generate_file enforces document permission before signing (H2)."""

    def _run(self, *, files, has_perm=True):
        import types as _types

        mock_frappe = MagicMock()
        mock_frappe.local._s3_operations = None
        mock_frappe.PermissionError = type("PermissionError", (Exception,), {})
        mock_frappe._ = lambda s: s
        mock_frappe.get_all.return_value = [_types.SimpleNamespace(**f) for f in files]
        mock_frappe.has_permission.return_value = has_perm

        with (
            patch("frappe_s3_attachment.controller.frappe", mock_frappe),
            patch("frappe_s3_attachment.controller.S3Operations") as mock_ops,
        ):
            mock_ops.return_value.get_url.return_value = "https://signed"
            from frappe_s3_attachment.controller import generate_file

            try:
                generate_file(key="2026/x_f.pdf", file_name="f.pdf")
                denied = False
            except mock_frappe.PermissionError:
                denied = True

            return denied, mock_ops.return_value.get_url.call_count

    def test_no_file_for_key_denied(self):
        """A key owned by no File record must be denied (no signing)."""
        denied, signed = self._run(files=[])
        self.assertTrue(denied)
        self.assertEqual(signed, 0)

    def test_private_without_permission_denied(self):
        """A private file the user cannot access must be denied."""
        f = {
            "name": "F1",
            "is_private": 1,
            "attached_to_doctype": "Journal Entry",
            "attached_to_name": "JE-1",
        }
        denied, signed = self._run(files=[f], has_perm=False)
        self.assertTrue(denied)
        self.assertEqual(signed, 0)

    def test_private_with_permission_signs(self):
        """A private file the user can access must be signed."""
        f = {
            "name": "F1",
            "is_private": 1,
            "attached_to_doctype": "Journal Entry",
            "attached_to_name": "JE-1",
        }
        denied, signed = self._run(files=[f], has_perm=True)
        self.assertFalse(denied)
        self.assertEqual(signed, 1)

    def test_public_file_signs_without_permission(self):
        """A public file is signed regardless of document permission."""
        f = {
            "name": "F1",
            "is_private": 0,
            "attached_to_doctype": None,
            "attached_to_name": None,
        }
        denied, signed = self._run(files=[f], has_perm=False)
        self.assertFalse(denied)
        self.assertEqual(signed, 1)


class TestUploadErrorHandling(unittest.TestCase):
    """Verify S3 client errors during upload are surfaced via frappe.throw (M2)."""

    def test_client_error_is_caught(self):
        import boto3 as real_boto3
        from botocore.exceptions import ClientError

        mock_settings = MagicMock()
        mock_settings.aws_key = "k"
        mock_settings.aws_secret = "s"
        mock_settings.region_name = "us-east-1"
        mock_settings.bucket_name = "b"
        mock_settings.folder_name = "f"

        mock_frappe = MagicMock()
        mock_frappe.get_doc.return_value = mock_settings
        mock_frappe.local.conf.get.return_value = None
        mock_frappe._ = lambda s: s

        mock_magic = MagicMock()
        mock_magic.from_file.return_value = "application/pdf"

        mock_client = MagicMock()
        mock_client.upload_file.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "PutObject"
        )
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_boto3.exceptions = real_boto3.exceptions  # keep real exception classes

        with (
            patch("frappe_s3_attachment.controller.frappe", mock_frappe),
            patch("frappe_s3_attachment.controller.boto3", mock_boto3),
            patch("frappe_s3_attachment.controller.magic", mock_magic),
        ):
            from frappe_s3_attachment.controller import S3Operations

            ops = S3Operations()
            ops.S3_CLIENT = mock_client
            ops.key_generator = MagicMock(return_value="b/key_test.pdf")
            ops.upload_files_to_s3_with_key(
                "/tmp/x.pdf", "x.pdf", True, "DocType", "n1"
            )

        self.assertTrue(mock_frappe.throw.called)


class TestIsS3File(unittest.TestCase):
    """Verify CustomFile.is_s3_file distinguishes S3 files from external links (L3)."""

    def _is_s3(self, file_url, content_hash):
        import types as _types

        from frappe_s3_attachment.overrides.file import CustomFile

        obj = _types.SimpleNamespace(file_url=file_url, content_hash=content_hash)
        return CustomFile.is_s3_file(obj)

    def test_generate_file_url_is_s3(self):
        url = "/api/method/frappe_s3_attachment.controller.generate_file?key=k"
        self.assertTrue(self._is_s3(url, "k"))

    def test_public_s3_https_with_key_is_s3(self):
        self.assertTrue(
            self._is_s3("https://b.s3.amazonaws.com/2026/x.pdf", "2026/x.pdf")
        )

    def test_external_https_without_key_is_not_s3(self):
        self.assertFalse(self._is_s3("https://example.com/foo.pdf", None))

    def test_local_file_is_not_s3(self):
        self.assertFalse(self._is_s3("/private/files/foo.pdf", "localhash"))


if __name__ == "__main__":
    unittest.main()
