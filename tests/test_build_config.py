"""Tests for pyproject.toml build-system configuration."""

from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib


PYPROJECT_PATH = Path(__file__).parent.parent / "pyproject.toml"


def _load_pyproject():
    with open(PYPROJECT_PATH, "rb") as f:
        return tomllib.load(f)


def _setuptools_min_version(build_system_requires):
    for requirement_str in build_system_requires:
        requirement = Requirement(requirement_str)
        if requirement.name.lower() != "setuptools":
            continue
        minimum = Version("0")
        for specifier in requirement.specifier:
            if specifier.operator in (">=", ">", "=="):
                candidate = Version(specifier.version)
                minimum = max(minimum, candidate)
        return minimum
    return None


class TestBuildSystemConfig:
    """Tests for the [build-system] table in pyproject.toml."""

    def test_pyproject_exists(self):
        assert PYPROJECT_PATH.exists()

    def test_build_system_requires_setuptools_ge_83(self):
        """The PYSEC-2026-3447 fix requires setuptools>=83.0.0."""
        data = _load_pyproject()
        requires = data["build-system"]["requires"]
        assert isinstance(requires, list)

        minimum = _setuptools_min_version(requires)
        assert minimum is not None, "build-system.requires has no setuptools entry"
        assert minimum >= Version("83.0.0"), (
            f"setuptools lower bound must be >= 83.0.0, got {minimum}"
        )

    def test_setuptools_min_version_gt_and_eq_operators(self):
        minimum = _setuptools_min_version(["setuptools>80.0,==83.0.0"])
        assert minimum == Version("83.0.0")

    def test_setuptools_min_version_no_setuptools_entry(self):
        assert _setuptools_min_version(["wheel"]) is None

    def test_build_backend_is_setuptools(self):
        data = _load_pyproject()
        assert data["build-system"]["build-backend"] == "setuptools.build_meta"
