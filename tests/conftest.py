"""Keep test-generated databases, JSONL and output folders out of project var/."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_relative_test_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy tests use relative ``var/test_*`` paths; make them temporary."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "var").mkdir()
