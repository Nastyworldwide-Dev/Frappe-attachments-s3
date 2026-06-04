from __future__ import unicode_literals

import datetime
import logging
import os
import random
import re
import string

import boto3

from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

import frappe


import magic


logger = logging.getLogger(__name__)


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
        if self.s3_settings_doc.aws_key and self.s3_settings_doc.aws_secret:
            self.S3_CLIENT = boto3.client(
                "s3",
                aws_access_key_id=self.s3_settings_doc.aws_key,
                aws_secret_access_key=self.s3_settings_doc.aws_secret,
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
                "Metadata": {"ContentType": content_type, "file_name": file_name},
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
            logger.error("[s3_attachment] upload failed for key %s: %s", key, e)
            frappe.throw(frappe._("File Upload Failed. Please try again."))
        return key

    def delete_from_s3(self, key):
        """Delete file from s3"""
        if self.s3_settings_doc.delete_file_from_cloud:
            try:
                self.S3_CLIENT.delete_object(
                    Bucket=self.s3_settings_doc.bucket_name, Key=key
                )
            except ClientError:
                frappe.throw(frappe._("Access denied: Could not delete file"))

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
            params["ResponseContentDisposition"] = "filename={}".format(file_name)

        url = self.S3_CLIENT.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=self.signed_url_expiry_time,
        )

        return url


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
    ignore_s3_upload_for_doctype = frappe.local.conf.get(
        "ignore_s3_upload_for_doctype"
    ) or ["Data Import"]
    if parent_doctype not in ignore_s3_upload_for_doctype:
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

        if doc.is_private:
            method = "frappe_s3_attachment.controller.generate_file"
            file_url = """/api/method/{0}?key={1}&file_name={2}""".format(
                method, key, doc.file_name
            )
        else:
            file_url = "{}/{}/{}".format(
                s3_upload.S3_CLIENT.meta.endpoint_url, s3_upload.BUCKET, key
            )
        # Frappe de-duplicates physical files by content, so several File
        # records can point at the same local file. Only remove it when this is
        # the last local reference, else the others would 404.
        other_local_refs = frappe.db.count(
            "File", {"file_url": path, "name": ("!=", doc.name)}
        )
        if not other_local_refs:
            os.remove(file_path)

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


def _check_file_access(key):
    """
    Ensure the current user may download the S3 object for ``key``.

    Access follows the document the file is attached to, mirroring Frappe's
    native private-file check. Allows access if any File referencing this key is
    accessible (amended documents legitimately share one S3 object).
    """
    files = frappe.get_all(
        "File",
        filters={"content_hash": key},
        fields=["name", "is_private", "attached_to_doctype", "attached_to_name"],
    )
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
    Function to upload all existing files.
    """
    file_doc_name = frappe.db.get_value("File", {"name": name})
    if file_doc_name:
        doc = frappe.get_doc("File", name)
        s3_upload = get_s3_client()
        path = doc.file_url
        site_path = frappe.utils.get_site_path()
        parent_doctype = doc.attached_to_doctype
        parent_name = doc.attached_to_name
        if not doc.is_private:
            file_path = site_path + "/public" + path
        else:
            file_path = site_path + path

        # File exists?
        if not os.path.exists(file_path):
            return

        key = s3_upload.upload_files_to_s3_with_key(
            file_path, doc.file_name, doc.is_private, parent_doctype, parent_name
        )

        if doc.is_private:
            method = "frappe_s3_attachment.controller.generate_file"
            file_url = """/api/method/{0}?key={1}&file_name={2}""".format(
                method, key, doc.file_name
            )
        else:
            file_url = "{}/{}/{}".format(
                s3_upload.S3_CLIENT.meta.endpoint_url, s3_upload.BUCKET, key
            )

        # Remove file from local.
        os.remove(file_path)

        frappe.db.sql(
            """UPDATE `tabFile` SET file_url=%s, folder=%s,
            old_parent=%s, content_hash=%s WHERE name=%s""",
            (file_url, "Home/Attachments", "Home/Attachments", key, doc.name),
        )
        frappe.db.commit()


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
    and is restricted to System Managers.
    """
    frappe.only_for("System Manager")
    frappe.enqueue(
        "frappe_s3_attachment.controller._migrate_existing_files",
        queue="long",
        timeout=21600,
    )
    logger.info("[s3_attachment] migration of existing files enqueued")
    return True


def _migrate_existing_files():
    """Upload every local (non-S3) File to S3. Runs as a background job."""
    files_list = frappe.get_all("File", fields=["name", "file_url"])
    for file in files_list:
        if file["file_url"] and not s3_file_regex_match(file["file_url"]):
            upload_existing_files_s3(file["name"])
    logger.info(
        "[s3_attachment] migration complete: %s File records scanned",
        len(files_list),
    )


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
    shared = frappe.db.count(
        "File", {"content_hash": doc.content_hash, "name": ("!=", doc.name)}
    )
    if shared:
        logger.debug(
            "[s3_attachment] delete_from_cloud skipped; %s File(s) share key %s",
            shared,
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
