"""Tests for S3 upload controller — verifies ACL is not passed to S3."""

import unittest
from unittest.mock import patch, MagicMock


class TestS3UploadNoACL(unittest.TestCase):
    """Verify that upload_files_to_s3_with_key does not pass ACL parameter."""

    @patch("frappe_s3_attachment.controller.magic")
    @patch("frappe_s3_attachment.controller.frappe")
    def _make_s3ops(self, mock_frappe, mock_magic):
        """Create an S3Operations instance with mocked dependencies."""
        mock_settings = MagicMock()
        mock_settings.aws_key = "test-key"
        mock_settings.aws_secret = "test-secret"
        mock_settings.region_name = "us-east-1"
        mock_settings.bucket_name = "test-bucket"
        mock_settings.folder_name = "test-folder"
        mock_frappe.get_doc.return_value = mock_settings

        with patch("frappe_s3_attachment.controller.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client

            from frappe_s3_attachment.controller import S3Operations

            ops = S3Operations()
            ops.S3_CLIENT = mock_client
            # Mock key_generator to avoid frappe.get_hooks() call
            ops.key_generator = MagicMock(return_value="test-folder/test-key_test.pdf")
            return ops, mock_client

    def test_public_upload_does_not_include_acl(self):
        """Public file upload must NOT include ACL in ExtraArgs (ACL-disabled buckets)."""
        ops, mock_client = self._make_s3ops()
        mock_client.upload_file = MagicMock()

        with patch("frappe_s3_attachment.controller.magic") as mock_magic:
            mock_magic.from_file.return_value = "application/pdf"
            ops.upload_files_to_s3_with_key(
                "/tmp/test.pdf",
                "test.pdf",
                is_private=False,
                parent_doctype="Sales Invoice",
                parent_name="SI-001",
            )

        call_args = mock_client.upload_file.call_args
        extra_args = call_args[1]["ExtraArgs"]
        self.assertNotIn(
            "ACL", extra_args, "ACL parameter should not be passed to S3 upload"
        )

    def test_private_upload_does_not_include_acl(self):
        """Private file upload must NOT include ACL in ExtraArgs."""
        ops, mock_client = self._make_s3ops()
        mock_client.upload_file = MagicMock()

        with patch("frappe_s3_attachment.controller.magic") as mock_magic:
            mock_magic.from_file.return_value = "application/pdf"
            ops.upload_files_to_s3_with_key(
                "/tmp/test.pdf",
                "test.pdf",
                is_private=True,
                parent_doctype="Sales Invoice",
                parent_name="SI-001",
            )

        call_args = mock_client.upload_file.call_args
        extra_args = call_args[1]["ExtraArgs"]
        self.assertNotIn(
            "ACL", extra_args, "ACL parameter should not be passed to S3 upload"
        )


if __name__ == "__main__":
    unittest.main()
