"""
purpose: Create external numbered migration runs with protected baselines and editable working copies.
usage:   uv run python scripts/create_migration_workspace.py --source-project <dir> --run-parent <dir> --run-slug <slug> --unit-key <key>
         uv run python scripts/create_migration_workspace.py --source-project <dir> --run-root <run dir> --unit-key <key>
         uv run python scripts/create_migration_workspace.py --verify-unit <unit dir>
         uv run python scripts/create_migration_workspace.py --verify-run <run dir>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

LOG = logging.getLogger("create_migration_workspace")
LOCAL_STATE_NAMES = {".pbi", "cache.abf", "localsettings.json"}
PROJECT_SUFFIXES = {".json", ".pbip", ".pbir", ".pbism", ".tmdl"}
PROJECT_METADATA_FILES = {
    "migration_metadata.json",
    "migration_review.json",
    "migration_review.md",
}
REPORT_RESOURCE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".json", ".png", ".svg"}
IMAGE_SIGNATURES = {
    ".gif": (b"GIF87a", b"GIF89a"),
    ".jpeg": (b"\xff\xd8\xff",),
    ".jpg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
}
PLATFORM_ITEM_TYPES = {".report": "Report", ".semanticmodel": "SemanticModel"}
RUN_SCHEMA = "migration-run.v1"
RUN_NAME_PATTERN = re.compile(r"^(?P<number>\d{3,})-(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)$")
KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")

BRIEF_TEMPLATE = """# Migration brief

> The human owns this brief. Agree on scope and evidence before agent work.

## Report and requested outcome

- Report name:
- Business purpose:
- Requested Power BI deliverable:

## Source inputs

Keep the Cognos specification, screenshots, data and oracle outside this workspace and outside Git.
Record local paths only when this workspace itself is protected and access is appropriate.

- Cognos specification path:
- Cognos source environment and version:
- Source screenshots or render paths:
- Source model, module or package paths:

## Data and numeric oracle

- Data estate: customer, synthetic, or other (identify):
- Approved source for expected values:
- Oracle path or query method:
- Prompts and filter state used:
- Known data limitations:

No oracle means numeric fidelity is unverified. Do not infer expected values from layout screenshots.

## Fidelity scope and acceptance

- Pages, visuals and calculations in scope:
- Required layout and labels:
- Numeric grain, filters and tolerances:
- Required output formats:
- Out of scope:

## Security and operations

- Allowed processing location and data boundary:
- AI provider: none unless separately approved:
- Data source and canary tables for refresh:
- Available Power BI Desktop and validation tools:
- Limitations accepted by the user:
"""


def repository_root() -> Path:
    """Return the checkout root, or this source tree when run from an archive."""
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        return Path(completed.stdout.strip()).resolve()
    return Path(__file__).resolve().parents[1]


def _is_reparse_point(path: Path) -> bool:
    metadata = path.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _reject_reparse_points(source: Path, label: str = "source project") -> None:
    for root, directories, files in os.walk(source, followlinks=False):
        current = Path(root)
        for name in [*directories, *files]:
            path = current / name
            if _is_reparse_point(path):
                raise ValueError(f"{label} contains a link or reparse point: {path}")


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name.casefold() in LOCAL_STATE_NAMES}


def _is_local_state(path: Path) -> bool:
    return any(part.casefold() in LOCAL_STATE_NAMES for part in path.parts)


def _in_project_definition(path: Path) -> bool:
    parts = path.parts
    return any(
        part.casefold().endswith((".report", ".semanticmodel"))
        and index + 1 < len(parts)
        and parts[index + 1] == "definition"
        for index, part in enumerate(parts[:-1])
    )


def _is_item_file(path: Path) -> bool:
    """Names of documented PBIP item files outside definition/ that the converter, Desktop and Fabric Git write.

    A name match is not enough: _check_item_content also requires the content PBIP writes there.
    """
    parts = [part.casefold() for part in path.parts]
    if len(parts) < 2 or not parts[0].endswith((".report", ".semanticmodel")):
        return False
    if parts[1:] == [".platform"]:
        return True
    if parts[0].endswith(".semanticmodel"):
        return parts[1:] == ["diagramlayout.json"]
    suffix = path.suffix.casefold()
    if len(parts) == 4 and parts[1:3] == ["staticresources", "registeredresources"]:
        return suffix in REPORT_RESOURCE_SUFFIXES
    return len(parts) == 5 and parts[1:4] == ["staticresources", "sharedresources", "basethemes"] and suffix == ".json"


def _json_object(path: Path, relative: Path) -> dict:
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"PBIP item file is not UTF-8 JSON: {relative}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"PBIP item file is not a JSON object: {relative}")
    return data


def _registered_resource_paths(source: Path, report_folder: str) -> set[str]:
    """Item paths of the RegisteredResources package in the report's PBIR definition/report.json."""
    report_json = source / report_folder / "definition" / "report.json"
    try:
        report = json.loads(report_json.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return set()
    packages = report.get("resourcePackages") if isinstance(report, dict) else None
    paths: set[str] = set()
    for package in packages if isinstance(packages, list) else []:
        if not isinstance(package, dict) or package.get("type") != "RegisteredResources":
            continue
        for item in package.get("items") if isinstance(package.get("items"), list) else []:
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                paths.add(item["path"].casefold())
    return paths


def _check_registered_resource(source: Path, relative: Path) -> None:
    path = source / relative
    suffix = relative.suffix.casefold()
    data = path.read_bytes()
    if suffix in IMAGE_SIGNATURES:
        if not data.startswith(IMAGE_SIGNATURES[suffix]):
            raise ValueError(f"registered resource is not a {suffix[1:].upper()} image: {relative}")
    elif suffix == ".svg":
        try:
            root = ElementTree.fromstring(data)
        except ElementTree.ParseError as exc:
            raise ValueError(f"registered resource is not SVG XML: {relative}: {exc}") from exc
        if root.tag.rsplit("}", 1)[-1] != "svg":
            raise ValueError(f"registered resource is XML without an svg root: {relative}")
    else:
        try:
            json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"registered resource is not UTF-8 JSON: {relative}: {exc}") from exc
    if relative.name.casefold() not in _registered_resource_paths(source, relative.parts[0]):
        raise ValueError(
            f"registered resource is not listed in {relative.parts[0]}/definition/report.json "
            f"resourcePackages: {relative}"
        )


