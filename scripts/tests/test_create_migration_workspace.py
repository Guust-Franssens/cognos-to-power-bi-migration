"""Tests for external numbered runs and their report-unit scaffolds."""

from __future__ import annotations

from collections.abc import Iterator
import hashlib
import json
import shutil
import stat
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from create_migration_workspace import add_unit, create_run, verify_run, verify_unit  # noqa: E402


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """Keep test artifacts inside the checkout and remove them after each case."""
    path = Path(__file__).resolve().parents[2] / ".run-root-test-data" / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(
            path,
            onerror=lambda function, filename, _: (Path(filename).chmod(stat.S_IWRITE), function(filename)),
        )
        parent = path.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()


@pytest.fixture
def source_project(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "Demo.pbip").write_text('{"version": "1"}\n', encoding="utf-8")
    model = source / "Demo.SemanticModel" / "definition"
    model.mkdir(parents=True)
    (model / "model.tmdl").write_text("model Demo\n", encoding="utf-8")
    return source


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    path = tmp_path / "repository"
    path.mkdir()
    return path


def _create_run(
    tmp_path: Path, source: Path, repository: Path, slug: str = "customer-report", key: str = "report-one"
) -> tuple[Path, Path]:
    return create_run(source, tmp_path / "external-runs", slug, key, repository)


def _rewrite_readonly_manifest(run_root: Path, update: dict) -> None:
    manifest_path = run_root / "run.json"
    manifest_path.chmod(manifest_path.stat().st_mode | stat.S_IWRITE)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(update)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest_path.chmod(manifest_path.stat().st_mode & ~stat.S_IWRITE)


def test_allocates_numbered_run_and_creates_run_unit_hierarchy(
    tmp_path: Path, source_project: Path, repository: Path
) -> None:
    parent = tmp_path / "external-runs"
    parent.mkdir()
    (parent / "004-older-run").mkdir()

    run_root, unit = create_run(source_project, parent, "customer-report", "report-one", repository)

    assert run_root.name == "005-customer-report"
    assert unit == run_root / "units" / "report-one"
    assert (unit / "baseline" / "Demo.pbip").is_file()
    assert (unit / "working" / "Demo.pbip").is_file()
    assert (unit / "brief.md").is_file()
    assert (run_root / "run.json").is_file()
    assert not (unit / "unit.json").exists()
    manifest = json.loads((run_root / "run.json").read_text(encoding="utf-8"))
    assert manifest["schemaVersion"] == "migration-run.v1"
    assert manifest["runRoot"] == str(run_root.resolve())
    assert manifest["units"]["report-one"]["projectFile"] == "Demo.pbip"


@pytest.mark.parametrize("filename", ["original-report.xml", "source-data.csv", "export.xlsx", "screenshot.png"])
def test_source_material_rejection_leaves_no_run_output(
    tmp_path: Path, source_project: Path, repository: Path, filename: str
) -> None:
    (source_project / filename).write_text("synthetic rejected input", encoding="utf-8")
    parent = tmp_path / "external-runs"
    parent.mkdir()

    with pytest.raises(ValueError, match="non-PBIP file"):
        create_run(source_project, parent, "customer-report", "report-one", repository)

    assert list(parent.iterdir()) == []


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'


def _platform(item_type: str) -> str:
    return json.dumps(
        {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
            "metadata": {"type": item_type, "displayName": "Demo"},
            "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000000"},
        }
    )


def _write(path: Path, content: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    return path


def _register(source: Path, *names: str) -> None:
    """Write Demo.Report/definition/report.json listing names in its RegisteredResources package (PBIR)."""
    report = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/3.0.0/schema.json",
        "themeCollection": {"baseTheme": {"name": "CY24SU10", "type": "SharedResources"}},
        "resourcePackages": [
            {
                "name": "SharedResources",
                "type": "SharedResources",
                "items": [{"name": "CY24SU10", "path": "BaseThemes/CY24SU10.json", "type": "BaseTheme"}],
            },
            {
                "name": "RegisteredResources",
                "type": "RegisteredResources",
                "items": [{"name": name, "path": name, "type": "Image"} for name in names],
            },
        ],
    }
    _write(source / "Demo.Report" / "definition" / "report.json", json.dumps(report))


