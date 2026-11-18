"""
purpose: Validate the exact code-only source allowlist and optionally archive the clean committed tree.
usage:   uv run python scripts/package_source.py [--output <zip outside this repository>]
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

LOG = logging.getLogger("package_source")
ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / "release-files.txt"
FORBIDDEN_SUFFIXES = {
    ".abf",
    ".csv",
    ".db",
    ".gif",
    ".html",
    ".jpeg",
    ".jpg",
    ".mht",
    ".onnx",
    ".parquet",
    ".pbir",
    ".pbip",
    ".pbism",
    ".pbix",
    ".pdf",
    ".pbit",
    ".png",
    ".rdl",
    ".sqlite",
    ".svg",
    ".tmdl",
    ".tsv",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
}
SENSITIVE_CONTENT = (
    re.compile(rb"[A-Za-z]:\\Users\\", re.IGNORECASE),
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"\bgh[psour]_[A-Za-z0-9]{20,}"),
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def _git(root: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


def read_allowlist(path: Path) -> list[str]:
    """Read unique, repository-relative POSIX paths from the exact-file manifest."""
    entries = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(entries) != len(set(entries)):
        raise ValueError(f"allowlist contains duplicate paths: {path}")
    for entry in entries:
        candidate = PurePosixPath(entry)
        if candidate.is_absolute() or ".." in candidate.parts or "\\" in entry:
            raise ValueError(f"allowlist path must be a repository-relative POSIX path: {entry}")
    return entries


def tracked_files(root: Path) -> tuple[list[str], list[str]]:
    """Return staged Git paths and reject links/submodules."""
    records = _git(root, "ls-files", "--stage", "-z").split(b"\0")
    paths: list[str] = []
    unsupported_modes: list[str] = []
    for record in filter(None, records):
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise ValueError("could not parse `git ls-files --stage` output")
        mode = metadata.split(maxsplit=1)[0].decode("ascii")
        path = raw_path.decode("utf-8")
        paths.append(path)
        if mode not in {"100644", "100755"}:
            unsupported_modes.append(f"{mode} {path}")
    return paths, unsupported_modes


def _validate_worktree(root: Path) -> None:
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    if status.strip():
        raise ValueError("source archives require a clean worktree and index; commit or remove local changes first")


def validate_tree(root: Path) -> list[str]:
    """Require a clean checkout whose tracked files exactly match the reviewed allowlist."""
    allowed = read_allowlist(root / ALLOWLIST.name)
    tracked, unsupported_modes = tracked_files(root)
    if unsupported_modes:
        raise ValueError(f"symlinks and submodules are not allowed: {', '.join(unsupported_modes)}")

    extras = sorted(set(tracked) - set(allowed))
    missing = sorted(set(allowed) - set(tracked))
    if extras or missing:
        detail = []
        if extras:
            detail.append(f"unlisted tracked files: {', '.join(extras)}")
        if missing:
            detail.append(f"allowlisted files not tracked: {', '.join(missing)}")
        raise ValueError("; ".join(detail))

    for relative in allowed:
        path = root.joinpath(*PurePosixPath(relative).parts)
        if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            raise ValueError(f"report, data or generated artifact type is not allowed: {relative}")
        if not path.is_file():
            raise ValueError(f"allowlisted path is not a regular file: {relative}")
        content = path.read_bytes()
        if b"\0" in content:
            raise ValueError(f"binary content is not allowed in the source archive: {relative}")
        if any(pattern.search(content) for pattern in SENSITIVE_CONTENT):
            raise ValueError(f"possible credential or local user path in allowlisted content: {relative}")

    _validate_worktree(root)
    return allowed


def build_archive(root: Path, allowed: list[str], output: Path) -> Path:
    """Create and verify an archive from HEAD, with the destination outside the checkout."""
    repository = root.resolve(strict=True)
    target = output.resolve()
    if target == repository or target.is_relative_to(repository):
        raise ValueError(f"archive destination must be outside the repository: {repository}")
    if target.exists():
        raise FileExistsError(f"refusing to overwrite an existing archive: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        completed = subprocess.run(
            ["git", "archive", "--format=zip", f"--output={temporary}", "HEAD", "--", *allowed],
            cwd=repository,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"git archive failed: {detail}")
        with zipfile.ZipFile(temporary) as archive:
            actual = sorted(archive.namelist())
            expected = sorted(allowed)
            if actual != expected:
                raise ValueError("archive entries do not match the reviewed allowlist")
            corrupt = archive.testzip()
            if corrupt is not None:
                raise ValueError(f"archive CRC check failed for {corrupt}")
        os.rename(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, help="write a source ZIP outside the repository")
    args = parser.parse_args()

    try:
        files = validate_tree(ROOT)
        if args.output is None:
            LOG.info("Code-only allowlist verified: %d committed files", len(files))
        else:
            archive = build_archive(ROOT, files, args.output)
            LOG.info("Created code-only source archive: %s (%d files)", archive, len(files))
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        LOG.error("%s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
