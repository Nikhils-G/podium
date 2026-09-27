"""One version number: pyproject.toml and the package agree (it shows in the footer, the API
document and every export)."""

import tomllib
from pathlib import Path

import podium


def test_package_and_pyproject_agree():
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    assert tomllib.loads(pyproject.read_text())["project"]["version"] == podium.__version__