def test_accepts_documented_pbip_item_files(tmp_path: Path, source_project: Path, repository: Path) -> None:
    report = source_project / "Demo.Report"
    resources = report / "StaticResources" / "RegisteredResources"
    item_files = [
        _write(source_project / "Demo.SemanticModel" / ".platform", _platform("SemanticModel")),
        _write(source_project / "Demo.SemanticModel" / "diagramLayout.json", '{"version": "1.1.0"}'),
        _write(report / ".platform", "\ufeff" + _platform("Report")),
        _write(resources / "logo1234.png", PNG),
        _write(resources / "photo.JPG", b"\xff\xd8\xff\xe0" + b"\x00" * 8),
        _write(resources / "anim.gif", b"GIF89a" + b"\x00" * 8),
        _write(resources / "icon.svg", SVG),
        _write(resources / "CustomTheme.json", '{"name": "Custom"}'),
        _write(report / "StaticResources" / "SharedResources" / "BaseThemes" / "CY24SU10.json", '{"name": "x"}'),
    ]
    _register(source_project, "logo1234.png", "photo.jpg", "anim.gif", "icon.svg", "CustomTheme.json")

    run_root, unit = _create_run(tmp_path, source_project, repository)

    inventory = json.loads((run_root / "run.json").read_text(encoding="utf-8"))["units"]["report-one"]
    for path in item_files:
        relative = path.relative_to(source_project)
        assert relative.as_posix() in inventory["baselineSha256"]
        assert (unit / "working" / relative).is_file()


@pytest.mark.parametrize(
    "relative",
    [
        ".platform",
        "Demo.Report/screenshot.png",
        "Demo.Report/diagramLayout.json",
        "Demo.Report/StaticResources/RegisteredResources/export.csv",
        "Demo.Report/StaticResources/RegisteredResources/nested/logo.png",
        "Demo.Report/StaticResources/SharedResources/BaseThemes/theme.png",
        "Demo.Report/StaticResources/SharedResources/BaseThemes/nested/theme.json",
        "Demo.SemanticModel/StaticResources/RegisteredResources/logo.png",
        "Demo.SemanticModel/nested/.platform",
        "Demo.SemanticModel/nested/diagramLayout.json",
        "Demo.SemanticModel/definition/x/../../nested2/diagramLayout.json",
        "Other/StaticResources/RegisteredResources/logo.png",
    ],
)
def test_refuses_near_miss_item_files(tmp_path: Path, source_project: Path, repository: Path, relative: str) -> None:
    path = source_project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="non-PBIP file|JSON outside"):
        _create_run(tmp_path, source_project, repository)


