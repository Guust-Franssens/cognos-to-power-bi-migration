"""Tests for the explicit source-archive allowlist."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from package_source import build_archive, read_allowlist, validate_tree  # noqa: E402


def _init_repo(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Migration Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "migration-test@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "add", "--all"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "test fixture"], cwd=root, check=True)


def test_allowlist_is_exact_and_archive_contains_only_committed_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    files = {
        "README.md": "# Synthetic test repository\n",
        "release-files.txt": "# reviewed\nREADME.md\nrelease-files.txt\n",
    }
    _init_repo(root, files)

    allowed = validate_tree(root)
    archive_path = tmp_path / "source.zip"
    build_archive(root, allowed, archive_path)

    with zipfile.ZipFile(archive_path) as archive:
        assert sorted(archive.namelist()) == sorted(files)
        archived_readme = archive.read("README.md").decode(encoding="utf-8").replace("\r\n", "\n")
        assert archived_readme == files["README.md"]
    assert not list(root.glob("*.zip"))


def test_refuses_unlisted_tracked_report_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(
        root,
        {
            "README.md": "safe\n",
            "release-files.txt": "# reviewed\nREADME.md\nrelease-files.txt\n",
            "private.xml": "<report />\n",
        },
    )

    with pytest.raises(ValueError, match="unlisted tracked files"):
        validate_tree(root)


def test_refuses_forbidden_file_even_if_added_to_allowlist(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(
        root,
        {
            "README.md": "safe\n",
            "private.xml": "<report />\n",
            "release-files.txt": "# reviewed\nREADME.md\nprivate.xml\nrelease-files.txt\n",
        },
    )

    with pytest.raises(ValueError, match="artifact type is not allowed"):
        validate_tree(root)


@pytest.mark.parametrize(
    "content",
    [
        "local path C:" + chr(92) + "Users" + chr(92) + "someone\n",
        "github_pat_" + "abcdefghijklmnopqrstuvwxyz123456\n",
        "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5 + "\n",
    ],
)
def test_refuses_high_confidence_sensitive_content(tmp_path: Path, content: str) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(
        root,
        {
            "README.md": content,
            "release-files.txt": "# reviewed\nREADME.md\nrelease-files.txt\n",
        },
    )

    with pytest.raises(ValueError, match="possible credential or local user path"):
        validate_tree(root)


def test_refuses_modified_worktree(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(
        root,
        {
            "README.md": "safe\n",
            "release-files.txt": "# reviewed\nREADME.md\nrelease-files.txt\n",
        },
    )
    (root / "README.md").write_text("unreviewed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="clean worktree and index"):
        validate_tree(root)


def test_refuses_archive_overwrite_or_destination_inside_repository(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(
        root,
        {
            "README.md": "safe\n",
            "release-files.txt": "# reviewed\nREADME.md\nrelease-files.txt\n",
        },
    )
    allowed = read_allowlist(root / "release-files.txt")

    with pytest.raises(ValueError, match="outside the repository"):
        build_archive(root, allowed, root / "source.zip")

    existing = tmp_path / "source.zip"
    existing.write_bytes(b"preserve")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        build_archive(root, allowed, existing)
    assert existing.read_bytes() == b"preserve"


def test_rejects_duplicate_and_escaping_allowlist_paths(tmp_path: Path) -> None:
    allowlist = tmp_path / "release-files.txt"
    allowlist.write_text("README.md\nREADME.md\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate paths"):
        read_allowlist(allowlist)

    allowlist.write_text("../private.xml\n", encoding="utf-8")
    with pytest.raises(ValueError, match="repository-relative"):
        read_allowlist(allowlist)
