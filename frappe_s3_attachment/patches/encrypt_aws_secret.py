import logging

import frappe

logger = logging.getLogger(__name__)


def execute():
    """Move the plaintext aws_secret into encrypted password storage.

    aws_secret was a plain Data field: readable in cleartext through the API
    and archived in Version history. Encrypt the current value and purge the
    plaintext copies.
    """
    frappe.reload_doc("frappe_s3_attachment", "doctype", "s3_file_attachment")
    # tabSingles has no `modified` column, so default ordering breaks the query.
    plaintext = frappe.db.get_value(
        "Singles",
        {"doctype": "S3 File Attachment", "field": "aws_secret"},
        "value",
        order_by=None,
    )
    if plaintext and plaintext != "*":
        from frappe.utils.password import set_encrypted_password

        set_encrypted_password(
            "S3 File Attachment", "S3 File Attachment", plaintext, "aws_secret"
        )
        frappe.db.set_single_value("S3 File Attachment", "aws_secret", "*")
        logger.info("[s3_attachment] aws_secret moved to encrypted storage")
    # Version history of this doctype recorded the secret in plaintext.
    frappe.db.delete("Version", {"ref_doctype": "S3 File Attachment"})
    logger.info("[s3_attachment] purged Version history for S3 File Attachment")