@pytest.mark.parametrize(
    ("relative", "content", "message"),
    [
        ("Demo.Report/.platform", "<report><query/></report>", "not UTF-8 JSON"),
        ("Demo.Report/.platform", "[1, 2]", "not a JSON object"),
        ("Demo.Report/.platform", '{"metadata": {"type": "Report", "displayName": "Demo"}}', "needs \\$schema"),
        ("Demo.Report/.platform", '{"$schema": "s", "metadata": {"type": "Report"}}', "and a displayName"),
        ("Demo.Report/.platform", '{"$schema": "s", "metadata": {"type": "Report", "displayName": 1}}', "displayName"),
        ("Demo.Report/.platform", _platform("SemanticModel"), "type Report"),
        ("Demo.SemanticModel/.platform", _platform("Report"), "type SemanticModel"),
        ("Demo.SemanticModel/diagramLayout.json", "<report><query/></report>", "not UTF-8 JSON"),
        ("Demo.SemanticModel/diagramLayout.json", '"text"', "not a JSON object"),
        ("Demo.Report/StaticResources/SharedResources/BaseThemes/CY24SU10.json", "<theme/>", "not UTF-8 JSON"),
    ],
)
def test_refuses_item_files_without_pbip_content(
    tmp_path: Path, source_project: Path, repository: Path, relative: str, content: str, message: str
) -> None:
    _write(source_project / relative, content)
    parent = tmp_path / "external-runs"
    parent.mkdir()

    with pytest.raises(ValueError, match=message):
        create_run(source_project, parent, "customer-report", "report-one", repository)
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("logo.png", b"GIF89a....", "not a PNG image"),
        ("logo.png", b"synthetic screenshot", "not a PNG image"),
        ("photo.jpg", PNG, "not a JPG image"),
        ("photo.jpeg", b"\x00\x00", "not a JPEG image"),
        ("anim.gif", PNG, "not a GIF image"),
        ("icon.svg", b"<report><query/></report>", "without an svg root"),
        ("icon.svg", b"<svg", "not SVG XML"),
        ("theme.json", b"<report/>", "not UTF-8 JSON"),
    ],
)
def test_refuses_registered_resources_whose_content_does_not_match_the_suffix(
    tmp_path: Path, source_project: Path, repository: Path, name: str, content: bytes, message: str
) -> None:
    _write(source_project / "Demo.Report" / "StaticResources" / "RegisteredResources" / name, content)
    _register(source_project, name)

    with pytest.raises(ValueError, match=message):
        _create_run(tmp_path, source_project, repository)


@pytest.mark.parametrize(
    ("name", "content"),
    [("screenshot.png", PNG), ("data-export.json", '[{"region": "north", "sales": 10}]'), ("icon.svg", SVG)],
)
def test_refuses_registered_resources_the_report_does_not_reference(
    tmp_path: Path, source_project: Path, repository: Path, name: str, content
) -> None:
    _write(source_project / "Demo.Report" / "StaticResources" / "RegisteredResources" / name, content)
    _register(source_project, "logo.png")

    with pytest.raises(ValueError, match="not listed in Demo.Report/definition/report.json"):
        _create_run(tmp_path, source_project, repository)


def test_refuses_a_registered_resource_named_only_in_another_package(
    tmp_path: Path, source_project: Path, repository: Path
) -> None:
    _write(source_project / "Demo.Report" / "StaticResources" / "RegisteredResources" / "CY24SU10.json", "{}")
    _register(source_project)
    report_json = source_project / "Demo.Report" / "definition" / "report.json"
    report = json.loads(report_json.read_text(encoding="utf-8"))
    report["resourcePackages"][0]["items"].append({"name": "x", "path": "CY24SU10.json", "type": "BaseTheme"})
    report_json.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="not listed"):
        _create_run(tmp_path, source_project, repository)


def test_refuses_a_registered_resource_without_a_report_json(
    tmp_path: Path, source_project: Path, repository: Path
) -> None:
    _write(source_project / "Demo.Report" / "StaticResources" / "RegisteredResources" / "logo.png", PNG)

    with pytest.raises(ValueError, match="not listed"):
        _create_run(tmp_path, source_project, repository)


def test_excludes_desktop_cache_and_local_settings(tmp_path: Path, source_project: Path, repository: Path) -> None:
    local_state = source_project / "Demo.SemanticModel" / ".pbi"
    local_state.mkdir()
    (local_state / "cache.abf").write_bytes(b"synthetic cache")
    (source_project / "localSettings.json").write_text('{"local": true}\n', encoding="utf-8")

    _, unit = _create_run(tmp_path, source_project, repository)

    for tree in ("baseline", "working"):
        assert not (unit / tree / "Demo.SemanticModel" / ".pbi").exists()
        assert not (unit / tree / "localSettings.json").exists()


