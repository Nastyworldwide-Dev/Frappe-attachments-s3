"""Regression tests for surviving broken pyOpenSSL/cryptography pairs.

A bench whose env holds cryptography >= 43 alongside pyOpenSSL < 24.2.1
raises ``AttributeError: module 'lib' has no attribute 'GEN_EMAIL'`` deep
inside ``import boto3`` (botocore -> urllib3.contrib.pyopenssl -> OpenSSL),
which took down every File-controller request on the live site. The compat
module must block the optional pyopenssl TLS shim and retry.
"""

import importlib
import sys

import pytest

from frappe_s3_attachment import boto3_compat

SHIM = "urllib3.contrib.pyopenssl"


@pytest.fixture
def restore_shim_module():
    original = sys.modules.get(SHIM, "absent")
    yield
    if original == "absent":
        sys.modules.pop(SHIM, None)
    else:
        sys.modules[SHIM] = original


def test_retries_with_pyopenssl_shim_blocked(monkeypatch, restore_shim_module):
    calls = []
    real_import = importlib.import_module

    def fake_import(name):
        calls.append(name)
        if len(calls) == 1:
            raise AttributeError("module 'lib' has no attribute 'GEN_EMAIL'")
        return real_import(name)

    monkeypatch.setattr(boto3_compat, "_import_module", fake_import)

    module = boto3_compat.import_boto3()

    assert module is sys.modules["boto3"]
    assert calls == ["boto3", "boto3"]
    # None in sys.modules makes any later import of the shim raise
    # ImportError, which botocore catches and falls back to stdlib ssl.
    assert sys.modules[SHIM] is None


def test_healthy_env_imports_without_touching_shim(restore_shim_module):
    sys.modules.pop(SHIM, None)

    module = boto3_compat.import_boto3()

    assert module.__name__ == "boto3"
    # Absent is fine (urllib3 v2 has no shim); poisoned-to-None is not.
    assert sys.modules.get(SHIM, "absent") is not None


def test_controller_uses_guarded_boto3():
    from frappe_s3_attachment import controller

    assert controller.boto3 is boto3_compat.boto3
