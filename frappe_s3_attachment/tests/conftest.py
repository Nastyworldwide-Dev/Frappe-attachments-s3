"""Allow the mock-based suite to run outside a bench.

The tests patch ``frappe_s3_attachment.controller.frappe`` per test, but the
modules under test still ``import frappe`` at import time, which only works
inside a bench. Stub the frappe module tree here so the suite is runnable
(and CI-able) from a plain checkout. Inside a bench the real frappe is
already importable and these stubs are skipped.
"""

import sys
from unittest.mock import MagicMock

if "frappe" not in sys.modules:
    try:
        import frappe  # noqa: F401 - real bench environment, nothing to stub
    except ImportError:
        frappe_stub = MagicMock()
        # Decorators must return the function unchanged, not a MagicMock,
        # or every decorated controller function becomes uncallable.
        frappe_stub.whitelist = lambda *a, **k: lambda fn: fn
        sys.modules["frappe"] = frappe_stub
        sys.modules["frappe.model"] = MagicMock()
        sys.modules["frappe.model.document"] = MagicMock()
        sys.modules["frappe.utils"] = MagicMock()
        sys.modules["frappe.utils.background_jobs"] = MagicMock()
        sys.modules["frappe.utils.password"] = MagicMock()
        sys.modules["frappe.core"] = MagicMock()
        sys.modules["frappe.core.doctype"] = MagicMock()
        sys.modules["frappe.core.doctype.file"] = MagicMock()

        file_module = MagicMock()

        class File(object):
            """Minimal stand-in for frappe's File document class."""

            def get_content(self):
                raise NotImplementedError

        file_module.File = File
        sys.modules["frappe.core.doctype.file.file"] = file_module