def test_run_manifest_is_read_only_and_working_copy_is_editable(
    tmp_path: Path, source_project: Path, repository: Path
) -> None:
    run_root, unit = _create_run(tmp_path, source_project, repository)
    baseline_file = unit / "baseline" / "Demo.pbip"
    working_file = unit / "working" / "Demo.pbip"
    run_manifest = run_root / "run.json"

    assert not baseline_file.stat().st_mode & stat.S_IWRITE
    assert not run_manifest.stat().st_mode & stat.S_IWRITE
    assert working_file.stat().st_mode & stat.S_IWRITE
    working_file.write_text('{"version": "2"}\n', encoding="utf-8")
    assert verify_unit(unit, repository)


def test_verifies_hash_inventory_stored_only_in_parent_run_manifest(
    tmp_path: Path, source_project: Path, repository: Path
) -> None:
    run_root, unit = _create_run(tmp_path, source_project, repository)
    baseline = unit / "baseline"
    manifest = json.loads((run_root / "run.json").read_text(encoding="utf-8"))
    assert manifest["units"]["report-one"]["baselineSha256"] == {
        path.relative_to(baseline).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(baseline.rglob("*"))
        if path.is_file()
    }
    assert not list(unit.glob("*.json"))
    assert verify_unit(unit, repository) == manifest["units"]["report-one"]["baselineSha256"]


@pytest.mark.parametrize("change", ["modified", "added", "deleted"])
def test_modified_added_and_deleted_baseline_files_fail_verification(
    tmp_path: Path, source_project: Path, repository: Path, change: str
) -> None:
    _, unit = _create_run(tmp_path, source_project, repository)
    baseline = unit / "baseline"
    baseline_file = baseline / "Demo.pbip"
    if change == "modified":
        baseline_file.chmod(baseline_file.stat().st_mode | stat.S_IWRITE)
        baseline_file.write_text('{"version": "2"}\n', encoding="utf-8")
        baseline_file.chmod(baseline_file.stat().st_mode & ~stat.S_IWRITE)
    elif change == "added":
        baseline.chmod(baseline.stat().st_mode | stat.S_IWRITE)
        added = baseline / "extra.tmdl"
        added.write_text("changed\n", encoding="utf-8")
        added.chmod(added.stat().st_mode & ~stat.S_IWRITE)
        baseline.chmod(baseline.stat().st_mode & ~stat.S_IWRITE)
    else:
        baseline.chmod(baseline.stat().st_mode | stat.S_IWRITE)
        baseline_file.chmod(baseline_file.stat().st_mode | stat.S_IWRITE)
        baseline_file.unlink()
        baseline.chmod(baseline.stat().st_mode & ~stat.S_IWRITE)

    with pytest.raises(ValueError, match="baseline changed since run creation"):
        verify_unit(unit, repository)


def test_manifest_identity_path_mismatch_fails_closed(tmp_path: Path, source_project: Path, repository: Path) -> None:
    run_root, unit = _create_run(tmp_path, source_project, repository)
    _rewrite_readonly_manifest(run_root, {"runRoot": str(tmp_path / "different-run")})

    with pytest.raises(ValueError, match="path does not match"):
        verify_unit(unit, repository)


@pytest.mark.parametrize("bad_path", ["../outside.tmdl", "C:/outside.tmdl"])
def test_malformed_manifest_baseline_paths_fail_closed(
    tmp_path: Path, source_project: Path, repository: Path, bad_path: str
) -> None:
    run_root, unit = _create_run(tmp_path, source_project, repository)
    manifest_path = run_root / "run.json"
    manifest_path.chmod(manifest_path.stat().st_mode | stat.S_IWRITE)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    inventory = manifest["units"]["report-one"]["baselineSha256"]
    inventory[bad_path] = next(iter(inventory.values()))
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest_path.chmod(manifest_path.stat().st_mode & ~stat.S_IWRITE)

    with pytest.raises(ValueError, match="unsafe baseline path"):
        verify_unit(unit, repository)


