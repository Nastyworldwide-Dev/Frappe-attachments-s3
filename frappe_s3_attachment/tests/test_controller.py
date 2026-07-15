"""Tests for S3 upload controller — verifies ACL handling."""

import unittest
from unittest.mock import patch, MagicMock
from urllib.parse import quote


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


class TestSecretReadViaGetPassword(unittest.TestCase):
    """Verify the AWS secret is read from encrypted Password storage (SEC).

    aws_secret was a plain Data field: readable in cleartext through the API
    and archived in Version history. As a Password field it must be fetched
    via get_password().
    """

    def _build_client(self, *, aws_key, secret):
        mock_settings = MagicMock()
        mock_settings.aws_key = aws_key
        mock_settings.get_password.return_value = secret
        mock_settings.region_name = "us-east-1"
        mock_settings.bucket_name = "b"
        mock_settings.folder_name = None

        mock_frappe = MagicMock()
        mock_frappe.get_doc.return_value = mock_settings

        mock_boto3 = MagicMock()

        with (
            patch("frappe_s3_attachment.controller.frappe", mock_frappe),
            patch("frappe_s3_attachment.controller.boto3", mock_boto3),
        ):
            from frappe_s3_attachment.controller import S3Operations

            S3Operations()

        return mock_settings, mock_boto3.client.call_args[1]

    def test_secret_comes_from_get_password(self):
        settings, kwargs = self._build_client(aws_key="AK", secret="decrypted-secret")
        settings.get_password.assert_called_once_with(
            "aws_secret", raise_exception=False
        )
        self.assertEqual(kwargs.get("aws_secret_access_key"), "decrypted-secret")

    def test_missing_secret_falls_back_to_default_credential_chain(self):
        _, kwargs = self._build_client(aws_key="AK", secret=None)
        self.assertNotIn("aws_secret_access_key", kwargs)


class TestEncryptAwsSecretPatch(unittest.TestCase):
    """Verify the one-time patch encrypts the plaintext secret and purges history."""

    def _run_patch(self, *, plaintext):
        mock_frappe = MagicMock()
        mock_frappe.db.get_value.return_value = plaintext

        with (
            patch(
                "frappe_s3_attachment.patches.encrypt_aws_secret.frappe", mock_frappe
            ),
            patch(
                "frappe.utils.password.set_encrypted_password", create=True
            ) as mock_set,
        ):
            from frappe_s3_attachment.patches.encrypt_aws_secret import execute

            execute()

        return mock_frappe, mock_set

    def test_plaintext_secret_is_encrypted_and_masked(self):
        mock_frappe, mock_set = self._run_patch(plaintext="old-plain-secret")
        mock_set.assert_called_once_with(
            "S3 File Attachment", "S3 File Attachment", "old-plain-secret", "aws_secret"
        )
        mock_frappe.db.set_single_value.assert_called_once()
        mock_frappe.db.delete.assert_called_once_with(
            "Version", {"ref_doctype": "S3 File Attachment"}
        )

    def test_no_secret_still_purges_versions_without_encrypting(self):
        mock_frappe, mock_set = self._run_patch(plaintext=None)
        self.assertEqual(mock_set.call_count, 0)
        mock_frappe.db.delete.assert_called_once()

    def test_singles_read_disables_default_ordering(self):
        # tabSingles has no `modified` column; get_value's default ordering
        # raises OperationalError 1054 during migrate unless order_by=None.
        mock_frappe, _ = self._run_patch(plaintext="old-plain-secret")
        mock_frappe.db.get_value.assert_called_once_with(
            "Singles",
            {"doctype": "S3 File Attachment", "field": "aws_secret"},
            "value",
            order_by=None,
        )


