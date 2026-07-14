from __future__ import unicode_literals

import datetime
import logging
import os
import random
import re
import string

from urllib.parse import parse_qs, quote, urlparse

import boto3

from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

import frappe


import magic


logger = logging.getLogger(__name__)


MIGRATION_JOB_ID = "frappe_s3_attachment::migrate_existing_files"


def _s3_error_reason(exc):
    """Return a concise, user-safe reason for an S3 upload failure.

    For AWS ClientErrors this is the AWS error code (e.g. ``AccessDenied``,
    ``NoSuchBucket``, ``SignatureDoesNotMatch``); for connection/credential
    failures it is the botocore exception class name (e.g.
    ``EndpointConnectionError``). The full exception is logged separately for
    deeper debugging. Surfacing the reason lets an admin diagnose a
    misconfigured bucket/credentials without reading server logs.
    """
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code")
        if code:
            return code
    return type(exc).__name__


class S3Operations(object):
    def __init__(self):
        """
        Function to initialise the aws settings from frappe S3 File attachment
        doctype.
        """
        self.s3_settings_doc = frappe.get_doc(
            "S3 File Attachment",
            "S3 File Attachment",
        )
        # aws_secret is a Password field stored encrypted; the doc attribute
        # only holds a mask, so read the real value via get_password.
        aws_secret = self.s3_settings_doc.get_password(
            "aws_secret", raise_exception=False
        )
        if self.s3_settings_doc.aws_key and aws_secret:
            self.S3_CLIENT = boto3.client(
                "s3",
                aws_access_key_id=self.s3_settings_doc.aws_key,
                aws_secret_access_key=aws_secret,
                region_name=self.s3_settings_doc.region_name,
                config=Config(signature_version="s3v4"),
            )
        else:
            self.S3_CLIENT = boto3.client(
                "s3",
                region_name=self.s3_settings_doc.region_name,
                config=Config(signature_version="s3v4"),
            )
        self.BUCKET = self.s3_settings_doc.bucket_name
        self.folder_name = self.s3_settings_doc.folder_name

    def strip_special_chars(self, file_name):
        """
        Strips file charachters which doesnt match the regex.
        """
        regex = re.compile("[^0-9a-zA-Z._-]")
        file_name = regex.sub("", file_name)
        return file_name

    def key_generator(self, file_name, parent_doctype, parent_name):
        """
        Generate keys for s3 objects uploaded with file name attached.
        """
        hook_cmd = frappe.get_hooks().get("s3_key_generator")
        if hook_cmd:
            try:
                k = frappe.get_attr(hook_cmd[0])(
                    file_name=file_name,
                    parent_doctype=parent_doctype,
                    parent_name=parent_name,
                )
                if k:
                    return k.rstrip("/").lstrip("/")
            except Exception as e:
                logger.warning("[s3_attachment] s3_key_generator hook failed: %s", e)

        file_name = file_name.replace(" ", "_")
        file_name = self.strip_special_chars(file_name)
        key = "".join(
            random.choice(string.ascii_uppercase + string.digits) for _ in range(8)
        )

        today = datetime.datetime.now()
        year = today.strftime("%Y")
        month = today.strftime("%m")
        day = today.strftime("%d")

        doc_path = None

        if not doc_path:
            if self.folder_name:
                final_key = (
                    self.folder_name
                    + "/"
                    + year
                    + "/"
                    + month
                    + "/"
                    + day
                    + "/"
                    + parent_doctype
                    + "/"
                    + key
                    + "_"
                    + file_name
                )
            else:
                final_key = (
                    year
                    + "/"
                    + month
                    + "/"
                    + day
                    + "/"
                    + parent_doctype
                    + "/"
                    + key
                    + "_"
                    + file_name
                )
            return final_key
        else:
            final_key = doc_path + "/" + key + "_" + file_name
            return final_key

    def upload_files_to_s3_with_key(
        self, file_path, file_name, is_private, parent_doctype, parent_name
    ):
        """
        Uploads a new file to S3.
        Strips the file extension to set the content_type in metadata.
        """
        mime_type = magic.from_file(file_path, mime=True)
        key = self.key_generator(file_name, parent_doctype, parent_name)
        content_type = mime_type
        try:
            extra_args = {
                "ContentType": content_type,
                # S3 user metadata must be ASCII; quote() keeps non-ASCII file
                # names uploadable and reversible (unquote to read back).
                "Metadata": {
                    "ContentType": content_type,
                    "file_name": quote(file_name),
                },
            }
            if not is_private and frappe.local.conf.get("s3_use_acl"):
                extra_args["ACL"] = "public-read"
            self.S3_CLIENT.upload_file(
                file_path,
                self.BUCKET,
                key,
                ExtraArgs=extra_args,
            )

        except (boto3.exceptions.S3UploadFailedError, ClientError, BotoCoreError) as e:
            reason = _s3_error_reason(e)
            logger.error("[s3_attachment] upload failed for key %s: %s", key, e)
            frappe.throw(frappe._("File Upload Failed: {0}").format(reason))
        return key

    def delete_from_s3(self, key):
        """Delete file from s3"""
        if self.s3_settings_doc.delete_file_from_cloud:
            try:
                self.S3_CLIENT.delete_object(
                    Bucket=self.s3_settings_doc.bucket_name, Key=key
                )
            except (ClientError, BotoCoreError) as e:
                reason = _s3_error_reason(e)
                logger.error("[s3_attachment] delete failed for key %s: %s", key, e)
                frappe.throw(
                    frappe._("Could not delete file from S3: {0}").format(reason)
                )

    def read_file_from_s3(self, key):
        """
        Function to read file from a s3 file.
        """
        return self.S3_CLIENT.get_object(Bucket=self.BUCKET, Key=key)

    def get_url(self, key, file_name=None):
        """
        Return url.

        :param bucket: s3 bucket name
        :param key: s3 object key
        """
        if self.s3_settings_doc.signed_url_expiry_time:
            self.signed_url_expiry_time = self.s3_settings_doc.signed_url_expiry_time  # noqa
        else:
            self.signed_url_expiry_time = 120
        params = {
            "Bucket": self.BUCKET,
            "Key": key,
        }
        if file_name:
            params["ResponseContentDisposition"] = 'inline; filename="{}"'.format(
                _header_safe_filename(file_name)
            )

        url = self.S3_CLIENT.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=self.signed_url_expiry_time,
        )

        return url