def test_unit_must_match_parent_run_manifest_path(tmp_path: Path, source_project: Path, repository: Path) -> None:
    run_root, _ = _create_run(tmp_path, source_project, repository)
    misplaced = tmp_path / "report-one"
    misplaced.mkdir()

    with pytest.raises(ValueError, match="under a run's units directory"):
        verify_unit(misplaced, repository)


def test_reparse_run_root_alias_is_rejected(tmp_path: Path, source_project: Path, repository: Path) -> None:
    run_root, _ = _create_run(tmp_path, source_project, repository)
    alias = tmp_path / "run-alias"
    try:
        alias.symlink_to(run_root, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="reparse point"):
        verify_unit(alias / "units" / "report-one", repository)


def test_adds_and_verifies_multiple_units_read_only(tmp_path: Path, source_project: Path, repository: Path) -> None:
    run_root, first_unit = _create_run(tmp_path, source_project, repository)
    second_unit = add_unit(source_project, run_root, "report-two", repository)
    before = (run_root / "run.json").read_bytes()

    verified = verify_run(run_root, repository)

    assert set(verified) == {"report-one", "report-two"}
    assert verify_unit(first_unit, repository)
    assert second_unit == run_root / "units" / "report-two"
    assert (run_root / "run.json").read_bytes() == before


def test_rejected_second_source_does_not_create_unit_or_change_manifest(
    tmp_path: Path, source_project: Path, repository: Path
) -> None:
    run_root, _ = _create_run(tmp_path, source_project, repository)
    bad_source = tmp_path / "bad-source"
    bad_source.mkdir()
    (bad_source / "Bad.pbip").write_text("{}", encoding="utf-8")
    (bad_source / "source.csv").write_text("private", encoding="utf-8")
    before = (run_root / "run.json").read_bytes()

    with pytest.raises(ValueError, match="non-PBIP file"):
        add_unit(bad_source, run_root, "report-two", repository)

    assert not (run_root / "units" / "report-two").exists()
    assert (run_root / "run.json").read_bytes() == before


def test_refuses_a_run_inside_the_repository(source_project: Path, repository: Path) -> None:
    with pytest.raises(ValueError, match="outside the repository"):
        create_run(source_project, repository / "runs", "customer-report", "report-one", repository)

    assert not (repository / "runs").exists()


def test_refuses_source_project_links(tmp_path: Path, source_project: Path, repository: Path) -> None:
    link = source_project / "linked.txt"
    try:
        link.symlink_to(source_project / "Demo.pbip")
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="link or reparse point"):
        _create_run(tmp_path, source_project, repository)

    assert not (tmp_path / "external-runs").exists()


@pytest.fixture
def junction() -> Iterator:
    """Create directory junctions (no privilege needed on Windows); remove them, unfollowed, after the test."""
    made: list[Path] = []

    def make(link: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        try:
            import _winapi

            _winapi.CreateJunction(str(target.resolve()), str(link))
        except (ImportError, AttributeError, OSError) as exc:
            pytest.skip(f"directory junctions cannot be created here: {exc}")
        made.append(link)

    yield make
    for link in reversed(made):
        if link.exists() or link.is_symlink():
            link.rmdir()


def test_a_link_inside_a_unit_is_named_as_in_the_unit(
    tmp_path: Path, source_project: Path, repository: Path, junction
) -> None:
    _, unit = _create_run(tmp_path, source_project, repository)
    junction(unit / "notes", tmp_path / "elsewhere")

    with pytest.raises(ValueError, match="^report unit contains a link or reparse point: .*notes$"):
        verify_unit(unit, repository)


def test_a_link_inside_the_run_root_is_named_as_in_the_run_root(
    tmp_path: Path, source_project: Path, repository: Path, junction
) -> None:
    run_root, _ = _create_run(tmp_path, source_project, repository)
    junction(run_root / "notes", tmp_path / "elsewhere")

    with pytest.raises(ValueError, match="^run root contains a link or reparse point: .*notes$"):
        add_unit(source_project, run_root, "report-two", repository)
    assert not (run_root / "units" / "report-two").exists()