class TestUploadMetadataAscii(unittest.TestCase):
    """Verify uploads survive non-ASCII file names (M).

    S3 user metadata must be ASCII; sending a raw non-ASCII file_name makes
    boto3 raise and blocks the whole document save.
    """

    def _run_upload(self, file_name):
        mock_settings = MagicMock()
        mock_settings.aws_key = "test-key"
        mock_settings.aws_secret = "test-secret"
        mock_settings.region_name = "us-east-1"
        mock_settings.bucket_name = "test-bucket"
        mock_settings.folder_name = "test-folder"

        mock_frappe = MagicMock()
        mock_frappe.get_doc.return_value = mock_settings
        mock_frappe.local.conf.get.return_value = None

        mock_magic = MagicMock()
        mock_magic.from_file.return_value = "application/pdf"

        mock_client = MagicMock()
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client

        with (
            patch("frappe_s3_attachment.controller.frappe", mock_frappe),
            patch("frappe_s3_attachment.controller.boto3", mock_boto3),
            patch("frappe_s3_attachment.controller.magic", mock_magic),
        ):
            from frappe_s3_attachment.controller import S3Operations

            ops = S3Operations()
            ops.S3_CLIENT = mock_client
            ops.key_generator = MagicMock(return_value="test-folder/K_test.pdf")
            ops.upload_files_to_s3_with_key(
                "/tmp/test.pdf",
                file_name,
                is_private=True,
                parent_doctype="Sales Invoice",
                parent_name="SI-001",
            )

        return mock_client.upload_file.call_args[1]["ExtraArgs"]

    def test_non_ascii_file_name_is_encoded_and_reversible(self):
        from urllib.parse import unquote

        meta = self._run_upload("リポート.pdf")["Metadata"]["file_name"]
        meta.encode("ascii")  # must not raise
        self.assertEqual(unquote(meta), "リポート.pdf")

    def test_ascii_file_name_unchanged(self):
        meta = self._run_upload("report.pdf")["Metadata"]["file_name"]
        self.assertEqual(meta, "report.pdf")


class TestGetUrlContentDisposition(unittest.TestCase):
    """Verify get_url sends a valid, header-safe Content-Disposition (M)."""

    def _params(self, file_name):
        mock_settings = MagicMock()
        mock_settings.aws_key = "k"
        mock_settings.aws_secret = "s"
        mock_settings.region_name = "us-east-1"
        mock_settings.bucket_name = "b"
        mock_settings.folder_name = None
        mock_settings.signed_url_expiry_time = 120

        mock_frappe = MagicMock()
        mock_frappe.get_doc.return_value = mock_settings

        mock_client = MagicMock()
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client

        with (
            patch("frappe_s3_attachment.controller.frappe", mock_frappe),
            patch("frappe_s3_attachment.controller.boto3", mock_boto3),
        ):
            from frappe_s3_attachment.controller import S3Operations

            ops = S3Operations()
            ops.S3_CLIENT = mock_client
            ops.get_url("2026/k_f.pdf", file_name)

        return mock_client.generate_presigned_url.call_args[1]["Params"]

    def test_disposition_is_valid_inline(self):
        p = self._params("report.pdf")
        self.assertEqual(
            p["ResponseContentDisposition"], 'inline; filename="report.pdf"'
        )

    def test_disposition_survives_hostile_file_name(self):
        v = self._params('リポ"ー\r\nト evil.pdf')["ResponseContentDisposition"]
        v.encode("ascii")  # must not raise
        self.assertNotIn("\r", v)
        self.assertNotIn("\n", v)
        self.assertTrue(v.startswith('inline; filename="'))
        self.assertTrue(v.endswith('"'))
        inner = v[len('inline; filename="') : -1]
        self.assertNotIn('"', inner)


