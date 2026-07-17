"""Guard the version-bump discipline: the package version has exactly one source.

Regression test for the 0.1.0/0.2.0 skew where ``pyproject.toml`` was bumped but
``agent_connect.__version__`` was not. The fix single-sources the version:
``pyproject`` declares it ``dynamic`` and reads it from ``agent_connect.__version__``.
These asserts fail loudly if anyone re-hardcodes a static ``version =`` in
``[project]`` (which would let the two drift again).
"""
import pathlib
import re

import pytest

import agent_connect

PYPROJECT = pathlib.Path(__file__).parent / "pyproject.toml"
# PEP 440 (common subset): 0.2.0, 1.2.3rc1, 0.2.0.post1 ...
_PEP440 = re.compile(r"^\d+(\.\d+)*([abc]|rc)?\d*(\.post\d+)?(\.dev\d+)?$")


def test_package_version_is_pep440():
    assert _PEP440.match(agent_connect.__version__), agent_connect.__version__


def test_pyproject_version_is_single_sourced():
    try:
        import tomllib  # py3.11+
    except ModuleNotFoundError:  # pragma: no cover - older interpreters
        tomllib = pytest.importorskip("tomli")
    data = tomllib.loads(PYPROJECT.read_text())
    project = data["project"]
    # No static version — it must be declared dynamic so it can't drift.
    assert "version" not in project, "pyproject [project] must not hard-code version"
    assert "version" in project.get("dynamic", []), "version must be dynamic"
    attr = data["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    assert attr == "agent_connect.__version__", attr
