from urllib.parse import parse_qs, unquote, urlparse

import frappe
from frappe.core.doctype.file.file import File

from frappe_s3_attachment.controller import get_s3_client

GENERATE_FILE_ENDPOINT = (
    "/api/method/frappe_s3_attachment.controller.generate_file"
)


class CustomFile(File):
    def get_content(self):
        """
        Override get_content to fetch file content from S3 when the file
        is stored on S3 instead of the local filesystem.
        """
        if self.is_s3_file():
            return self._get_content_from_s3()

        return super().get_content()

    def get_full_path(self):
        """Return a resolvable path for S3-backed files instead of throwing.

        Core's File.get_full_path() maps file_url to a local disk path and, for
        our private URL (``/api/method/...generate_file?key=...``), fails
        is_safe_path() and raises "Cannot access file path". This fires during a
        document *amend*: Frappe copies the attachment into a new File row and
        validates it on save, before our after_insert hook runs. Returning an
        absolute URL makes core treat the file as remote — the same way it
        handles any ``http(s)://`` file_url — so no local path is resolved.
        """
        if self.is_s3_file():
            log = "[s3_attachment] get_full_path: remote S3 file %s"
            frappe.logger("frappe_s3_attachment").debug(log, self.file_url)
            return frappe.utils.get_url(self.file_url)

        return super().get_full_path()

    def is_s3_file(self):
        """True if file_url points to S3 (generate_file endpoint, or https with an S3 key)."""
        url = self.file_url or ""
        return bool(
            url.startswith(GENERATE_FILE_ENDPOINT)
            or (url.startswith("https:") and self.content_hash)
        )

    def _s3_key(self):
        """The S3 object key for this file.

        Prefer content_hash (where the key is stored on upload). Amended-document
        copies, however, carry the key only inside file_url's ``?key=`` query
        (their content_hash is empty), so fall back to parsing it from file_url.
        """
        if self.content_hash:
            return self.content_hash

        query = parse_qs(urlparse(self.file_url or "").query)
        key = query.get("key", [None])[0]
        return unquote(key) if key else None

    def _get_content_from_s3(self):
        """Fetch file content from S3 using the object key."""
        key = self._s3_key()
        if not key:
            frappe.throw(f"S3 key not found for file {self.name}")

        s3 = get_s3_client()
        response = s3.read_file_from_s3(key)
        return response["Body"].read()
