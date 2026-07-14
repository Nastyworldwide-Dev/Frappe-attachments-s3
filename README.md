<a href="https://zerodha.tech"><img src="https://zerodha.tech/static/images/github-badge.svg" align="right" /></a>

## Frappe S3 Attachment

Frappe app to make file upload automatically upload and read from s3.

#### Features.

1. Upload both public and private files to s3.
2. Stream files from S3, when file is viewed everytime.
3. Lets you add S3 credentials
    (aws key, aws secret, bucket name, folder name) through ui and migrate existing
    files.
4. Deletes from s3 whenever a file is deleted in ui.
5. Files are uploaded categorically in the format.
    {s3_folder_path}/{year}/{month}/{day}/{doctype}/{file_hash}

#### Installation.

1. bench get-app https://github.com/zerodhatech/Frappe-attachments-s3.git
2. bench install-app frappe_s3_attachment

#### Configuration Setup.

1. Open single doctype "s3 File Attachment"
2. Enter (Bucket Name, AWS key, AWS secret, S3 bucket Region name, Folder Name)
    Folder Name- folder name is the default folder path in s3.
3. Migrate existing files lets all the existing files in private and public folders
    to be migrated to s3.
4. Delete From Cloud when selected deletes the file form s3 bucket whenever a file
    is deleted from ui. By default the Delete from cloud will be unchecked.

#### Migration behavior.

- "Migrate Existing Files" runs as a background job with live progress and a
    completion summary (migrated / skipped / failed with reasons). Only one
    migration job can run at a time.
- Files attached to ignored doctypes (see `ignore_s3_upload_for_doctype`
    below) are skipped. Local copies shared by several File records are kept
    until the last reference is migrated, and are only deleted after the
    database changes commit.
- The AWS Secret is stored encrypted (Password field). On upgrade, a patch
    encrypts any existing plaintext value and purges the doctype's Version
    history.

#### site_config.json flags.

- `s3_use_acl` — set to 1 to upload public files with a `public-read` ACL
    (for buckets that still use ACLs). Off by default.
- `ignore_s3_upload_for_doctype` — list of doctypes whose attachments stay
    local. Defaults to `["Data Import"]`.

#### License

MIT