def _header_safe_filename(file_name):
    """ASCII-only, quote/CR/LF-free value for a Content-Disposition header."""
    cleaned = re.sub(r'["\r\n]', "", file_name)
    return cleaned.encode("ascii", "ignore").decode() or "download"


def _make_file_url(s3_upload, key, file_name, is_private):
    """Build the stored file_url for an uploaded object.

    Values are URL-encoded: keys contain spaces (doctype names) and file
    names may contain '&' or '#', which would truncate the query string.
    """
    logger.debug("[s3_attachment] building file_url for key %s", key)
    if is_private:
        return "/api/method/{0}?key={1}&file_name={2}".format(
            "frappe_s3_attachment.controller.generate_file",
            quote(key),
            quote(file_name or ""),
        )
    return "{}/{}/{}".format(
        s3_upload.S3_CLIENT.meta.endpoint_url, s3_upload.BUCKET, quote(key)
    )


def _remove_local_file_after_commit(file_path):
    """Delete the local copy only after the DB changes are committed.

    os.remove is not transactional: deleting inside the transaction loses the
    file if the surrounding save later rolls back (the File row reverts to a
    local file_url whose file no longer exists).
    """

    def _remove():
        try:
            os.remove(file_path)
        except FileNotFoundError:
            logger.warning("[s3_attachment] local file already gone: %s", file_path)

    frappe.db.after_commit.add(_remove)


def _ignored_doctypes():
    """Doctypes whose attachments must stay local (default: Data Import)."""
    return frappe.local.conf.get("ignore_s3_upload_for_doctype") or ["Data Import"]


def _local_files():
    """File records still stored locally (not folders, not yet on S3)."""
    files = frappe.get_all(
        "File",
        filters={"is_folder": 0},
        fields=["name", "file_url", "attached_to_doctype"],
    )
    local = [
        f for f in files if f.get("file_url") and not s3_file_regex_match(f["file_url"])
    ]
    logger.debug(
        "[s3_attachment] %s of %s File records are still local", len(local), len(files)
    )
    return local


def get_s3_client():
    """
    Return a request-cached S3Operations instance.

    S3 settings do not change within a single request, so building a new boto3
    client per File event (insert/trash/download) is wasteful. Reuse one.
    """
    client = getattr(frappe.local, "_s3_operations", None)
    if client is None:
        logger.debug("[s3_attachment] initialising S3 client for request")
        client = S3Operations()
        frappe.local._s3_operations = client
    return client


