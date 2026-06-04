import frappe
from frappe.core.doctype.file.file import File

from frappe_s3_attachment.controller import get_s3_client


class CustomFile(File):
    def get_content(self):
        """
        Override get_content to fetch file content from S3 when the file
        is stored on S3 instead of the local filesystem.
        """
        if self.is_s3_file():
            return self._get_content_from_s3()

        return super().get_content()

    def is_s3_file(self):
        """True if file_url points to S3 (generate_file endpoint, or https with an S3 key)."""
        url = self.file_url or ""
        api = "/api/method/frappe_s3_attachment.controller.generate_file"
        return bool(
            url.startswith(api) or (url.startswith("https:") and self.content_hash)
        )

    def _get_content_from_s3(self):
        """Fetch file content from S3 using the content_hash (S3 key)."""
        key = self.content_hash
        if not key:
            frappe.throw(f"S3 key (content_hash) not found for file {self.name}")

        s3 = get_s3_client()
        response = s3.read_file_from_s3(key)
        return response["Body"].read()
