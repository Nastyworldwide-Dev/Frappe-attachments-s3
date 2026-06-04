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


if __name__ == "__main__":
    unittest.main()
