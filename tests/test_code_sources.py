import subprocess
from pathlib import Path

import pytest

from agents.explore_agent import run


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


@pytest.fixture
def source_repo(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.invalid")
    (source / "version.txt").write_text("pinned")
    git(source, "add", ".")
    git(source, "commit", "-m", "Initial version")
    pinned = git(source, "rev-parse", "HEAD")
    git(source, "tag", "v1")
    (source / "version.txt").write_text("latest")
    git(source, "commit", "-am", "Next version")
    monkeypatch.setattr(run, "CODE_SOURCES_DIR", tmp_path / "cache")
    return source, pinned


@pytest.mark.parametrize("ref_kind", ["commit", "tag", "branch"])
def test_code_source_checks_out_requested_revision(source_repo, ref_kind):
    source, pinned = source_repo
    ref = {"commit": pinned, "tag": "v1", "branch": "main"}[ref_kind]
    config = {"explore_agent": {"code_sources": {
        "nexau": {"type": "git", "url": source.as_uri(), "ref": ref},
    }}}

    checkout = run.prepare_code_sources(config)["nexau"]

    expected = git(source, "rev-parse", "main") if ref_kind == "branch" else pinned
    assert git(checkout, "rev-parse", "HEAD") == expected
    assert (checkout / "version.txt").read_text() == ("latest" if ref_kind == "branch" else "pinned")
    assert run.prepare_code_sources(config)["nexau"] == checkout


def test_failed_checkout_does_not_leave_a_cache(source_repo):
    source, _ = source_repo
    config = {"explore_agent": {"code_sources": {
        "nexau": {"type": "git", "url": source.as_uri(), "ref": "missing"},
    }}}

    with pytest.raises(subprocess.CalledProcessError):
        run.prepare_code_sources(config)

    assert list(run.CODE_SOURCES_DIR.iterdir()) == []