@frappe.whitelist()
def file_upload_to_s3(doc, method):
    """
    check and upload files to s3. the path check and
    """
    path = doc.file_url
    # Folders and File records without an uploaded file have nothing to upload.
    if getattr(doc, "is_folder", 0) or not path:
        return
    # Skip files already on S3 (e.g. attachments copied from an amended-from
    # document). Their file_url is an S3 URL, not a local path, so trying to
    # re-upload would fail with FileNotFoundError.
    if s3_file_regex_match(path):
        return
    s3_upload = get_s3_client()
    site_path = frappe.utils.get_site_path()
    parent_doctype = doc.attached_to_doctype or "File"
    parent_name = doc.attached_to_name
    if parent_doctype not in _ignored_doctypes():
        if not doc.is_private:
            file_path = site_path + "/public" + path
        else:
            file_path = site_path + path
        # The local file must exist to be uploaded; if it is missing there is
        # nothing to do (mirrors upload_existing_files_s3).
        if not os.path.exists(file_path):
            return
        key = s3_upload.upload_files_to_s3_with_key(
            file_path, doc.file_name, doc.is_private, parent_doctype, parent_name
        )

        file_url = _make_file_url(s3_upload, key, doc.file_name, doc.is_private)
        # Frappe de-duplicates physical files by content, so several File
        # records can point at the same local file. Only remove it when this is
        # the last local reference, else the others would 404 — and only after
        # the transaction commits, else a rollback would lose the file.
        other_local_refs = frappe.db.count(
            "File", {"file_url": path, "name": ("!=", doc.name)}
        )
        if not other_local_refs:
            _remove_local_file_after_commit(file_path)

        frappe.db.sql(
            """UPDATE `tabFile` SET file_url=%s, folder=%s,
            old_parent=%s, content_hash=%s WHERE name=%s""",
            (file_url, "Home/Attachments", "Home/Attachments", key, doc.name),
        )

        doc.file_url = file_url

        if parent_doctype and frappe.get_meta(parent_doctype).get("image_field"):
            frappe.db.set_value(
                parent_doctype,
                parent_name,
                frappe.get_meta(parent_doctype).get("image_field"),
                file_url,
                update_modified=False,
            )
        # No explicit commit: these changes belong to the document-save
        # transaction so they roll back together if the save later fails.


@frappe.whitelist()
def generate_file(key=None, file_name=None):
    """
    Function to stream file from s3.
    """
    if not key:
        frappe.local.response["body"] = "Key not found."
        return
    # Enforce the same per-download permission Frappe applies to native private
    # files. Without this, any logged-in user could download any S3 object by
    # passing its key here.
    _check_file_access(key)
    logger.debug("[s3_attachment] generate_file signing key %s", key)
    s3_upload = get_s3_client()
    signed_url = s3_upload.get_url(key, file_name)
    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = signed_url
    return


def _key_url_variants(key):
    """file_url stores the key raw (legacy rows) or URL-encoded (new rows)."""
    return {key, quote(key)}