class TestDeleteFromS3ErrorReason(unittest.TestCase):
    """Verify delete_from_s3 surfaces the real S3 error, not 'Access denied' (M)."""

    def _run_failing_delete(self, exc):
        mock_settings = MagicMock()
        mock_settings.delete_file_from_cloud = True
        mock_settings.bucket_name = "b"

        mock_frappe = MagicMock()
        mock_frappe.get_doc.return_value = mock_settings
        mock_frappe._ = lambda s: s

        mock_client = MagicMock()
        mock_client.delete_object.side_effect = exc
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client

        with (
            patch("frappe_s3_attachment.controller.frappe", mock_frappe),
            patch("frappe_s3_attachment.controller.boto3", mock_boto3),
        ):
            from frappe_s3_attachment.controller import S3Operations

            ops = S3Operations()
            ops.S3_CLIENT = mock_client
            ops.delete_from_s3("2026/k_f.pdf")

        return mock_frappe.throw.call_args[0][0]

    def test_client_error_code_is_surfaced(self):
        from botocore.exceptions import ClientError

        msg = self._run_failing_delete(
            ClientError({"Error": {"Code": "NoSuchBucket"}}, "DeleteObject")
        )
        self.assertIn("NoSuchBucket", msg)

    def test_connection_error_is_caught_and_named(self):
        from botocore.exceptions import EndpointConnectionError

        msg = self._run_failing_delete(
            EndpointConnectionError(endpoint_url="https://s3.example.com")
        )
        self.assertIn("EndpointConnectionError", msg)


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
        file_name="f.pdf",
        key="k",
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
            mock_ops.return_value.upload_files_to_s3_with_key.return_value = key

            from frappe_s3_attachment.controller import file_upload_to_s3

            doc = MagicMock()
            doc.file_url = file_url
            doc.is_folder = is_folder
            doc.is_private = is_private
            doc.attached_to_doctype = attached_to_doctype
            doc.attached_to_name = "X-1"
            doc.file_name = file_name

            file_upload_to_s3(doc, "after_insert")

            # Removals must be scheduled for after-commit, not run inline;
            # "removes" reflects what happens once the transaction commits.
            removes_immediate = mock_os.remove.call_count
            for call in mock_frappe.db.after_commit.add.call_args_list:
                call[0][0]()

            sql_call = mock_frappe.db.sql.call_args
            return {
                "uploads": mock_ops.return_value.upload_files_to_s3_with_key.call_count,
                "removes_immediate": removes_immediate,
                "removes": mock_os.remove.call_count,
                "file_url": sql_call[0][1][0] if sql_call else None,
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

    def test_removal_is_deferred_until_commit(self):
        """The local file must not be deleted inside the transaction (H4).

        A rollback after the hook ran would revert the row to a local
        file_url whose file was already deleted — the attachment is lost.
        """
        r = self._run_hook(file_url="/private/files/f.pdf", other_local_refs=0)
        self.assertEqual(r["removes_immediate"], 0)
        self.assertEqual(r["removes"], 1)

    def test_private_file_url_query_is_encoded(self):
        """Keys contain spaces (doctype names) and file names may contain
        '&' or '#'; unencoded they truncate the query and break download (M)."""
        from urllib.parse import parse_qs

        r = self._run_hook(
            file_url="/private/files/f.pdf",
            key="2026/07/14/Purchase Order/K1_x.pdf",
            file_name="P&L report.pdf",
        )
        query = r["file_url"].split("?", 1)[1]
        self.assertNotIn(" ", query)
        parsed = parse_qs(query)
        self.assertEqual(parsed["key"][0], "2026/07/14/Purchase Order/K1_x.pdf")
        self.assertEqual(parsed["file_name"][0], "P&L report.pdf")


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


KEY = "2026/05/18/Purchase Order/1TLG3BY1_x.pdf"


def _private_url(key):
    from urllib.parse import quote

    return (
        "/api/method/frappe_s3_attachment.controller.generate_file"
        "?key={}&file_name=x.pdf".format(quote(key))
    )


class TestDeleteSharedKeyInFileUrl(unittest.TestCase):
    """Verify delete_from_cloud sees references that live only in file_url (H3).

    Amending a document copies its attachments into File records whose
    file_url contains the S3 key but whose content_hash is empty (19 such
    records exist in live data). Deleting the original must not delete the
    S3 object those copies still point at.
    """

    def _run(self, *, hash_refs, url_files):
        import types as _types

        mock_frappe = MagicMock()
        mock_frappe.local._s3_operations = None
        mock_frappe.db.count.return_value = hash_refs
        mock_frappe.get_all.return_value = [
            _types.SimpleNamespace(**f) for f in url_files
        ]
        deleted = {"n": 0}

        with (
            patch("frappe_s3_attachment.controller.frappe", mock_frappe),
            patch("frappe_s3_attachment.controller.S3Operations") as mock_ops,
        ):
            mock_ops.return_value.delete_from_s3.side_effect = lambda key: (
                deleted.__setitem__("n", deleted["n"] + 1)
            )

            from frappe_s3_attachment.controller import delete_from_cloud

            doc = MagicMock()
            doc.name = "F-original"
            doc.content_hash = KEY
            doc.file_url = _private_url(KEY)
            delete_from_cloud(doc, "on_trash")

        return deleted["n"]

    def test_skips_when_key_referenced_only_in_file_url(self):
        """Copies without content_hash must still protect the S3 object."""
        n = self._run(hash_refs=0, url_files=[{"file_url": _private_url(KEY)}])
        self.assertEqual(n, 0)

    def test_deletes_when_nothing_references_key(self):
        self.assertEqual(self._run(hash_refs=0, url_files=[]), 1)

    def test_deletes_despite_like_overmatch_of_unrelated_file(self):
        """A file_url that only shares a substring must not block deletion (SEC-01)."""
        overmatch = {"file_url": _private_url("OTHER/1TLG3BY1_x.pdf.different")}
        self.assertEqual(self._run(hash_refs=0, url_files=[overmatch]), 1)


class TestGenerateFileAuthKeyInFileUrl(unittest.TestCase):
    """Verify _check_file_access finds files whose key lives only in file_url (H3).

    If the original File row (the one carrying content_hash == key) is deleted,
    amended-document copies must remain downloadable via their file_url — but a
    mere substring over-match must NOT grant access to a different object (SEC-01).
    """

    def _run(self, *, url_match_files, has_perm=True):
        import types as _types

        mock_frappe = MagicMock()
        mock_frappe.local._s3_operations = None
        mock_frappe.PermissionError = type("PermissionError", (Exception,), {})
        mock_frappe._ = lambda s: s
        mock_frappe.session.user = "user@example.com"
        mock_frappe.db.get_single_value.return_value = 1  # strict mode
        mock_frappe.get_doc.return_value.is_downloadable.return_value = has_perm
        signed = {"n": 0}

        def _get_all(doctype, filters=None, fields=None):
            if "content_hash" in (filters or {}):
                return []
            return [_types.SimpleNamespace(**f) for f in url_match_files]

        mock_frappe.get_all.side_effect = _get_all

        with (
            patch("frappe_s3_attachment.controller.frappe", mock_frappe),
            patch("frappe_s3_attachment.controller.S3Operations") as mock_ops,
        ):
            mock_ops.return_value.get_url.side_effect = lambda *a, **k: (
                signed.__setitem__("n", signed["n"] + 1) or "https://signed"
            )
            from frappe_s3_attachment.controller import generate_file

            try:
                generate_file(key=KEY, file_name="x.pdf")
                denied = False
            except mock_frappe.PermissionError:
                denied = True

        return denied, signed["n"]

    def _copy(self, **over):
        f = {
            "name": "F-copy",
            "is_private": 1,
            "attached_to_doctype": "Purchase Order",
            "attached_to_name": "PO-1",
            "file_url": _private_url(KEY),
        }
        f.update(over)
        return f

    def test_copy_with_key_in_file_url_is_accessible(self):
        denied, signed = self._run(url_match_files=[self._copy()], has_perm=True)
        self.assertFalse(denied)
        self.assertEqual(signed, 1)

    def test_copy_without_permission_still_denied(self):
        denied, signed = self._run(url_match_files=[self._copy()], has_perm=False)
        self.assertTrue(denied)
        self.assertEqual(signed, 0)

    def test_overmatched_public_file_does_not_grant_access(self):
        """A public file whose url only shares a substring with the requested
        key must not pass the permission gate or get signed (SEC-01)."""
        unrelated = self._copy(
            name="F-other",
            is_private=0,
            attached_to_doctype=None,
            attached_to_name=None,
            file_url=_private_url("SOMEWHERE/1TLG3BY1_x.pdf.other"),
        )
        denied, signed = self._run(url_match_files=[unrelated], has_perm=True)
        self.assertTrue(denied)
        self.assertEqual(signed, 0)


class TestGenerateFileAuth(unittest.TestCase):
    """Verify generate_file enforces document permission before signing (H2)."""

    def _run(self, *, files, has_perm=True):
        import types as _types

        mock_frappe = MagicMock()
        mock_frappe.local._s3_operations = None
        mock_frappe.PermissionError = type("PermissionError", (Exception,), {})
        mock_frappe._ = lambda s: s
        mock_frappe.session.user = "user@example.com"
        mock_frappe.db.get_single_value.return_value = 1  # strict mode
        mock_frappe.get_all.return_value = [_types.SimpleNamespace(**f) for f in files]
        mock_frappe.get_doc.return_value.is_downloadable.return_value = has_perm

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


class TestDownloadPermissionModes(unittest.TestCase):
    """Default mode allows any logged-in user; strict mode gates on native File permission.

    The strict per-document gate (introduced in 0.1.8) broke sites whose users
    hold logins but not doctype role permissions — every attachment 403'd. The
    default must therefore stay backward compatible (authenticated users only),
    with strict document-permission checks as an opt-in that mirrors Frappe's
    native File.is_downloadable (owner, shares, attached-document read).
    """

    def _run(
        self,
        *,
        strict,
        is_downloadable=False,
        has_perm=False,
        user="user@example.com",
        files=None,
    ):
        import types as _types

        mock_frappe = MagicMock()
        mock_frappe.local._s3_operations = None
        mock_frappe.PermissionError = type("PermissionError", (Exception,), {})
        mock_frappe._ = lambda s: s
        mock_frappe.session.user = user
        mock_frappe.db.get_single_value.return_value = strict
        mock_frappe.has_permission.return_value = has_perm
        mock_frappe.get_doc.return_value.is_downloadable.return_value = is_downloadable
        if files is None:
            files = [
                {
                    "name": "F1",
                    "is_private": 1,
                    "attached_to_doctype": "Purchase Order",
                    "attached_to_name": "PO-1",
                }
            ]
        mock_frappe.get_all.return_value = [_types.SimpleNamespace(**f) for f in files]

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

    def test_default_mode_allows_any_logged_in_user(self):
        """Regression: users without document permission must still download."""
        denied, signed = self._run(strict=0, is_downloadable=False, has_perm=False)
        self.assertFalse(denied)
        self.assertEqual(signed, 1)

    def test_default_mode_still_denies_guest(self):
        denied, signed = self._run(strict=0, user="Guest")
        self.assertTrue(denied)
        self.assertEqual(signed, 0)

    def test_default_mode_unknown_key_denied(self):
        denied, signed = self._run(strict=0, files=[])
        self.assertTrue(denied)
        self.assertEqual(signed, 0)

    def test_strict_mode_denies_without_native_permission(self):
        denied, signed = self._run(strict=1, is_downloadable=False)
        self.assertTrue(denied)
        self.assertEqual(signed, 0)

    def test_strict_mode_allows_via_native_file_permission(self):
        """Owner/shared/attached-doc access comes from File.is_downloadable."""
        denied, signed = self._run(strict=1, is_downloadable=True)
        self.assertFalse(denied)
        self.assertEqual(signed, 1)


class TestDoubleEncodedKeyResolution(unittest.TestCase):
    """Desk sidebar encodeURI()s file_url, double-encoding stored %-escapes.

    Rows written since the URL-encoding fix store the key quoted in file_url
    (e.g. Purchase%20Order); v15's attachments sidebar encodeURI()s the href so
    the server decodes the key only half-way (key arrives as
    'ERP/.../Purchase%20Order/...'). generate_file must fall back to the
    once-unquoted key — and sign S3 with the key that actually matched.
    """

    RAW_KEY = "ERP/2026/05/15/Purchase Order/EBOVX8YR_x.xlsx"
    HALF_DECODED = "ERP/2026/05/15/Purchase%20Order/EBOVX8YR_x.xlsx"

    def _run(self, *, request_key, rows_by_hash, file_name="x.xlsx"):
        import types as _types

        mock_frappe = MagicMock()
        mock_frappe.local._s3_operations = None
        mock_frappe.PermissionError = type("PermissionError", (Exception,), {})
        mock_frappe._ = lambda s: s
        mock_frappe.session.user = "user@example.com"
        mock_frappe.db.get_single_value.return_value = 0  # default mode

        def _get_all(doctype, filters=None, fields=None):
            content_hash = (filters or {}).get("content_hash")
            if content_hash is not None:
                return [
                    _types.SimpleNamespace(**r)
                    for r in rows_by_hash
                    if r["content_hash"] == content_hash
                ]
            return []  # no file_url LIKE matches in these scenarios

        mock_frappe.get_all.side_effect = _get_all
        signed = []

        with (
            patch("frappe_s3_attachment.controller.frappe", mock_frappe),
            patch("frappe_s3_attachment.controller.S3Operations") as mock_ops,
        ):
            mock_ops.return_value.get_url.side_effect = lambda key, fname=None: (
                signed.append((key, fname)) or "https://signed"
            )
            from frappe_s3_attachment.controller import generate_file

            try:
                generate_file(key=request_key, file_name=file_name)
                denied = False
            except mock_frappe.PermissionError:
                denied = True

        return denied, signed

    def _row(self, content_hash, **over):
        row = {
            "name": "F-" + content_hash[-8:],
            "is_private": 1,
            "attached_to_doctype": "Purchase Order",
            "attached_to_name": "PO-1",
            "file_url": "/api/method/frappe_s3_attachment.controller.generate_file"
            "?key=" + quote(content_hash) + "&file_name=x.xlsx",
            "content_hash": content_hash,
        }
        row.update(over)
        return row

    def test_half_decoded_key_resolves_and_signs_raw_key(self):
        """Regression: sidebar-clicked keys arrive %-encoded and must still work."""
        denied, signed = self._run(
            request_key=self.HALF_DECODED,
            rows_by_hash=[self._row(self.RAW_KEY)],
            file_name="my%20file.xlsx",
        )
        self.assertFalse(denied)
        # S3 must be signed with the key that matched, not the mangled input,
        # and the file_name must be un-mangled the same way.
        self.assertEqual(signed, [(self.RAW_KEY, "my file.xlsx")])

    def test_raw_key_still_resolves_directly(self):
        denied, signed = self._run(
            request_key=self.RAW_KEY, rows_by_hash=[self._row(self.RAW_KEY)]
        )
        self.assertFalse(denied)
        self.assertEqual(signed, [(self.RAW_KEY, "x.xlsx")])

    def test_file_name_unquoted_even_when_key_resolves_directly(self):
        """A space-free key (e.g. Item folder) resolves on the first try, but
        the sidebar still double-encodes file_name independently."""
        key = "ERP/2026/06/22/Item/V4722XBH_draft_jacket.jpg"
        denied, signed = self._run(
            request_key=key,
            rows_by_hash=[self._row(key)],
            file_name="draft%20jacket.jpg",
        )
        self.assertFalse(denied)
        self.assertEqual(signed, [(key, "draft jacket.jpg")])

    def test_as_received_key_wins_over_unquoted_sibling(self):
        """A literal %-containing key (custom hook) must not be hijacked by its
        decoded sibling: the as-received match is tried first."""
        literal = self._row(self.HALF_DECODED)  # hook-generated literal '%20'
        decoded = self._row(self.RAW_KEY)
        denied, signed = self._run(
            request_key=self.HALF_DECODED, rows_by_hash=[literal, decoded]
        )
        self.assertFalse(denied)
        self.assertEqual(signed[0][0], self.HALF_DECODED)

    def test_unknown_key_still_denied_after_fallback(self):
        denied, signed = self._run(
            request_key="ERP/2026/01/01/Item/NOPE_x.pdf", rows_by_hash=[]
        )
        self.assertTrue(denied)
        self.assertEqual(signed, [])


class TestUploadErrorHandling(unittest.TestCase):
    """Verify S3 client errors during upload are surfaced via frappe.throw (M2)."""

    def _run_failing_upload(self, exc):
        """Run an upload whose S3 client raises ``exc``; return the mock_frappe."""
        import boto3 as real_boto3

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
        mock_client.upload_file.side_effect = exc
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

        return mock_frappe

    def test_client_error_is_caught(self):
        from botocore.exceptions import ClientError

        mock_frappe = self._run_failing_upload(
            ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
        )
        self.assertTrue(mock_frappe.throw.called)

    def test_thrown_message_includes_s3_error_code(self):
        """The thrown message must surface the real S3 error code so the
        admin can diagnose the failure without reading server logs."""
        from botocore.exceptions import ClientError

        mock_frappe = self._run_failing_upload(
            ClientError({"Error": {"Code": "NoSuchBucket"}}, "PutObject")
        )
        thrown_msg = mock_frappe.throw.call_args[0][0]
        self.assertIn("NoSuchBucket", thrown_msg)

    def test_thrown_message_includes_connection_error_name(self):
        """A connection failure (no error code) must still name the cause."""
        from botocore.exceptions import EndpointConnectionError

        mock_frappe = self._run_failing_upload(
            EndpointConnectionError(endpoint_url="https://s3.example.com")
        )
        thrown_msg = mock_frappe.throw.call_args[0][0]
        self.assertIn("EndpointConnectionError", thrown_msg)


class TestS3ErrorReason(unittest.TestCase):
    """Verify _s3_error_reason extracts a concise, user-safe failure reason."""

    def test_client_error_returns_aws_code(self):
        from botocore.exceptions import ClientError

        from frappe_s3_attachment.controller import _s3_error_reason

        e = ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
        self.assertEqual(_s3_error_reason(e), "AccessDenied")

    def test_client_error_without_code_falls_back_to_class_name(self):
        from botocore.exceptions import ClientError

        from frappe_s3_attachment.controller import _s3_error_reason

        e = ClientError({"Error": {}}, "PutObject")
        self.assertEqual(_s3_error_reason(e), "ClientError")

    def test_botocore_error_returns_class_name(self):
        from botocore.exceptions import EndpointConnectionError

        from frappe_s3_attachment.controller import _s3_error_reason

        e = EndpointConnectionError(endpoint_url="https://s3.example.com")
        self.assertEqual(_s3_error_reason(e), "EndpointConnectionError")


class TestMigrationSingleFileUpload(unittest.TestCase):
    """Verify upload_existing_files_s3 handles the edge cases found in live data.

    H1: files not attached to any doctype must fall back to "File" instead of
    concatenating None into the S3 key (TypeError that kills the whole job).
    H2: a local file shared by several File records must not be deleted while
    other records still reference it (mirrors file_upload_to_s3).
    """

    def _run(
        self,
        *,
        attached_to_doctype="Journal Entry",
        file_name="f.pdf",
        other_local_refs=0,
        local_exists=True,
        key="2026/07/14/X/KEY12345_f.pdf",
    ):
        mock_frappe = MagicMock()
        mock_frappe.local._s3_operations = None
        mock_frappe.utils.get_site_path.return_value = "/site"
        mock_frappe.db.count.return_value = other_local_refs

        doc = MagicMock()
        doc.name = "F1"
        doc.file_url = "/private/files/f.pdf"
        doc.is_private = 1
        doc.attached_to_doctype = attached_to_doctype
        doc.attached_to_name = None
        doc.file_name = file_name
        mock_frappe.get_doc.return_value = doc

        captured = {}

        with (
            patch("frappe_s3_attachment.controller.frappe", mock_frappe),
            patch("frappe_s3_attachment.controller.S3Operations") as mock_ops,
            patch("frappe_s3_attachment.controller.os") as mock_os,
        ):
            mock_os.path.exists.return_value = local_exists
            mock_os.path.basename.side_effect = lambda p: p.rsplit("/", 1)[-1]

            def _upload(file_path, file_name, is_private, parent_doctype, parent_name):
                captured["file_name"] = file_name
                captured["parent_doctype"] = parent_doctype
                return key

            mock_ops.return_value.upload_files_to_s3_with_key.side_effect = _upload

            from frappe_s3_attachment.controller import upload_existing_files_s3

            captured["result"] = upload_existing_files_s3("F1")
            # Execute any deferred after-commit callbacks so "removes" reflects
            # what would happen once the transaction commits.
            for call in mock_frappe.db.after_commit.add.call_args_list:
                call[0][0]()
            captured["removes"] = mock_os.remove.call_count
            sql_call = mock_frappe.db.sql.call_args
            captured["file_url"] = sql_call[0][1][0] if sql_call else None

        return captured

    def test_unattached_file_uses_file_fallback_doctype(self):
        """A File with no attached_to_doctype must migrate under "File" (H1)."""
        r = self._run(attached_to_doctype=None)
        self.assertEqual(r["parent_doctype"], "File")
        self.assertTrue(r["result"])

    def test_missing_file_name_falls_back_to_basename(self):
        """A File row without file_name must not crash key generation (H1)."""
        r = self._run(file_name=None)
        self.assertEqual(r["file_name"], "f.pdf")

    def test_keeps_shared_local_file(self):
        """The local file must survive while other records still use it (H2)."""
        r = self._run(other_local_refs=1)
        self.assertEqual(r["removes"], 0)

    def test_removes_local_file_when_last_reference(self):
        """The local file is removed once no other record references it."""
        r = self._run(other_local_refs=0)
        self.assertEqual(r["removes"], 1)

    def test_returns_false_when_local_file_missing(self):
        """A missing local file is reported as skipped, not uploaded."""
        r = self._run(local_exists=False)
        self.assertFalse(r["result"])

    def test_migrated_private_url_query_is_encoded(self):
        """The migration path must produce the same encoded URLs as the hook (M)."""
        from urllib.parse import parse_qs

        r = self._run(
            key="2026/07/14/Purchase Order/K1_x.pdf", file_name="P&L report.pdf"
        )
        query = r["file_url"].split("?", 1)[1]
        self.assertNotIn(" ", query)
        parsed = parse_qs(query)
        self.assertEqual(parsed["key"][0], "2026/07/14/Purchase Order/K1_x.pdf")
        self.assertEqual(parsed["file_name"][0], "P&L report.pdf")


class TestMigrationJob(unittest.TestCase):
    """Verify _migrate_existing_files isolates failures and respects skip rules."""

    def _run(self, files, *, upload=None, conf_ignore=None):
        mock_frappe = MagicMock()
        mock_frappe.local.conf.get.return_value = conf_ignore
        mock_frappe.get_all.return_value = files

        with (
            patch("frappe_s3_attachment.controller.frappe", mock_frappe),
            patch(
                "frappe_s3_attachment.controller.upload_existing_files_s3"
            ) as mock_up,
        ):
            if upload:
                mock_up.side_effect = upload
            else:
                mock_up.return_value = True

            from frappe_s3_attachment.controller import _migrate_existing_files

            summary = _migrate_existing_files(user="admin@example.com")

        return summary, mock_up, mock_frappe

    def _local(self, name, doctype="Journal Entry"):
        return {
            "name": name,
            "file_url": "/private/files/{}.pdf".format(name),
            "attached_to_doctype": doctype,
        }

    def test_one_failure_does_not_abort_the_job(self):
        """A file that fails must be recorded; the rest still migrate (H1)."""

        def upload(name):
            if name == "F2":
                raise TypeError("can only concatenate str")
            return True

        files = [self._local("F1"), self._local("F2"), self._local("F3")]
        summary, mock_up, _ = self._run(files, upload=upload)
        self.assertEqual(mock_up.call_count, 3)
        self.assertEqual(summary["migrated"], 2)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(len(summary["failures"]), 1)

    def test_skips_ignored_doctype(self):
        """Files attached to an ignored doctype must not be migrated."""
        files = [self._local("F1"), self._local("F2", doctype="Data Import")]
        summary, mock_up, _ = self._run(files)
        self.assertEqual(mock_up.call_count, 1)
        self.assertEqual(summary["migrated"], 1)
        self.assertEqual(summary["skipped"], 1)

    def test_skips_files_already_on_s3(self):
        """Already-migrated files and folders must not be re-uploaded."""
        files = [
            self._local("F1"),
            {
                "name": "F2",
                "file_url": (
                    "/api/method/frappe_s3_attachment.controller.generate_file?key=k"
                ),
                "attached_to_doctype": "Journal Entry",
            },
            {"name": "F3", "file_url": None, "attached_to_doctype": None},
        ]
        _, mock_up, _ = self._run(files)
        self.assertEqual(mock_up.call_count, 1)

    def test_publishes_completion_event(self):
        """The job must publish a completion summary for the settings page UI."""
        summary, _, mock_frappe = self._run([self._local("F1")])
        events = [c[0][0] for c in mock_frappe.publish_realtime.call_args_list]
        self.assertIn("s3_migration_complete", events)
        self.assertEqual(summary["migrated"], 1)


class TestMigrateEnqueueGuard(unittest.TestCase):
    """Verify migrate_existing_files cannot start duplicate concurrent jobs."""

    def _run(self, *, already_running):
        mock_frappe = MagicMock()
        with (
            patch("frappe_s3_attachment.controller.frappe", mock_frappe),
            patch(
                "frappe.utils.background_jobs.is_job_enqueued",
                return_value=already_running,
                create=True,
            ),
        ):
            from frappe_s3_attachment.controller import migrate_existing_files

            result = migrate_existing_files()
        return result, mock_frappe

    def test_second_click_does_not_enqueue(self):
        result, mock_frappe = self._run(already_running=True)
        self.assertEqual(result["status"], "already_running")
        self.assertEqual(mock_frappe.enqueue.call_count, 0)

    def test_first_click_enqueues_with_job_id(self):
        result, mock_frappe = self._run(already_running=False)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(mock_frappe.enqueue.call_count, 1)
        self.assertIn("job_id", mock_frappe.enqueue.call_args[1])


class TestGetMigrationSummary(unittest.TestCase):
    """Verify the pre-migration summary counts shown in the confirm dialog."""

    def test_counts(self):
        mock_frappe = MagicMock()
        mock_frappe.local.conf.get.return_value = None
        mock_frappe.get_all.return_value = [
            # two migratable local files sharing one physical file
            {
                "name": "F1",
                "file_url": "/private/files/a.csv",
                "attached_to_doctype": "Journal Entry",
            },
            {
                "name": "F2",
                "file_url": "/private/files/a.csv",
                "attached_to_doctype": "Journal Entry",
            },
            # unattached local file
            {
                "name": "F3",
                "file_url": "/private/files/b.html",
                "attached_to_doctype": None,
            },
            # ignored doctype
            {
                "name": "F4",
                "file_url": "/private/files/c.csv",
                "attached_to_doctype": "Data Import",
            },
            # already migrated
            {
                "name": "F5",
                "file_url": "/api/method/frappe_s3_attachment.controller.generate_file?key=k",
                "attached_to_doctype": "Journal Entry",
            },
        ]

        with (
            patch("frappe_s3_attachment.controller.frappe", mock_frappe),
            patch(
                "frappe.utils.background_jobs.is_job_enqueued",
                return_value=False,
                create=True,
            ),
        ):
            from frappe_s3_attachment.controller import get_migration_summary

            s = get_migration_summary()

        self.assertEqual(s["total_local"], 4)
        self.assertEqual(s["skipped_ignored"], 1)
        self.assertEqual(s["will_upload"], 3)
        self.assertEqual(s["unattached"], 1)
        self.assertEqual(s["shared_local"], 1)
        self.assertFalse(s["running"])


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
