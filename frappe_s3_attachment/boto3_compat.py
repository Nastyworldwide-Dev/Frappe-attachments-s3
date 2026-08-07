"""Import boto3 while surviving broken pyOpenSSL/cryptography pairs.

cryptography >= 43 removed the ``GEN_*`` cffi bindings that pyOpenSSL
< 24.2.1 still references at import time, so a bench that upgrades
cryptography while keeping a stale pyOpenSSL raises ``AttributeError:
module 'lib' has no attribute 'GEN_EMAIL'`` deep inside ``import boto3``
(botocore -> urllib3.contrib.pyopenssl -> OpenSSL). The pyopenssl TLS
shim is optional and unnecessary on modern Python, so when that happens,
block the shim and retry — botocore treats the resulting ImportError as
"shim unavailable" and falls back to stdlib ssl.
"""

import importlib
import logging
import sys

logger = logging.getLogger(__name__)

_import_module = importlib.import_module


def import_boto3():
    try:
        return _import_module("boto3")
    except AttributeError as e:
        logger.warning(
            "[boto3_compat] import boto3 failed (%s); retrying with the "
            "urllib3 pyopenssl shim blocked",
            e,
        )
        sys.modules["urllib3.contrib.pyopenssl"] = None
        return _import_module("boto3")


boto3 = import_boto3()