def _check_item_content(source: Path, relative: Path) -> None:
    """An allowed item file name must also hold what PBIP writes there, not other material."""
    parts = [part.casefold() for part in relative.parts]
    if parts[1:] == [".platform"]:
        item_type = PLATFORM_ITEM_TYPES[".report" if parts[0].endswith(".report") else ".semanticmodel"]
        data = _json_object(source / relative, relative)
        metadata = data.get("metadata")
        if (
            not isinstance(data.get("$schema"), str)
            or not isinstance(metadata, dict)
            or metadata.get("type") != item_type
            or not isinstance(metadata.get("displayName"), str)
        ):
            raise ValueError(
                f".platform needs $schema and metadata with type {item_type} and a displayName: {relative}"
            )
    elif parts[1:3] == ["staticresources", "registeredresources"]:
        _check_registered_resource(source, relative)
    else:
        _json_object(source / relative, relative)


def _validate_project_files(source: Path) -> None:
    for path in source.rglob("*"):
        if not path.is_file() or _is_local_state(path.relative_to(source)):
            continue
        relative = path.relative_to(source)
        if _is_item_file(relative):
            _check_item_content(source, relative)
            continue
        name = path.name.casefold()
        suffix = path.suffix.casefold()
        if suffix not in PROJECT_SUFFIXES and name not in PROJECT_METADATA_FILES:
            raise ValueError(f"source project contains a non-PBIP file that will not be copied: {relative}")
        if suffix == ".json" and relative.parent == Path(".") and name not in PROJECT_METADATA_FILES:
            raise ValueError(f"JSON outside the PBIP definitions is not permitted: {relative}")
        if suffix == ".json" and relative.parent != Path(".") and not _in_project_definition(relative):
            raise ValueError(f"JSON outside a PBIP report/model definition is not permitted: {relative}")
        if suffix == ".pbip" and relative.parent != Path("."):
            raise ValueError(f"nested .pbip files are not permitted: {relative}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _baseline_manifest(baseline: Path) -> dict[str, str]:
    return {
        path.relative_to(baseline).as_posix(): _sha256(path) for path in sorted(baseline.rglob("*")) if path.is_file()
    }


def _make_read_only(path: Path) -> None:
    """Remove write permissions from a tree, processing children before parents."""
    write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    for item in sorted([path, *path.rglob("*")], key=lambda child: len(child.parts), reverse=True):
        item.chmod(item.stat().st_mode & ~write_bits)


def _require_read_only(path: Path) -> None:
    if path.stat().st_mode & stat.S_IWRITE:
        raise ValueError(f"protected run manifest is writable: {path}")


def _validate_hash_inventory(inventory: object) -> dict[str, str]:
    if not isinstance(inventory, dict) or not inventory:
        raise ValueError("run manifest has no valid baseline hash inventory")
    checked: dict[str, str] = {}
    for relative, digest in inventory.items():
        if not isinstance(relative, str) or not isinstance(digest, str) or not HASH_PATTERN.fullmatch(digest):
            raise ValueError("run manifest has no valid baseline hash inventory")
        path = PurePosixPath(relative)
        if (
            path.is_absolute()
            or path.as_posix() != relative
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError(f"unsafe baseline path in run manifest: {relative}")
        if "\\" in relative or ":" in relative:
            raise ValueError(f"unsafe baseline path in run manifest: {relative}")
        checked[relative] = digest
    return checked


def _load_run(run_root: Path, repo_root: Path) -> dict:
    if run_root.exists() and _is_reparse_point(run_root):
        raise ValueError(f"run root is a reparse point: {run_root}")
    root = run_root.resolve(strict=True)
    repository = repo_root.resolve(strict=True)
    if root == repository or root.is_relative_to(repository):
        raise ValueError(f"run root must be outside the repository: {repository}")
    if not RUN_NAME_PATTERN.fullmatch(root.name):
        raise ValueError(f"run root must use the numbered <NNN>-<slug> name: {root.name}")
    manifest_path = root / "run.json"
    if not manifest_path.is_file() or _is_reparse_point(manifest_path):
        raise ValueError(f"not a migration run created by this tool: {root}")
    _require_read_only(manifest_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read run manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != RUN_SCHEMA:
        raise ValueError("unsupported or malformed migration run manifest")
    if manifest.get("runRoot") != str(root):
        raise ValueError("run manifest path does not match this run root")
    try:
        uuid.UUID(manifest.get("runId", ""))
        datetime.fromisoformat(manifest.get("createdAt", ""))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("run manifest has invalid identity or creation time") from exc
    if not isinstance(manifest.get("units"), dict):
        raise ValueError("run manifest has malformed metadata or unit inventory")
    units = manifest["units"]
    if not units:
        raise ValueError("run manifest has no report units")
    for key, entry in units.items():
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key) or not isinstance(entry, dict):
            raise ValueError("run manifest has an invalid report-unit entry")
        if (
            not isinstance(entry.get("projectFile"), str)
            or Path(entry["projectFile"]).name != entry["projectFile"]
            or Path(entry["projectFile"]).suffix.casefold() != ".pbip"
        ):
            raise ValueError(f"run manifest has an invalid project file for unit {key}")
        inventory = _validate_hash_inventory(entry.get("baselineSha256"))
        if entry["projectFile"] not in inventory:
            raise ValueError(f"run manifest hash inventory omits project file for unit {key}")
        entry["baselineSha256"] = inventory
    return manifest


def _verify_unit_path(unit_dir: Path, repo_root: Path) -> tuple[Path, str, dict, dict[str, str]]:
    if unit_dir.exists() and _is_reparse_point(unit_dir):
        raise ValueError(f"unit directory is a reparse point: {unit_dir}")
    if unit_dir.parent.exists() and _is_reparse_point(unit_dir.parent):
        raise ValueError(f"units directory is a reparse point: {unit_dir.parent}")
    if unit_dir.parent.parent.exists() and _is_reparse_point(unit_dir.parent.parent):
        raise ValueError(f"run root is a reparse point: {unit_dir.parent.parent}")
    unit = unit_dir.resolve(strict=True)
    if unit.parent.name != "units":
        raise ValueError(f"unit must be under a run's units directory: {unit}")
    run_root = unit.parent.parent
    manifest = _load_run(run_root, repo_root)
    key = unit.name
    if key not in manifest["units"]:
        raise ValueError(f"unit is not recorded in its run manifest: {key}")
    expected_unit = (run_root / "units" / key).resolve(strict=True)
    if unit != expected_unit:
        raise ValueError("unit path does not match its run manifest entry")
    if _is_reparse_point(unit.parent):
        raise ValueError(f"units directory is a reparse point: {unit.parent}")
    entry = manifest["units"][key]
    baseline = unit / "baseline"
    working = unit / "working"
    brief = unit / "brief.md"
    if not baseline.is_dir() or not working.is_dir() or not brief.is_file():
        raise ValueError(f"unit is missing baseline, working copy or human-owned brief: {unit}")
    _reject_reparse_points(unit, "report unit")
    actual = _baseline_manifest(baseline)
    expected = entry["baselineSha256"]
    changed = sorted(path for path in set(expected) | set(actual) if expected.get(path) != actual.get(path))
    if changed:
        raise ValueError(f"baseline changed since run creation: {', '.join(changed[:10])}")
    for path in baseline.rglob("*"):
        if path.is_file():
            _require_read_only(path)
    return run_root, key, manifest, actual


def verify_unit(unit_dir: Path, repo_root: Path) -> dict[str, str]:
    """Verify one unit against the authoritative hash inventory in its parent run.json."""
    _, _, _, hashes = _verify_unit_path(unit_dir, repo_root)
    return hashes


def verify_run(run_root: Path, repo_root: Path) -> dict[str, dict[str, str]]:
    """Read-only verification of every manifest-listed unit in a numbered run."""
    manifest = _load_run(run_root, repo_root)
    root = run_root.resolve(strict=True)
    units_dir = root / "units"
    if not units_dir.is_dir() or _is_reparse_point(units_dir):
        raise ValueError(f"run is missing a safe units directory: {units_dir}")
    actual_keys = {child.name for child in units_dir.iterdir() if child.is_dir()}
    expected_keys = set(manifest["units"])
    if actual_keys != expected_keys:
        raise ValueError("run unit directories do not match the run manifest")
    verified = {}
    for key in sorted(expected_keys):
        hashes = verify_unit(units_dir / key, repo_root)
        verified[key] = hashes
    return verified


def _prepare_source(source_project: Path, target: Path, repo_root: Path) -> tuple[Path, Path]:
    if source_project.exists() and _is_reparse_point(source_project):
        raise ValueError(f"source project is a reparse point: {source_project}")
    source = source_project.resolve(strict=True)
    repository = repo_root.resolve(strict=True)
    if not source.is_dir():
        raise ValueError(f"source project is not a directory: {source}")
    if target == repository or target.is_relative_to(repository):
        raise ValueError(f"run root must be outside the repository: {repository}")
    if target == source or target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError("source project and run root must not contain one another")
    pbip_files = sorted(source.glob("*.pbip"))
    if len(pbip_files) != 1:
        raise ValueError(f"source project must contain exactly one top-level .pbip file, found {len(pbip_files)}")
    _reject_reparse_points(source)
    _validate_project_files(source)
    return source, pbip_files[0]


def _unit_key(value: str) -> str:
    if not KEY_PATTERN.fullmatch(value):
        raise ValueError("unit key must contain only lowercase letters, digits, hyphens or underscores")
    return value


def _write_run_manifest(run_root: Path, manifest: dict) -> None:
    path = run_root / "run.json"
    temporary = run_root / f".run-{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        if path.exists():
            path.chmod(path.stat().st_mode | stat.S_IWRITE)
        os.replace(temporary, path)
        path.chmod(path.stat().st_mode & ~stat.S_IWRITE)
    finally:
        if temporary.exists():
            temporary.chmod(temporary.stat().st_mode | stat.S_IWRITE)
            temporary.unlink()
        if path.exists():
            path.chmod(path.stat().st_mode & ~stat.S_IWRITE)


def _stage_unit(source: Path, project_file: Path, stage: Path) -> dict[str, object]:
    baseline = stage / "baseline"
    working = stage / "working"
    shutil.copytree(source, baseline, ignore=_copy_ignore)
    shutil.copytree(baseline, working)
    hashes = _baseline_manifest(baseline)
    (stage / "brief.md").write_text(BRIEF_TEMPLATE, encoding="utf-8")
    _make_read_only(baseline)
    return {"projectFile": project_file.name, "baselineSha256": hashes}


def create_run(
    source_project: Path, run_parent: Path, run_slug: str, unit_key: str, repo_root: Path
) -> tuple[Path, Path]:
    """Allocate the next numbered run root and scaffold its first report unit."""
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", run_slug):
        raise ValueError("run slug must contain lowercase letters, digits and hyphens")
    key = _unit_key(unit_key)
    parent = run_parent.resolve()
    repository = repo_root.resolve(strict=True)
    if parent == repository or parent.is_relative_to(repository):
        raise ValueError(f"run parent must be outside the repository: {repository}")
    if parent.exists() and _is_reparse_point(parent):
        raise ValueError(f"run parent is a reparse point: {parent}")
    candidate_target = parent / f"000-{run_slug}"
    source, project_file = _prepare_source(source_project, candidate_target, repository)
    parent.mkdir(parents=True, exist_ok=True)
    occupied = []
    for child in parent.iterdir():
        match = RUN_NAME_PATTERN.fullmatch(child.name)
        if match:
            occupied.append(int(match.group("number")))
    number = max(occupied, default=0) + 1
    run_root = parent / f"{number:03d}-{run_slug}"
    if run_root.exists():
        raise FileExistsError(f"run root already exists; refusing to overwrite: {run_root}")
    with tempfile.TemporaryDirectory(prefix=f".{run_root.name}.staging-", dir=parent) as temporary:
        stage = Path(temporary)
        (stage / "units").mkdir()
        unit_stage = stage / "units" / key
        unit_stage.mkdir()
        entry = _stage_unit(source, project_file, unit_stage)
        manifest = {
            "schemaVersion": RUN_SCHEMA,
            "runId": str(uuid.uuid4()),
            "runRoot": str(run_root.resolve()),
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "units": {key: entry},
        }
        (stage / "run.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        (stage / "run.json").chmod((stage / "run.json").stat().st_mode & ~stat.S_IWRITE)
        os.rename(stage, run_root)
    return run_root, run_root / "units" / key


def add_unit(source_project: Path, run_root: Path, unit_key: str, repo_root: Path) -> Path:
    """Add a report unit to an existing run, updating only its external run manifest."""
    key = _unit_key(unit_key)
    if run_root.exists() and _is_reparse_point(run_root):
        raise ValueError(f"run root is a reparse point: {run_root}")
    root = run_root.resolve(strict=True)
    verify_run(root, repo_root)
    manifest = _load_run(root, repo_root)
    if key in manifest["units"] or (root / "units" / key).exists():
        raise FileExistsError(f"report unit already exists; refusing to overwrite: {key}")
    source, project_file = _prepare_source(source_project, root / "units" / key, repo_root)
    units_dir = root / "units"
    if not units_dir.is_dir() or _is_reparse_point(units_dir):
        raise ValueError(f"run is missing a safe units directory: {units_dir}")
    _reject_reparse_points(root, "run root")
    installed = False
    try:
        with tempfile.TemporaryDirectory(prefix=f".{key}.staging-", dir=units_dir) as temporary:
            stage_path = Path(temporary) / key
            stage_path.mkdir()
            entry = _stage_unit(source, project_file, stage_path)
            os.rename(stage_path, units_dir / key)
            installed = True
        updated = dict(manifest)
        updated["units"] = {**manifest["units"], key: entry}
        _write_run_manifest(root, updated)
    except Exception:
        created = units_dir / key
        if installed and created.exists():
            shutil.rmtree(created, onerror=lambda function, path, _: (os.chmod(path, stat.S_IWRITE), function(path)))
        raise
    return units_dir / key


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-project", type=Path, help="directory containing one converted .pbip")
    parser.add_argument("--run-parent", type=Path, help="external directory in which to allocate a numbered run")
    parser.add_argument("--run-slug", help="lowercase slug for a newly allocated run root")
    parser.add_argument("--run-root", type=Path, help="existing numbered run root to add a report unit to")
    parser.add_argument("--unit-key", help="safe key for the report unit under units\\")
    parser.add_argument("--verify-unit", type=Path, help="verify one unit against its parent run.json")
    parser.add_argument("--verify-run", type=Path, help="read-only verification of all units in a run")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        root = repository_root()
        if args.verify_unit is not None or args.verify_run is not None:
            if any((args.source_project, args.run_parent, args.run_slug, args.run_root, args.unit_key)):
                parser.error("verification options cannot be combined with creation options")
            if args.verify_unit is not None and args.verify_run is not None:
                parser.error("choose either --verify-unit or --verify-run")
            if args.verify_unit is not None:
                hashes = verify_unit(args.verify_unit, root)
                LOG.info("Baseline verified: %d files match parent run.json", len(hashes))
            else:
                verified = verify_run(args.verify_run, root)
                LOG.info("Run verified: %d report unit(s) match run.json", len(verified))
            return 0
        if args.source_project is None or args.unit_key is None:
            parser.error("creation requires --source-project and --unit-key")
        if args.run_root is not None:
            if args.run_parent is not None or args.run_slug is not None:
                parser.error("--run-root cannot be combined with --run-parent or --run-slug")
            unit = add_unit(args.source_project, args.run_root, args.unit_key, root)
            run = unit.parent.parent
        else:
            if args.run_parent is None or args.run_slug is None:
                parser.error("new runs require --run-parent and --run-slug")
            run, unit = create_run(args.source_project, args.run_parent, args.run_slug, args.unit_key, root)
    except (OSError, ValueError) as exc:
        LOG.error("%s", exc)
        return 2
    LOG.info("Created report unit: %s", unit)
    LOG.info("Run manifest: %s", run / "run.json")
    LOG.info("Baseline is read-only evidence; agents edit only working after brief.md is agreed.")
    LOG.info("The run root is outside unit scope, not a cryptographic trust boundary for same-user write access.")
    LOG.info("Local Desktop cache and settings were not copied. No Cognos source files or data were copied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
