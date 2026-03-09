import re

import frappe
from frappe.core.doctype.file.file import File

from frappe_s3_attachment.controller import S3Operations


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
		"""Check if the file URL points to S3 storage."""
		if not self.file_url:
			return False
		return bool(
			re.match(
				r"^(https:|/api/method/frappe_s3_attachment\.controller\.generate_file)",
				self.file_url,
			)
		)

	def _get_content_from_s3(self):
		"""Fetch file content from S3 using the content_hash (S3 key)."""
		key = self.content_hash
		if not key:
			frappe.throw(f"S3 key (content_hash) not found for file {self.name}")

		s3 = S3Operations()
		response = s3.read_file_from_s3(key)
		return response["Body"].read()