def _escape_like(value):
    """Escape SQL LIKE wildcards so a user-supplied key can't act as a pattern.

    Frappe parameterises LIKE values against injection but does not neutralise
    the % and _ wildcards; the app's own keys contain '_', so an unescaped key
    would silently match unrelated file_urls.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _file_url_references_key(file_url, key):
    """True only if file_url points at exactly this S3 key.

    A LIKE substring match is used only to narrow candidates; unlike a
    content_hash equality it does not guarantee the same object, so the key is
    verified precisely here before any access or delete decision is made.
    """
    if not file_url:
        return False
    parsed = urlparse(file_url)
    key_params = parse_qs(parsed.query).get("key")
    if key_params is not None:
        return key in key_params
    # Public files store the key as the URL-encoded tail of the path.
    return parsed.path.endswith("/" + quote(key)) or parsed.path.endswith("/" + key)


def _files_referencing_key(key):
    """All File records referencing this S3 key.

    Migrated rows carry the key in content_hash, but amended-document copies
    only carry it inside file_url (their content_hash is empty), so fall back
    to a file_url match when the hash lookup finds nothing.
    """
    fields = [
        "name",
        "is_private",
        "attached_to_doctype",
        "attached_to_name",
        "file_url",
    ]
    files = frappe.get_all("File", filters={"content_hash": key}, fields=fields)
    if files:
        return files
    logger.debug(
        "[s3_attachment] no content_hash match for key %s; trying file_url", key
    )
    seen, matched = set(), []
    for variant in _key_url_variants(key):
        for f in frappe.get_all(
            "File",
            filters={"file_url": ("like", "%{}%".format(_escape_like(variant)))},
            fields=fields,
        ):
            if f.name not in seen and _file_url_references_key(f.file_url, key):
                seen.add(f.name)
                matched.append(f)
    return matched


def _other_files_reference_key(key, exclude_name):
    """True if any other File record still references this S3 key.

    Used before deleting an S3 object: keep it while any copy (by content_hash
    or by exact key in file_url) still points at it.
    """
    if frappe.db.count("File", {"content_hash": key, "name": ("!=", exclude_name)}):
        return True
    for variant in _key_url_variants(key):
        for f in frappe.get_all(
            "File",
            filters={
                "file_url": ("like", "%{}%".format(_escape_like(variant))),
                "name": ("!=", exclude_name),
            },
            fields=["file_url"],
        ):
            if _file_url_references_key(f.file_url, key):
                return True
    return False


def _check_file_access(key):
    """
    Ensure the current user may download the S3 object for ``key``.

    Access follows the document the file is attached to, mirroring Frappe's
    native private-file check. Allows access if any File referencing this key is
    accessible (amended documents legitimately share one S3 object).
    """
    files = _files_referencing_key(key)
    if not files:
        logger.warning("[s3_attachment] generate_file: no File owns key %s", key)
        raise frappe.PermissionError(frappe._("File not found"))
    for f in files:
        if not f.is_private:
            return
        if f.attached_to_doctype and f.attached_to_name:
            if frappe.has_permission(f.attached_to_doctype, doc=f.attached_to_name):
                return
        elif frappe.has_permission("File", doc=f.name):
            return
    logger.warning("[s3_attachment] generate_file: access denied for key %s", key)
    raise frappe.PermissionError(frappe._("You are not permitted to access this file"))


def upload_existing_files_s3(name):
    """
    Upload one existing local File to S3.

    Returns True when the file was uploaded, False when there was nothing to
    upload (local copy missing).
    """
    doc = frappe.get_doc("File", name)
    path = doc.file_url
    site_path = frappe.utils.get_site_path()
    # Live data contains files not attached to any doctype; None would crash
    # key generation, so file them under "File" like file_upload_to_s3 does.
    parent_doctype = doc.attached_to_doctype or "File"
    parent_name = doc.attached_to_name
    file_name = doc.file_name or os.path.basename(path)
    if not doc.is_private:
        file_path = site_path + "/public" + path
    else:
        file_path = site_path + path

    if not os.path.exists(file_path):
        logger.warning(
            "[s3_attachment] migration: local copy missing for %s (%s)",
            name,
            file_path,
        )
        return False

    s3_upload = get_s3_client()
    key = s3_upload.upload_files_to_s3_with_key(
        file_path, file_name, doc.is_private, parent_doctype, parent_name
    )

    file_url = _make_file_url(s3_upload, key, file_name, doc.is_private)

    # Frappe de-duplicates physical files by content: several File records can
    # share one local file. Only drop the local copy when this record is the
    # last reference (mirrors file_upload_to_s3), and only after commit.
    other_local_refs = frappe.db.count(
        "File", {"file_url": path, "name": ("!=", doc.name)}
    )
    if not other_local_refs:
        _remove_local_file_after_commit(file_path)

    frappe.db.sql(
        """UPDATE `tabFile` SET file_url=%s, folder=%s,
        old_parent=%s, content_hash=%s WHERE name=%s""",
        (file_url, "Home/Attachments", "Home/Attachments", key, doc.name),
    )
    frappe.db.commit()
    return True


def s3_file_regex_match(file_url):
    """
    Match the public file regex match.
    """
    return re.match(
        r"^(https:|/api/method/frappe_s3_attachment.controller.generate_file)", file_url
    )


@frappe.whitelist()
def migrate_existing_files():
    """
    Queue migration of all existing local files to S3.

    Runs in a background job so the request does not time out on large sites,
    and is restricted to System Managers. A stable job id prevents duplicate
    concurrent migrations.
    """
    frappe.only_for("System Manager")
    from frappe.utils.background_jobs import is_job_enqueued

    if is_job_enqueued(MIGRATION_JOB_ID):
        logger.info("[s3_attachment] migration already running; not enqueuing again")
        return {"status": "already_running"}

    frappe.enqueue(
        "frappe_s3_attachment.controller._migrate_existing_files",
        queue="long",
        timeout=21600,
        job_id=MIGRATION_JOB_ID,
        user=frappe.session.user,
    )
    logger.info("[s3_attachment] migration of existing files enqueued")
    return {"status": "queued"}


@frappe.whitelist()
def get_migration_summary():
    """
    Counts shown in the migration confirm dialog: how many local files would
    be uploaded or skipped, and which records need special handling.
    """
    frappe.only_for("System Manager")
    logger.info("[s3_attachment] migration summary requested")
    from frappe.utils.background_jobs import is_job_enqueued

    ignored_doctypes = _ignored_doctypes()
    local_files = _local_files()
    skipped = sum(
        1 for f in local_files if f.get("attached_to_doctype") in ignored_doctypes
    )
    urls = [f["file_url"] for f in local_files]
    return {
        "total_local": len(local_files),
        "will_upload": len(local_files) - skipped,
        "skipped_ignored": skipped,
        "unattached": sum(1 for f in local_files if not f.get("attached_to_doctype")),
        "shared_local": len(urls) - len(set(urls)),
        "running": bool(is_job_enqueued(MIGRATION_JOB_ID)),
    }


def _migrate_existing_files(user=None):
    """Upload every local (non-S3) File to S3. Runs as a background job.

    Each file is processed independently: one bad record must not abort the
    rest of the migration. Progress and the final summary are published to
    the user who started the job.
    """
    ignored_doctypes = _ignored_doctypes()
    todo = _local_files()
    total = len(todo)
    migrated, skipped, failures = 0, 0, []
    for index, f in enumerate(todo, 1):
        _publish_migration_event(
            user,
            "s3_migration_progress",
            {"current": index, "total": total, "file_name": f["file_url"]},
        )
        if f.get("attached_to_doctype") in ignored_doctypes:
            skipped += 1
            continue
        try:
            if upload_existing_files_s3(f["name"]):
                migrated += 1
            else:
                skipped += 1
        except Exception as e:
            # Discard this file's partial changes; the job commits per file.
            frappe.db.rollback()
            logger.error(
                "[s3_attachment] migration failed for %s: %s",
                f["name"],
                e,
                exc_info=True,
            )
            failures.append(
                {"file": f["file_url"], "reason": str(e) or type(e).__name__}
            )
    summary = {
        "migrated": migrated,
        "skipped": skipped,
        "failed": len(failures),
        "failures": failures,
    }
    logger.info("[s3_attachment] migration complete: %s", summary)
    _publish_migration_event(user, "s3_migration_complete", summary)
    return summary


def _publish_migration_event(user, event, message):
    """Realtime updates are best-effort; a publish failure must not kill the job."""
    if not user:
        return
    try:
        frappe.publish_realtime(event, message, user=user)
    except Exception:
        logger.warning("[s3_attachment] realtime publish failed for %s", event)


def delete_from_cloud(doc, method):
    """Delete file from s3"""
    # Only S3-stored files carry an S3 key in content_hash. Local files have a
    # content hash that is not an S3 key, so skip them to avoid issuing a
    # delete with a bogus key (which can raise and block trashing the doc).
    if not doc.content_hash or not (doc.file_url and s3_file_regex_match(doc.file_url)):
        logger.debug(
            "[s3_attachment] delete_from_cloud skipped for non-S3 file: %s", doc.name
        )
        return
    # Amended/copied documents can share the same S3 object. Only delete it when
    # no other File still references the key, otherwise those documents would
    # lose their file.
    if _other_files_reference_key(doc.content_hash, doc.name):
        logger.debug(
            "[s3_attachment] delete_from_cloud skipped; other File(s) share key %s",
            doc.content_hash,
        )
        return
    s3 = get_s3_client()
    s3.delete_from_s3(doc.content_hash)


@frappe.whitelist()
def ping():
    """
    Test function to check if api function work.
    """
    return "pong"


def update_has_attachment_flag(doc, method):
    """Update custom_has_attachment on parent document when files are added or removed."""
    if not doc.attached_to_doctype or not doc.attached_to_name:
        return
    # Guard against attachments pointing at a doctype that no longer exists;
    # frappe.get_meta would raise for an unknown doctype.
    if not frappe.db.exists("DocType", doc.attached_to_doctype):
        return
    if not frappe.get_meta(doc.attached_to_doctype).has_field("custom_has_attachment"):
        return
    if not frappe.db.exists(doc.attached_to_doctype, doc.attached_to_name):
        return

    if method == "after_insert":
        frappe.db.set_value(
            doc.attached_to_doctype,
            doc.attached_to_name,
            "custom_has_attachment",
            1,
            update_modified=False,
        )
    elif method == "on_trash":
        remaining = frappe.db.count(
            "File",
            {
                "attached_to_doctype": doc.attached_to_doctype,
                "attached_to_name": doc.attached_to_name,
            },
        )
        # on_trash fires before deletion, so the current file is still counted
        if remaining <= 1:
            frappe.db.set_value(
                doc.attached_to_doctype,
                doc.attached_to_name,
                "custom_has_attachment",
                0,
                update_modified=False,
            )
