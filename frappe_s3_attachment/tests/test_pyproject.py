"""Guard the packaging metadata Frappe Cloud and bench validate before install.

Regression for: "Could not find a compatible Frappe version in pyproject.toml
... Please ensure '[tool.bench.frappe-dependencies]' is defined."
"""

import pathlib
import tomllib

import pytest

sv = pytest.importorskip("semantic_version")

PYPROJECT = pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"


def _load():
    return tomllib.loads(PYPROJECT.read_text())


def test_frappe_dependency_declared_and_bounded():
    spec_str = _load()["tool"]["bench"]["frappe-dependencies"]["frappe"]
    # Same normalisation press applies before parsing (press/press/doctype/app/app.py).
    spec = sv.NpmSpec(spec_str.replace(" ", "").replace(",", " "))
    assert spec.match(sv.Version("16.0.0")), "must match a version-16 bench"
    assert not spec.match(sv.Version("14.0.0")) and not spec.match(sv.Version("17.0.0")), (
        "range must be bounded on both ends"
    )


def test_frappe_not_listed_as_pip_dependency():
    # press audit: frappe/erpnext must only appear under [tool.bench.*], never in
    # [project].dependencies, or pip tries to install them from PyPI.
    deps = " ".join(_load()["project"]["dependencies"]).lower()
    assert "frappe" not in deps and "erpnext" not in deps


def test_project_version_matches_package_version():
    from frappe_s3_attachment import __version__

    assert _load()["project"]["version"] == __version__
