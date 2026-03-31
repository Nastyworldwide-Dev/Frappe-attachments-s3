"""Tests for S3 upload controller — verifies ACL handling."""

import unittest
from unittest.mock import patch, MagicMock


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
