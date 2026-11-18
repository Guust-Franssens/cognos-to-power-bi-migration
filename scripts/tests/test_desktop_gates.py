"""Offline tests for the Power BI Desktop gates of a migration run unit (no Desktop or ADOMD needed)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import capture_pages  # noqa: E402
import dax_query  # noqa: E402
import desktop_gate  # noqa: E402
import parity_check  # noqa: E402
from create_migration_workspace import create_run  # noqa: E402
from desktop_gate import GateRefusal  # noqa: E402

CHECK = {
    "id": "by_month",
    "title": "Totals by month",
    "source": "synthetic",
    "dax": "EVALUATE SUMMARIZECOLUMNS('T'[Month], \"V\", [V])",
    "keys": ["Month"],
    "round": 2,
    "exact_rows": True,
    "expected": {"jan": [1.5], "feb": [2.25]},
}
TMSL = '{"delete": {"object": {"database": "x"}}}'
PAGES = ["ReportSection1", "ReportSection2"]
PBIP = {"version": "1.0", "artifacts": [{"report": {"path": "Demo.Report"}}], "settings": {}}
PBIR = {"version": "4.0", "datasetReference": {"byPath": {"path": "../Demo.SemanticModel"}}}


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
def repository(tmp_path: Path) -> Path:
    path = tmp_path / "repository"
    path.mkdir()
    return path


@pytest.fixture
def unit_dir(tmp_path: Path, repository: Path) -> Path:
    source = tmp_path / "source"
    model = source / "Demo.SemanticModel" / "definition"
    model.mkdir(parents=True)
    pages = source / "Demo.Report" / "definition" / "pages"
    pages.mkdir(parents=True)
    (source / "Demo.pbip").write_text(json.dumps(PBIP), encoding="utf-8")
    (source / "Demo.Report" / "definition.pbir").write_text(json.dumps(PBIR), encoding="utf-8")
    (model / "model.tmdl").write_text("model Demo\n", encoding="utf-8")
    (pages / "pages.json").write_text(json.dumps({"pageOrder": PAGES}), encoding="utf-8")
    _, unit = create_run(source, tmp_path / "runs", "gate-test", "report-one", repository)
    return unit


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch, repository: Path) -> pytest.MonkeyPatch:
    monkeypatch.setattr(desktop_gate, "repository_root", lambda: repository)
    return monkeypatch


def _desktop(pbip: Path | str, name: str = "PBIDesktop.exe", ports: list[int] | None = None) -> dict:
    return {"pid": 4242, "name": name, "cmdline": f'"C:\\PBI\\PBIDesktop.exe" "{pbip}"', "ports": ports or [50000]}


def _oracle(folder: Path, checks: list[dict] | None = None, name: str = "parity.json") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(json.dumps({"checks": [CHECK] if checks is None else checks}), encoding="utf-8")
    return path


def _raw_oracle(folder: Path, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "parity.json"
    path.write_text(text, encoding="utf-8")
    return path


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def Close(self) -> None:  # noqa: N802  (mirrors the .NET method name)
        self.closed = True


# --- unit verification -------------------------------------------------------------------------------


def test_verified_unit_returns_the_working_project_of_a_recorded_unit(unit_dir: Path, repository: Path) -> None:
    unit = desktop_gate.verified_unit(unit_dir, repository)

    assert unit.key == "report-one"
    assert unit.working_pbip == unit_dir.resolve() / "working" / "Demo.pbip"
    assert unit.run_root == unit_dir.resolve().parent.parent


def test_verified_unit_refuses_a_changed_baseline(unit_dir: Path, repository: Path) -> None:
    model = unit_dir / "baseline" / "Demo.SemanticModel" / "definition" / "model.tmdl"
    model.chmod(stat.S_IWRITE)
    model.write_text("model Changed\n", encoding="utf-8")

    with pytest.raises(GateRefusal, match="baseline changed"):
        desktop_gate.verified_unit(unit_dir, repository)


@pytest.mark.parametrize("change", ["rename", "extra"])
def test_verified_unit_refuses_a_working_tree_without_exactly_the_baseline_project(
    unit_dir: Path, repository: Path, change: str
) -> None:
    working = unit_dir / "working"
    if change == "rename":
        (working / "Demo.pbip").rename(working / "Other.pbip")
    else:
        (working / "Other.pbip").write_text("{}", encoding="utf-8")

    with pytest.raises(GateRefusal, match="exactly the baseline project"):
        desktop_gate.verified_unit(unit_dir, repository)


def test_verified_unit_refuses_a_folder_that_is_not_a_run_unit(tmp_path: Path, repository: Path) -> None:
    stray = tmp_path / "loose" / "working"
    stray.mkdir(parents=True)
    (stray / "Demo.pbip").write_text("{}", encoding="utf-8")

    with pytest.raises(GateRefusal, match="run.json"):
        desktop_gate.verified_unit(stray.parent, repository)


def _repoint(unit_dir: Path, pbip: object = None, pbir: object = None) -> None:
    """Rewrite the working .pbip or definition.pbir; a str is written as is, anything else as JSON."""
    for path, content in (
        (unit_dir / "working" / "Demo.pbip", pbip),
        (unit_dir / "working" / "Demo.Report" / "definition.pbir", pbir),
    ):
        if content is not None:
            path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")


def _report(path: str) -> dict:
    return {**PBIP, "artifacts": [{"report": {"path": path}}]}


def _model(path: str) -> dict:
    return {**PBIR, "datasetReference": {"byPath": {"path": path}}}


@pytest.mark.parametrize(
    ("pbip", "pbir", "message"),
    [
        (_report("..\\baseline\\Demo.Report"), None, "outside the unit's working folder"),
        (_report("../baseline/Demo.Report"), None, "outside the unit's working folder"),
        (_report("../../report-two/working/Demo.Report"), None, "outside the unit's working folder"),
        (_report("C:\\elsewhere\\Demo.Report"), None, "absolute report path"),
        (_report("\\elsewhere\\Demo.Report"), None, "absolute report path"),
        (_report(""), None, "names no report path"),
        (_report("."), None, "outside the unit's working folder"),
        ({**PBIP, "artifacts": []}, None, "exactly one artifact"),
        ({**PBIP, "artifacts": [{"report": {"path": "Demo.Report"}}] * 2}, None, "exactly one artifact"),
        ({"version": "1.0"}, None, "exactly one artifact"),
        ("{not json", None, "UTF-8 JSON"),
        (None, _model("../../baseline/Demo.SemanticModel"), "outside the unit's working folder"),
        (None, _model("..\\..\\baseline\\Demo.SemanticModel"), "outside the unit's working folder"),
        (None, _model("C:/elsewhere/Demo.SemanticModel"), "absolute model path"),
        (None, {**PBIR, "datasetReference": {"byConnection": {"connectionString": "x"}}}, "no datasetReference.byPath"),
        (None, {"version": "4.0"}, "no datasetReference.byPath"),
        (None, _model(""), "names no model path"),
        (None, _model(".."), "outside the unit's working folder"),
    ],
)
def test_verified_unit_refuses_a_working_project_pointing_outside_working(
    unit_dir: Path, repository: Path, pbip, pbir, message: str
) -> None:
    _repoint(unit_dir, pbip, pbir)

    with pytest.raises(GateRefusal, match=message):
        desktop_gate.verified_unit(unit_dir, repository)


def test_verified_unit_refuses_a_report_without_definition_pbir(unit_dir: Path, repository: Path) -> None:
    (unit_dir / "working" / "Demo.Report" / "definition.pbir").unlink()

    with pytest.raises(GateRefusal, match="definition.pbir"):
        desktop_gate.verified_unit(unit_dir, repository)


def test_verified_unit_accepts_relative_pointers_that_stay_inside_working(unit_dir: Path, repository: Path) -> None:
    _repoint(unit_dir, _report("./Demo.Report"), _model("..\\Demo.SemanticModel"))

    assert desktop_gate.verified_unit(unit_dir, repository).key == "report-one"


@pytest.fixture
def junction(tmp_path: Path) -> Iterator:
    """Create directory junctions (no privilege needed on Windows); skip where they cannot be made.

    Each junction is removed (never followed) before tmp_path is torn down.
    """
    made: list[Path] = []

    def make(link: Path, target: Path) -> Path:
        assert not os.path.lexists(link), f"test setup: {link} already exists"
        assert target.is_dir(), f"test setup: {target} is not a folder"
        try:
            import _winapi

            _winapi.CreateJunction(str(target.resolve()), str(link))
        except (ImportError, AttributeError, OSError) as exc:
            pytest.skip(f"directory junctions cannot be created here: {exc}")
        made.append(link)
        return link

    yield make
    for link in reversed(made):
        if os.path.lexists(link):
            os.rmdir(link)


# --- is_within ---------------------------------------------------------------------------------------


def test_is_within_needs_a_separator_boundary(tmp_path: Path) -> None:
    run = tmp_path / "run"

    assert desktop_gate.is_within(run / "x", run)
    assert desktop_gate.is_within(run, run)
    assert not desktop_gate.is_within(tmp_path / "run2" / "x", run)
    assert not desktop_gate.is_within(tmp_path / "run2", run)


def test_is_within_ignores_case_on_either_side(tmp_path: Path) -> None:
    # Neither folder exists, so resolve() keeps the spelling given and only normcase can match them.
    assert desktop_gate.is_within(tmp_path / "MISSING-ROOT" / "Child", tmp_path / "missing-root")
    assert desktop_gate.is_within(tmp_path / "missing-root" / "child", tmp_path / "MISSING-ROOT")


# --- Desktop identity --------------------------------------------------------------------------------


def test_identity_accepts_desktop_opened_on_the_project_with_spaces_and_other_case(tmp_path: Path) -> None:
    pbip = tmp_path / "My Report" / "Demo.pbip"

    assert desktop_gate.verify_identity(_desktop(Path(str(pbip).upper())), pbip) is None


def test_identity_accepts_an_unquoted_absolute_project(tmp_path: Path) -> None:
    pbip = tmp_path / "Demo.pbip"
    instance = {"pid": 1, "name": "PBIDesktop.exe", "cmdline": f"C:\\PBI\\PBIDesktop.exe {pbip}", "ports": [1]}

    assert desktop_gate.verify_identity(instance, pbip) is None


@pytest.mark.parametrize(
    ("instance", "message"),
    [
        ({"pid": 1, "name": "notepad.exe", "cmdline": "x.pbip", "ports": [1]}, "not PBIDesktop.exe"),
        ({"pid": 1, "name": None, "cmdline": None, "ports": []}, "not running"),
        ({"pid": 1, "name": "PBIDesktop.exe", "cmdline": "PBIDesktop.exe", "ports": [1]}, "no .pbip"),
        ({"pid": 1, "name": "PBIDesktop.exe", "cmdline": '"C:\\other\\Demo.pbip"', "ports": [1]}, "not "),
    ],
)
def test_identity_refuses_anything_but_desktop_on_the_expected_project(
    tmp_path: Path, instance: dict, message: str
) -> None:
    assert message in (desktop_gate.verify_identity(instance, tmp_path / "Demo.pbip") or "")


def test_identity_refuses_a_command_line_naming_baseline_and_working(unit_dir: Path) -> None:
    working = unit_dir.resolve() / "working" / "Demo.pbip"
    baseline = unit_dir.resolve() / "baseline" / "Demo.pbip"
    for first, second in ((baseline, working), (working, baseline)):
        instance = {**_desktop(working), "cmdline": f'PBIDesktop.exe "{first}" "{second}"'}

        assert "names 2 .pbip arguments" in (desktop_gate.verify_identity(instance, working) or "")


@pytest.mark.parametrize("suffix", [".pbip.bak", "x", ".bak"])
def test_identity_refuses_a_name_that_only_starts_with_the_project(tmp_path: Path, suffix: str) -> None:
    pbip = tmp_path / "working" / "Demo.pbip"
    instance = {**_desktop(pbip), "cmdline": f"PBIDesktop.exe {pbip}{suffix}"}

    assert "is not a .pbip file" in (desktop_gate.verify_identity(instance, pbip) or "")


def test_identity_refuses_a_relative_project_even_from_its_folder(
    unit_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    working = unit_dir.resolve() / "working"
    monkeypatch.chdir(working)
    instance = {**_desktop(working / "Demo.pbip"), "cmdline": "PBIDesktop.exe Demo.pbip"}

    assert "relative path" in (desktop_gate.verify_identity(instance, working / "Demo.pbip") or "")


def test_identity_refuses_desktop_whose_engine_is_not_listening_yet(tmp_path: Path) -> None:
    pbip = tmp_path / "Demo.pbip"

    assert "no listening msmdsrv port" in desktop_gate.verify_identity({**_desktop(pbip), "ports": []}, pbip)


# --- inspecting the pid ------------------------------------------------------------------------------


def _powershell(monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", returncode: int = 0, raises=None) -> None:
    def run(cmd, **kwargs):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="boom")

    monkeypatch.setattr(desktop_gate.subprocess, "run", run)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"stdout": "WARNING: something\n"}, "no JSON"),
        ({"stdout": "[1, 2]"}, "not an object"),
        ({"stdout": '{"pid": 1, "name": 5, "cmdline": "x", "ports": []}'}, "name is not text"),
        ({"stdout": '{"pid": 1, "name": "x", "cmdline": "x", "ports": ["a"]}'}, "port list"),
        ({"stdout": "", "returncode": 1}, "boom"),
        ({"raises": subprocess.TimeoutExpired("powershell", 60)}, "did not answer"),
        ({"raises": FileNotFoundError("powershell")}, "could not inspect"),
    ],
)
def test_desktop_instance_turns_any_inspection_failure_into_a_refusal(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict, message: str
) -> None:
    _powershell(monkeypatch, **kwargs)

    with pytest.raises(GateRefusal, match=message):
        desktop_gate.desktop_instance(4242)


def test_desktop_instance_reads_a_single_port_as_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _powershell(monkeypatch, stdout='{"pid": 4242, "name": "PBIDesktop.exe", "cmdline": "x", "ports": 50000}')

    assert desktop_gate.desktop_instance(4242)["ports"] == [50000]


# --- read-only queries -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        'EVALUATE ROW("x", 1)',
        "EVALUATE {1}",
        "evaluate {1}",
        '  // note\n-- note\n/* block\n */ define measure T[x] = 1 evaluate ROW("x", [x])',
        'DEFINE VAR v = 1 MEASURE T[m] = v EVALUATE ROW("m", [m])',
        "EVALUATE {1} EVALUATE {2}",
        'EVALUATE ROW("x", "a;b")',
        "EVALUATE 'Odd;Table'",
        "EVALUATE {1} // trailing ; in a comment",
        "EVALUATE T[col;umn]",
        "EVALUATE T[a]];b]",  # "]]" escapes "]", so ";b" stays inside the column name
        "EVALUATE 'a'';b'[c]",
    ],
)
def test_read_only_accepts_dax_queries(text: str) -> None:
    assert desktop_gate.is_read_only(text)


@pytest.mark.parametrize(
    "text",
    [
        '{"refresh": {"type": "full", "objects": [{"database": "x"}]}}',
        f"// EVALUATE\n{TMSL}",
        "SELECT * FROM $SYSTEM.TMSCHEMA_TABLES",
        "SELECT 1 EVALUATE {1}",
        f"{TMSL} EVALUATE {{1}}",
        "<Batch><Delete/></Batch>",
        "EVALUATEX",
        "-- EVALUATE only in a comment",
        "",
        "DEFINE MEASURE T[x]=1",
        f"DEFINE {TMSL}",
        f"EVALUATE {{1}};{TMSL}",
        "EVALUATE {1};",
        f"// \r{TMSL} /*\nEVALUATE x */",
        f"// \u2028{TMSL} /*\nEVALUATE x */",
        f"// \u2029{TMSL} /*\nEVALUATE x */",
        f"-- \u0085{TMSL} /*\nEVALUATE x */",
        'EVALUATE ROW("x", "unterminated)',
        "EVALUATE {1} /* unterminated",
        'EVALUATE T[a]]"]; SELECT * FROM $SYSTEM.TMSCHEMA_TABLES // "',  # the quote is inside [a]]"]
    ],
)
def test_read_only_refuses_commands_batches_and_non_dax(text: str) -> None:
    assert not desktop_gate.is_read_only(text)


# --- data source -------------------------------------------------------------------------------------

PARAMETER_META = ' meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]'


def _accepted_sources(data: Path) -> list[str]:
    return [
        f'"{data}"{PARAMETER_META}',
        f'"{str(data).upper()}"{PARAMETER_META}',
        f'"{data}\\"{PARAMETER_META}',
        f'"{data}\\sub\\.."',
        f'/* folder */ "{data}" // the synthetic data\n',
        f'"{data}" meta [Note="a [bracket] text"]',
        f'// note\r"{data}"',
        f'// note\u0085"{data}"',
        f'"{data}" meta [IsParameterQuery=true, Type="Text", Description=""]',
        f'"{data}" meta [D = ""] // ] & "\\old"',
    ]


def test_data_source_accepts_a_text_parameter_naming_the_folder(tmp_path: Path) -> None:
    data = tmp_path / "data"
    for expression in _accepted_sources(data):
        assert desktop_gate.data_source_problem(expression, data) is None, expression


def test_data_source_ignores_a_trailing_separator_on_the_data_path(tmp_path: Path) -> None:
    data = tmp_path / "data"

    assert desktop_gate.data_source_problem(f'"{data}\\"', Path(f"{data}\\")) is None
    assert desktop_gate.data_source_problem(f'"{data}"', Path(f"{data}\\")) is None


@pytest.mark.parametrize("place", ["subfolder", "parent", "other", "prefix", "empty", "relative"])
def test_data_source_refuses_any_other_folder(tmp_path: Path, place: str) -> None:
    data = tmp_path / "data"
    literal = {
        "subfolder": f"{data}\\old",
        "parent": str(tmp_path),
        "other": "D:\\other",
        "prefix": f"{data}2",
        "empty": "",
        "relative": "data",
    }[place]

    assert "it names" in (desktop_gate.data_source_problem(f'"{literal}"{PARAMETER_META}', data) or "")


@pytest.mark.parametrize(
    "template",
    [
        'let /* was "{data}" */ p = "D:\\other" in p',
        'let a = "{data}", p = "D:\\other" in p',
        '"{data}" & ""',
        '"{data}" & "\\old"',
        'Csv.Document(File.Contents("{data}\\t.csv"))',
        '"{data}" "{data}"',
        '"{data}" meta [x = "a"] meta [y = 1]',
        '"{data}" /* unterminated',
        "{data}",
        '"{data}" // note\r& "\\old"',
        '"{data}" // note\u0085& "\\old"',
        '"{data}" // note\u2028& "\\old"',
        '"{data}" // note\u2029& "\\old"',
    ],
)
def test_data_source_refuses_anything_but_a_text_parameter(tmp_path: Path, template: str) -> None:
    data = tmp_path / "data"

    problem = desktop_gate.data_source_problem(template.replace("{data}", str(data)), data)

    assert "supports only a text parameter" in (problem or "")


# --- value normalisation -----------------------------------------------------------------------------

INVARIANT = object()


def _dotnet(kind: str, invariant_text: str, culture_text: str):
    """A stand-in for a pythonnet .NET value: invariant text only when asked with the invariant culture."""

    def to_string(self, *args):
        return invariant_text if args and args[-1] is INVARIANT else culture_text

    cls = type(kind, (), {"ToString": to_string, "__str__": lambda self: culture_text})
    cls.__module__ = "System"
    return cls()


@pytest.fixture
def invariant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop_gate, "invariant_culture", lambda: INVARIANT)


@pytest.mark.parametrize(
    ("kind", "invariant_text", "culture_text", "expected"),
    [
        ("Decimal", "936.5000", "936,5000", 936.5),
        ("Decimal", "-0.0100", "-0,0100", -0.01),
        ("Decimal", "79228162514264337593543950335", "79.228.162.514.264.337.593.543.950.335", 7.922816251426434e28),
        ("Double", "0.1", "0,1", 0.1),
        ("Single", "2.5", "2,5", 2.5),
        ("Int64", "1234567", "1.234.567", 1234567),
        ("Int32", "-5", "-5", -5),
    ],
)
def test_plain_reads_dotnet_numbers_through_the_invariant_culture(
    invariant, kind: str, invariant_text: str, culture_text: str, expected
) -> None:
    value = desktop_gate.plain(_dotnet(kind, invariant_text, culture_text))

    assert value == expected
    assert type(value) is type(expected)


def test_plain_decimal_passes_parity_against_the_oracle_number(invariant) -> None:
    assert parity_check.same(desktop_gate.plain(_dotnet("Decimal", "936.5000", "936,5000")), 936.5, 2)
    assert parity_check.same(desktop_gate.plain(_dotnet("Decimal", "936.5000", "936,5000")), 936.5, None)


def test_plain_formats_other_dotnet_values_with_the_invariant_culture(invariant) -> None:
    assert desktop_gate.plain(_dotnet("TimeSpan", "01:02:03.5000000", "01:02:03,5000000")) == "01:02:03.5000000"


def test_plain_falls_back_to_str_for_dotnet_values_without_a_culture_overload(invariant) -> None:
    cls = type("Guid", (), {"ToString": lambda self: "abc", "__str__": lambda self: "abc"})
    cls.__module__ = "System"

    assert desktop_gate.plain(cls()) == "abc"


def test_plain_refuses_unreadable_dotnet_number_text(invariant) -> None:
    with pytest.raises(desktop_gate.UnreadableValue, match="unreadable"):
        desktop_gate.plain(_dotnet("Decimal", "936,5000", "936,5000"))


def test_plain_keeps_python_values() -> None:
    dbnull = type("DBNull", (), {})()

    assert desktop_gate.plain(dbnull) is None
    assert desktop_gate.plain(None) is None
    assert desktop_gate.plain(3) == 3
    assert desktop_gate.plain("jan") == "jan"


def test_plain_reads_a_real_dotnet_decimal_under_a_comma_culture() -> None:
    pytest.importorskip("pythonnet")
    import clr  # noqa: F401  (loads the default .NET runtime)
    from System import Decimal as DotnetDecimal  # type: ignore[import-not-found]
    from System.Globalization import CultureInfo  # type: ignore[import-not-found]
    from System.Threading import Thread  # type: ignore[import-not-found]

    thread = Thread.CurrentThread
    previous = thread.CurrentCulture
    thread.CurrentCulture = CultureInfo("nl-BE")
    try:
        value = DotnetDecimal.Parse("936.5000", CultureInfo.InvariantCulture)
        assert "," in str(value)
        assert desktop_gate.plain(value) == 936.5
    finally:
        thread.CurrentCulture = previous


def test_plain_writes_a_real_dotnet_datetime_as_iso_text_under_any_culture() -> None:
    pytest.importorskip("pythonnet", reason="needs pythonnet: uv run --no-project --with pythonnet --with pytest")
    import clr  # noqa: F401  (loads the default .NET runtime)
    from System import DateTime  # type: ignore[import-not-found]
    from System.Globalization import CultureInfo  # type: ignore[import-not-found]
    from System.Threading import Thread  # type: ignore[import-not-found]

    thread = Thread.CurrentThread
    previous = thread.CurrentCulture
    thread.CurrentCulture = CultureInfo("nl-BE")
    try:
        value = DateTime(2024, 1, 31, 13, 45, 30, 250)
        assert desktop_gate.plain(value) == "2024-01-31T13:45:30.250000"
        assert desktop_gate.plain(DateTime(2024, 1, 31)) == "2024-01-31T00:00:00"
    finally:
        thread.CurrentCulture = previous


# --- oracle ------------------------------------------------------------------------------------------


def test_oracle_outside_run_and_checkout_loads_with_its_digest(
    tmp_path: Path, unit_dir: Path, repository: Path
) -> None:
    path = _oracle(tmp_path / "evidence")

    checks, digest = parity_check.load_oracle(path, unit_dir.parent.parent, repository)

    assert [c["id"] for c in checks] == ["by_month"]
    assert len(digest) == 64


@pytest.mark.parametrize("place", ["unit", "run", "checkout"])
def test_oracle_inside_the_run_or_checkout_is_refused(
    tmp_path: Path, unit_dir: Path, repository: Path, place: str
) -> None:
    folder = {"unit": unit_dir / "working", "run": unit_dir.parent.parent, "checkout": repository / "evidence"}[place]

    with pytest.raises(GateRefusal, match="outside the"):
        parity_check.load_oracle(_oracle(folder, name="oracle.json"), unit_dir.parent.parent, repository)


def test_a_hard_linked_oracle_is_refused(tmp_path: Path, unit_dir: Path, repository: Path) -> None:
    path = _oracle(tmp_path / "evidence")
    os.link(path, unit_dir / "captures-link.json")

    with pytest.raises(GateRefusal, match="hard links"):
        parity_check.load_oracle(path, unit_dir.parent.parent, repository)


@pytest.mark.parametrize(
    ("checks", "message"),
    [
        ([], "no checks"),
        ([CHECK, CHECK], "duplicated"),
        ([{**CHECK, "dax": '{"refresh": {"type": "full"}}'}], "read-only"),
        ([{**CHECK, "expected": {}, "exact_rows": False}], "could never fail"),
        ([{**CHECK, "expected": {"jan": []}}], "non-empty value lists"),
        ([{**CHECK, "expected": {"jan": [1], "feb": [1, 2]}}], "different numbers"),
        ([{**CHECK, "expected": {"jan": [[1]]}}], "finite numbers, text or null"),
        ([{**CHECK, "expected": {"jan": [{"v": 1}]}}], "finite numbers, text or null"),
        ([{**CHECK, "expected": {"jan": [True]}}], "finite numbers, text or null"),
        ([{**CHECK, "round": True}], "round"),
        ([{**CHECK, "round": -1}], "round"),
        ([{**CHECK, "keys": "Month"}], "keys"),
        ([{**CHECK, "keys": [1]}], "keys"),
        ([{**CHECK, "keys": [None]}], "keys"),
        ([{**CHECK, "id": " "}], "no id"),
        ([{**CHECK, "exact_rows": "yes"}], "exact_rows"),
    ],
)
def test_malformed_or_vacuous_oracles_are_refused(
    tmp_path: Path, unit_dir: Path, repository: Path, checks: list, message: str
) -> None:
    with pytest.raises(GateRefusal, match=message):
        parity_check.load_oracle(_oracle(tmp_path / "evidence", checks), unit_dir.parent.parent, repository)


EXPECTED = '"expected": {"jan": [1.5], "feb": [2.25]}'


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('{"checks": [], "checks": [{BASE, EXPECTED}]}', "repeats the key 'checks'"),
        ('{"checks": [{BASE, "expected": {"jan": [9], "jan": [1.5], "feb": [2.25]}}]}', "repeats the key 'jan'"),
        ('{"checks": [{BASE, EXPECTED, "round": 2, "round": null}]}', "repeats the key 'round'"),
        ('{"checks": [{BASE, "expected": {"jan": [NaN], "feb": [2.25]}}]}', "NaN"),
        ('{"checks": [{BASE, "expected": {"jan": [Infinity], "feb": [2.25]}}]}', "Infinity"),
        ('{"checks": [{BASE, "expected": {"jan": [-Infinity], "feb": [2.25]}}]}', "Infinity"),
        ('{"checks": [{BASE, "expected": {"jan": [1e400], "feb": [2.25]}}]}', "finite numbers"),
    ],
)
def test_oracle_text_that_json_would_read_loosely_is_refused(
    tmp_path: Path, unit_dir: Path, repository: Path, text: str, message: str
) -> None:
    base = json.dumps({k: v for k, v in CHECK.items() if k not in ("expected", "round")})[1:-1]
    path = _raw_oracle(tmp_path / "evidence", text.replace("BASE", base).replace("EXPECTED", EXPECTED))

    with pytest.raises(GateRefusal, match=message):
        parity_check.load_oracle(path, unit_dir.parent.parent, repository)


def test_oracle_raw_text_fixture_loads_when_well_formed(tmp_path: Path, unit_dir: Path, repository: Path) -> None:
    base = json.dumps({k: v for k, v in CHECK.items() if k not in ("expected", "round")})[1:-1]
    path = _raw_oracle(tmp_path / "evidence", '{"checks": [{' + base + ", " + EXPECTED + "}]}")

    assert parity_check.load_oracle(path, unit_dir.parent.parent, repository)[0][0]["expected"]["feb"] == [2.25]


def test_an_empty_expected_set_is_accepted_when_it_asserts_no_rows(
    tmp_path: Path, unit_dir: Path, repository: Path
) -> None:
    path = _oracle(tmp_path / "evidence", [{**CHECK, "expected": {}, "exact_rows": True}])

    assert parity_check.load_oracle(path, unit_dir.parent.parent, repository)[0][0]["expected"] == {}


def test_expected_values_may_be_numbers_text_or_null(tmp_path: Path, unit_dir: Path, repository: Path) -> None:
    path = _oracle(tmp_path / "evidence", [{**CHECK, "expected": {"jan": [1, 2.5, "x", None]}}])

    assert parity_check.load_oracle(path, unit_dir.parent.parent, repository)[0][0]["expected"]["jan"][3] is None


# --- comparison --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("actual", "expected", "digits", "result"),
    [
        (2.345, 2.345, 2, True),  # both sides round ties away from zero; mixed modes would disagree here
        (2.345, 2.35, 2, True),
        (2.344, 2.35, 2, False),
        # ties away from zero on the decimal text; float round() or half-even would get each of these wrong
        (2.675, 2.68, 2, True),
        (1.005, 1.01, 2, True),
        (0.5, 1, 0, True),
        (-0.5, -1, 0, True),
        (2.5, 3, 0, True),
        (2.5, 2, 0, False),
        (float("inf"), 1.5, 2, False),
        (float("-inf"), -1.5, 2, False),
        (float("nan"), 1.5, 2, False),
        (1.5, 1.5, 10**19, True),  # beyond the decimal context's maximum precision without the place cap
        ("jan", "feb", None, False),
        ("jan", "Jan", None, False),
        ("jan", "feb", 2, False),
        (10686, 10686.0, None, True),
        (1.0, 1.0000001, None, False),
        (None, None, None, True),
        (None, 0, None, False),
        ("jan", "jan", None, True),
        (True, 1, None, False),
        (1e30, 1e30, 2, True),
        (1e30, 2e30, 2, False),
        (1e-30, 0, 2, True),
        (1e300, 1e300, 10**6, True),
        (5e-324, 0.0, 400, False),
        (5e-324, 0.0, 10**6, False),
        (5e-10, 0.0, None, True),  # absolute 1e-9 below 1
        (2e-9, 0.0, None, False),
        (1000.0, 1000.0000005, None, True),  # relative 1e-9 above 1
        (1000.0, 1000.000002, None, False),
    ],
)
def test_same_compares_values_by_the_oracle_rules(actual, expected, digits, result) -> None:
    assert parity_check.same(actual, expected, digits) is result


def test_run_check_passes_matching_rows() -> None:
    assert parity_check.run_check(lambda _: [["jan", 1.499999], ["feb", 2.25]], CHECK) == []


def test_run_check_reports_missing_wrong_unexpected_and_duplicate_rows() -> None:
    problems = parity_check.run_check(lambda _: [["jan", 1.6], ["mar", 3.0], ["mar", 3.0]], CHECK)

    assert any(p.startswith("[jan] got") for p in problems)
    assert any(p.startswith("missing row [feb]") for p in problems)
    assert any(p.startswith("unexpected row [mar]") for p in problems)
    assert any(p.startswith("duplicate row [mar]") for p in problems)


def test_run_check_ignores_extra_rows_without_exact_rows() -> None:
    check = {**CHECK, "exact_rows": False}

    assert parity_check.run_check(lambda _: [["jan", 1.5], ["feb", 2.25], ["mar", 9]], check) == []


def test_run_check_joins_multiple_and_null_keys() -> None:
    check = {**CHECK, "keys": ["Day", "Hour"], "expected": {"mon|": [1], "|00-01": [2]}}

    assert parity_check.run_check(lambda _: [["mon", None, 1], [None, "00-01", 2]], check) == []


def test_run_check_turns_a_dax_error_into_a_failure() -> None:
    def broken(_: str) -> list[list]:
        raise RuntimeError("Column 'Month' cannot be found")

    assert parity_check.run_check(broken, CHECK) == ["DAX error: Column 'Month' cannot be found"]


def test_run_check_labels_an_unreadable_value_apart_from_a_dax_error() -> None:
    def unreadable(_: str) -> list[list]:
        raise desktop_gate.UnreadableValue("unreadable .NET Decimal text '936,5'")

    assert parity_check.run_check(unreadable, CHECK) == [
        "the query ran but a result value could not be read: unreadable .NET Decimal text '936,5'"
    ]


@pytest.mark.parametrize(
    "row",
    [["jan", 1.5], ["jan", 1.5, 2.0, 7.0], ["jan"]],
    ids=["fewer-values", "more-values", "no-values"],
)
def test_run_check_fails_a_row_whose_value_count_differs(row: list) -> None:
    check = {**CHECK, "expected": {"jan": [1.5, 2.0]}}

    assert parity_check.run_check(lambda _: [row], check) == [f"[jan] got {row[1:]} expected [1.5, 2.0]"]


def test_run_check_compares_text_values() -> None:
    check = {**CHECK, "expected": {"jan": ["open", 1.5]}}

    assert parity_check.run_check(lambda _: [["jan", "open", 1.5]], check) == []
    assert parity_check.run_check(lambda _: [["jan", "closed", 1.5]], check) == [
        "[jan] got ['closed', 1.5] expected ['open', 1.5]"
    ]


@pytest.mark.parametrize(
    ("value", "key"),
    [(2024.0, "2024"), (-3.0, "-3"), (0.0, "0"), (2024, "2024"), (2024.5, "2024.5"), (2.0**53, str(2.0**53))],
)
def test_row_key_writes_a_whole_number_float_like_an_integer(value: float, key: str) -> None:
    assert parity_check.row_key([value]) == key


def test_run_check_matches_a_decimal_year_key_with_the_oracle_text() -> None:
    check = {**CHECK, "keys": ["Year", "Month"], "expected": {"2024|jan": [1.5]}}

    assert parity_check.run_check(lambda _: [[2024.0, "jan", 1.5]], check) == []
    assert parity_check.run_check(lambda _: [[2024.5, "jan", 1.5]], check) == [
        "missing row [2024|jan] expected [1.5]",
        "unexpected row [2024.5|jan] = [1.5]",
    ]


# --- parity_check.main -------------------------------------------------------------------------------


def _fake_engine(patched: pytest.MonkeyPatch, unit_dir: Path, rows: list[list], data: Path | None = None):
    conn = FakeConnection()
    pbip = unit_dir.resolve() / "working" / "Demo.pbip"
    queries: list[str] = []

    def read(c, text):
        queries.append(text)
        if "TMSCHEMA_EXPRESSIONS" in text:
            return ["Name", "Expression"], [["DataFolder", f'"{data}"{PARAMETER_META}']]
        return ["Month", "V"], rows

    patched.setattr(desktop_gate, "desktop_instance", lambda pid: _desktop(pbip))
    patched.setattr(desktop_gate, "open_connection", lambda port, dll: conn)
    patched.setattr(desktop_gate, "read", read)
    conn.queries = queries
    return conn


def _parity(unit_dir: Path, oracle: Path, *extra: str) -> int:
    return parity_check.main(["--unit", str(unit_dir), "--pid", "4242", "--oracle", str(oracle), *extra])


def test_parity_main_passes_and_fails_by_exit_code(tmp_path: Path, unit_dir: Path, patched) -> None:
    oracle = _oracle(tmp_path / "evidence")

    conn = _fake_engine(patched, unit_dir, [["jan", 1.5], ["feb", 2.25]])
    assert _parity(unit_dir, oracle) == 0
    assert conn.closed

    _fake_engine(patched, unit_dir, [["jan", 1.5], ["feb", 9.0]])
    assert _parity(unit_dir, oracle) == 1


def test_parity_main_compares_huge_rounded_values_without_crashing(tmp_path: Path, unit_dir: Path, patched) -> None:
    oracle = _oracle(tmp_path / "evidence", [{**CHECK, "expected": {"jan": [1e30]}}])
    _fake_engine(patched, unit_dir, [["jan", 1e30]])

    assert _parity(unit_dir, oracle) == 0


def test_parity_main_refuses_desktop_serving_another_project_without_connecting(
    tmp_path: Path, unit_dir: Path, patched
) -> None:
    patched.setattr(desktop_gate, "desktop_instance", lambda pid: _desktop(unit_dir / "baseline" / "Demo.pbip"))
    patched.setattr(desktop_gate, "open_connection", lambda port, dll: pytest.fail("must not connect"))

    assert _parity(unit_dir, _oracle(tmp_path / "evidence")) == 2


@pytest.mark.parametrize(
    "kwargs",
    [{"stdout": "not json at all"}, {"raises": subprocess.TimeoutExpired("powershell", 60)}],
)
def test_parity_main_refuses_when_the_pid_cannot_be_inspected(
    tmp_path: Path, unit_dir: Path, patched, kwargs: dict
) -> None:
    _powershell(patched, **kwargs)
    patched.setattr(desktop_gate, "open_connection", lambda port, dll: pytest.fail("must not connect"))

    assert _parity(unit_dir, _oracle(tmp_path / "evidence")) == 2


def test_parity_main_refuses_an_invalid_oracle_before_touching_desktop(tmp_path: Path, unit_dir: Path, patched) -> None:
    patched.setattr(desktop_gate, "desktop_instance", lambda pid: pytest.fail("must not inspect Desktop"))

    assert _parity(unit_dir, _oracle(tmp_path / "evidence", [])) == 2


def test_parity_main_checks_a_model_reading_the_given_data(tmp_path: Path, unit_dir: Path, patched) -> None:
    data = tmp_path / "data"
    conn = _fake_engine(patched, unit_dir, [["jan", 1.5], ["feb", 2.25]], data)

    code = _parity(
        unit_dir, _oracle(tmp_path / "evidence"), "--data-expression", "DataFolder", "--data-path", str(data)
    )

    assert code == 0
    assert any("TMSCHEMA_EXPRESSIONS" in q for q in conn.queries)


def test_parity_main_refuses_a_model_reading_other_data(tmp_path: Path, unit_dir: Path, patched) -> None:
    conn = _fake_engine(patched, unit_dir, [["jan", 1.5], ["feb", 2.25]], tmp_path / "data" / "old")

    code = _parity(
        unit_dir,
        _oracle(tmp_path / "evidence"),
        "--data-expression",
        "DataFolder",
        "--data-path",
        str(tmp_path / "data"),
    )

    assert code == 2
    assert conn.closed
    assert conn.queries == ["SELECT [Name], [Expression] FROM $SYSTEM.TMSCHEMA_EXPRESSIONS"]


def test_parity_main_reports_an_unexpected_error_as_a_crash(tmp_path: Path, unit_dir: Path, patched, capsys) -> None:
    def boom(*args, **kwargs):
        raise KeyError("surprise")

    patched.setattr(parity_check, "load_oracle", boom)

    assert _parity(unit_dir, _oracle(tmp_path / "evidence")) == 3
    assert capsys.readouterr().err.strip() == "CRASHED: KeyError: 'surprise'"


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_parity_main_fails_a_non_finite_result_against_a_rounded_check(
    tmp_path: Path, unit_dir: Path, patched, capsys, value: float
) -> None:
    oracle = _oracle(tmp_path / "evidence", [{**CHECK, "round": 2}])
    _fake_engine(patched, unit_dir, [["jan", value], ["feb", 2.25]])

    assert _parity(unit_dir, oracle) == 1
    assert "CRASHED" not in capsys.readouterr().err


def test_parity_main_accepts_a_round_beyond_the_place_cap(tmp_path: Path, unit_dir: Path, patched, capsys) -> None:
    oracle = _oracle(tmp_path / "evidence", [{**CHECK, "round": 10**19}])
    _fake_engine(patched, unit_dir, [["jan", 1.5], ["feb", 2.25]])

    assert _parity(unit_dir, oracle) == 0
    assert "CRASHED" not in capsys.readouterr().err


@pytest.mark.parametrize("oracle", ["missing", "folder", "text-keys"])
def test_parity_main_refuses_an_unusable_oracle_as_a_refusal_not_a_crash(
    tmp_path: Path, unit_dir: Path, patched, capsys, oracle: str
) -> None:
    path = {
        "missing": tmp_path / "evidence" / "missing.json",
        "folder": tmp_path / "evidence",
        "text-keys": _oracle(tmp_path / "keys", [{**CHECK, "keys": [1]}]),
    }[oracle]
    (tmp_path / "evidence").mkdir(exist_ok=True)
    _fake_engine(patched, unit_dir, [["jan", 1.5], ["feb", 2.25]])

    assert _parity(unit_dir, path) == 2
    assert "CRASHED" not in capsys.readouterr().err


@pytest.mark.parametrize("guard", [["--data-expression", "DataFolder"], ["--data-path", "data"]])
def test_parity_main_refuses_half_a_data_guard_before_touching_desktop(
    tmp_path: Path, unit_dir: Path, patched, guard: list[str]
) -> None:
    patched.setattr(desktop_gate, "desktop_instance", lambda pid: pytest.fail("must not inspect Desktop"))

    with pytest.raises(SystemExit) as stopped:
        _parity(unit_dir, _oracle(tmp_path / "evidence"), *guard)

    assert stopped.value.code == 2


def _data_guard_failure(patched, unit_dir: Path, data: Path, failure: str) -> FakeConnection:
    """A fake engine whose expression DMV fails, or holds no DataFolder, while the data query would pass."""
    conn = _fake_engine(patched, unit_dir, [["jan", 1.5], ["feb", 2.25]], data)

    def read(c, text):
        conn.queries.append(text)
        if "TMSCHEMA_EXPRESSIONS" not in text:
            return ["Month", "V"], [["jan", 1.5], ["feb", 2.25]]
        if failure == "dmv-error":
            raise RuntimeError("The DMV is not available")
        return ["Name", "Expression"], [["OtherFolder", f'"{data}"{PARAMETER_META}']]

    patched.setattr(desktop_gate, "read", read)
    return conn


@pytest.mark.parametrize(
    ("failure", "expression"),
    [("dmv-error", "DataFolder"), ("no-expression", "DataFolder"), ("no-expression", "")],
)
def test_parity_main_refuses_when_the_data_source_cannot_be_proven(
    tmp_path: Path, unit_dir: Path, patched, capsys, failure: str, expression: str
) -> None:
    data = tmp_path / "data"
    conn = _data_guard_failure(patched, unit_dir, data, failure)

    code = _parity(unit_dir, _oracle(tmp_path / "evidence"), "--data-expression", expression, "--data-path", str(data))

    assert code == 2
    assert conn.queries == ["SELECT [Name], [Expression] FROM $SYSTEM.TMSCHEMA_EXPRESSIONS"]
    assert "CRASHED" not in capsys.readouterr().err


def test_require_data_source_names_a_missing_expression(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    rows = [["OtherFolder", f'"{data}"{PARAMETER_META}']]
    monkeypatch.setattr(desktop_gate, "read", lambda conn, text: (["Name", "Expression"], rows))

    with pytest.raises(GateRefusal, match="no shared expression named DataFolder"):
        desktop_gate.require_data_source(FakeConnection(), "DataFolder", data)
    desktop_gate.require_data_source(FakeConnection(), "OtherFolder", data)


def test_require_data_source_turns_a_dmv_error_into_a_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(conn, text):
        raise RuntimeError("The DMV is not available")

    monkeypatch.setattr(desktop_gate, "read", broken)

    with pytest.raises(GateRefusal, match="could not read the model's shared expressions: The DMV is not available"):
        desktop_gate.require_data_source(FakeConnection(), "DataFolder", tmp_path / "data")


# --- dax_query.main ----------------------------------------------------------------------------------


def _dax(unit_dir: Path, *extra: str) -> int:
    return dax_query.main(["--unit", str(unit_dir), "--pid", "4242", *extra])


def test_dax_query_refuses_a_command_before_verifying_anything(unit_dir: Path, patched) -> None:
    patched.setattr(desktop_gate, "verified_unit", lambda *a, **k: pytest.fail("must not verify"))

    assert _dax(unit_dir, "--dax", '{"refresh": {"type": "full"}}') == 2
    assert _dax(unit_dir, "--dax", f"EVALUATE {{1}};{TMSL}") == 2


def test_dax_query_prints_rows_and_reports_dax_errors(unit_dir: Path, patched, capsys) -> None:
    _fake_engine(patched, unit_dir, [["jan", 1.5]])
    assert _dax(unit_dir, "--dax", "EVALUATE T") == 0
    assert json.loads(capsys.readouterr().out) == {"Month": "jan", "V": 1.5}

    def broken(conn, text):
        raise RuntimeError("Syntax error")

    patched.setattr(desktop_gate, "read", broken)
    assert _dax(unit_dir, "--dax", "EVALUATE T") == 1


def test_dax_query_reads_a_query_file_and_refuses_a_missing_one(
    tmp_path: Path, unit_dir: Path, patched, capsys
) -> None:
    _fake_engine(patched, unit_dir, [["jan", 1.5]])
    query = tmp_path / "q.dax"
    query.write_text("EVALUATE T", encoding="utf-8")

    assert _dax(unit_dir, "--file", str(query)) == 0
    assert _dax(unit_dir, "--file", str(tmp_path / "missing.dax")) == 2
    assert "CRASHED" not in capsys.readouterr().err


def test_dax_query_refuses_desktop_on_another_project(tmp_path: Path, unit_dir: Path, patched) -> None:
    patched.setattr(desktop_gate, "desktop_instance", lambda pid: _desktop(tmp_path / "elsewhere" / "Demo.pbip"))
    patched.setattr(desktop_gate, "open_connection", lambda port, dll: pytest.fail("must not connect"))

    assert _dax(unit_dir, "--dax", "EVALUATE T") == 2


def test_dax_query_checks_the_data_source_before_querying(tmp_path: Path, unit_dir: Path, patched, capsys) -> None:
    data = tmp_path / "data"
    conn = _fake_engine(patched, unit_dir, [["jan", 1.5]], data / "old")
    guard = ["--data-expression", "DataFolder", "--data-path", str(data)]

    assert _dax(unit_dir, "--dax", "EVALUATE T", *guard) == 2
    assert conn.queries == ["SELECT [Name], [Expression] FROM $SYSTEM.TMSCHEMA_EXPRESSIONS"]
    assert capsys.readouterr().out == ""

    conn = _fake_engine(patched, unit_dir, [["jan", 1.5]], data)
    assert _dax(unit_dir, "--dax", "EVALUATE T", *guard) == 0
    assert conn.queries[-1] == "EVALUATE T"


def test_dax_query_reports_an_unexpected_error_as_a_crash(unit_dir: Path, patched, capsys) -> None:
    def boom(*args, **kwargs):
        raise LookupError("no such unit")

    patched.setattr(desktop_gate, "verified_unit", boom)

    assert _dax(unit_dir, "--dax", "EVALUATE T") == 3
    assert capsys.readouterr().err.strip() == "CRASHED: LookupError: no such unit"


@pytest.mark.parametrize("guard", [["--data-expression", "DataFolder"], ["--data-path", "data"]])
def test_dax_query_refuses_half_a_data_guard_before_touching_desktop(unit_dir: Path, patched, guard: list[str]) -> None:
    patched.setattr(desktop_gate, "desktop_instance", lambda pid: pytest.fail("must not inspect Desktop"))

    with pytest.raises(SystemExit) as stopped:
        _dax(unit_dir, "--dax", "EVALUATE T", *guard)

    assert stopped.value.code == 2


@pytest.mark.parametrize(
    ("failure", "expression"),
    [("dmv-error", "DataFolder"), ("no-expression", "DataFolder"), ("no-expression", "")],
)
def test_dax_query_refuses_when_the_data_source_cannot_be_proven(
    tmp_path: Path, unit_dir: Path, patched, capsys, failure: str, expression: str
) -> None:
    data = tmp_path / "data"
    conn = _data_guard_failure(patched, unit_dir, data, failure)

    code = _dax(unit_dir, "--dax", "EVALUATE T", "--data-expression", expression, "--data-path", str(data))

    assert code == 2
    assert conn.queries == ["SELECT [Name], [Expression] FROM $SYSTEM.TMSCHEMA_EXPRESSIONS"]
    output = capsys.readouterr()
    assert output.out == ""
    assert "CRASHED" not in output.err


def test_dax_query_keeps_every_column_of_a_repeated_name(unit_dir: Path, patched, capsys) -> None:
    _fake_engine(patched, unit_dir, [])
    names = ["a", "a", "a#2", "b", "a"]
    patched.setattr(desktop_gate, "read", lambda conn, text: (names, [[1, 2, 3, 4, 5]]))

    assert _dax(unit_dir, "--dax", "EVALUATE T") == 0
    assert json.loads(capsys.readouterr().out) == {"a": 1, "a#3": 2, "a#2": 3, "b": 4, "a#4": 5}


def test_dax_query_labels_an_unreadable_value_apart_from_a_dax_error(unit_dir: Path, patched, caplog) -> None:
    _fake_engine(patched, unit_dir, [])

    def unreadable(conn, text):
        raise desktop_gate.UnreadableValue("unreadable .NET Decimal text '936,5'")

    patched.setattr(desktop_gate, "read", unreadable)

    with caplog.at_level(logging.ERROR):
        assert _dax(unit_dir, "--dax", "EVALUATE T") == 1
    assert "the query ran but a result value could not be read: unreadable .NET Decimal text '936,5'" in caplog.text
    assert "DAX error" not in caplog.text


# --- output encoding: agents read the gates through a pipe -------------------------------------------

UNENCODABLE = "Omzet \u2192 Q1 \U0001f4c8"  # neither character exists in cp1252
DRIVER = """
import sys
sys.path.insert(0, {scripts!r})
import desktop_gate, dax_query, parity_check, capture_pages
from desktop_gate import GateRefusal, Unit
from pathlib import Path

TEXT = {text!r}


class Conn:
    def Close(self):
        pass


def refuse(*args):
    raise GateRefusal(TEXT)


unit = Unit(run_root=Path("run"), unit_dir=Path("run/units/u"), key="u", working_pbip=Path("Demo.pbip"))
desktop_gate.verified_unit = lambda *args: unit
desktop_gate.repository_root = lambda: Path("repo")
desktop_gate.connect_verified = lambda *args: (Conn(), {{"pid": 1, "ports": [50000]}})
desktop_gate.read = lambda conn, text: (["Month", TEXT], [["jan", TEXT]])
check = {{"id": "c", "dax": "EVALUATE T", "keys": ["Month"], "expected": {{"jan": [TEXT + "?"]}}, "title": TEXT}}
parity_check.load_oracle = lambda *args: ([check], "0" * 64)
if sys.argv[1] == "crash":
    def boom(*args):
        raise RuntimeError(TEXT)
    desktop_gate.verified_unit = boom
if sys.argv[1] == "capture":
    capture_pages.USER32 = object()
    desktop_gate.verified_unit = refuse
gate = {{"parity": parity_check, "capture": capture_pages}}.get(sys.argv[1], dax_query)
extra = {{"parity": ["--oracle", "o.json"], "capture": []}}.get(sys.argv[1], ["--dax", "EVALUATE T"])
sys.exit(gate.main(["--unit", "run/units/u", "--pid", "1", *extra]))
"""


@pytest.mark.parametrize(("gate", "code"), [("dax", 0), ("parity", 1), ("crash", 3), ("capture", 2)])
@pytest.mark.parametrize("encoding", ["cp1252", None])
def test_gates_print_server_text_through_a_cp1252_pipe(tmp_path: Path, gate: str, code: int, encoding) -> None:
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER.format(scripts=str(SCRIPTS), text=UNENCODABLE), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    if encoding is not None:
        env["PYTHONIOENCODING"] = encoding

    done = subprocess.run(
        [sys.executable, str(driver), gate], capture_output=True, env=env, cwd=tmp_path, timeout=60, check=False
    )

    out, err = done.stdout.decode("utf-8"), done.stderr.decode("utf-8")
    assert done.returncode == code, err
    if gate == "dax":
        assert json.loads(out) == {"Month": "jan", UNENCODABLE: UNENCODABLE}
    elif gate == "parity":
        assert f"got ['{UNENCODABLE}'] expected ['{UNENCODABLE}?']" in err
        assert any(line.startswith("FAIL") and line.endswith(UNENCODABLE) for line in err.splitlines())
    elif gate == "capture":
        assert err.strip() == f"refusing to capture: {UNENCODABLE}"
    else:
        assert err.strip() == f"CRASHED: RuntimeError: {UNENCODABLE}"
    assert "Logging error" not in err


# --- capture_pages: output folder and previous captures ----------------------------------------------


@pytest.mark.parametrize(
    ("place", "message"),
    [
        ("baseline", "inside the run root"),
        ("working", "inside the run root"),
        ("unit", "inside the run root"),
        ("captures", "inside the run root"),
        ("other-unit", "inside the run root"),
        ("run", "inside the run root"),
        ("checkout", "inside the checkout"),
    ],
)
def test_capture_output_inside_the_run_must_be_below_the_unit_captures(
    unit_dir: Path, repository: Path, place: str, message: str
) -> None:
    unit = desktop_gate.verified_unit(unit_dir, repository)
    target = {
        "baseline": unit_dir / "baseline" / "shots",
        "working": unit_dir / "working" / "shots",
        "unit": unit_dir / "shots",
        "captures": unit_dir / "captures",
        "other-unit": unit_dir.parent / "report-one-old" / "baseline" / "shots",
        "run": unit.run_root / "shots",
        "checkout": repository / "captures",
    }[place]

    with pytest.raises(GateRefusal, match=message):
        capture_pages.output_folder(unit, target, repository)


def test_capture_output_may_not_be_inside_another_run(tmp_path: Path, unit_dir: Path, repository: Path) -> None:
    unit = desktop_gate.verified_unit(unit_dir, repository)
    other = tmp_path / "other-run"
    (other / "units" / "x").mkdir(parents=True)
    (other / "run.json").write_text("{}", encoding="utf-8")

    with pytest.raises(GateRefusal, match="another migration run"):
        capture_pages.output_folder(unit, other / "units" / "x" / "shots", repository)


def test_capture_output_is_free_below_captures_and_outside_the_run(
    tmp_path: Path, unit_dir: Path, repository: Path
) -> None:
    unit = desktop_gate.verified_unit(unit_dir, repository)
    beside_run = Path(f"{unit.run_root}2") / "shots"  # shares the run root's name as a prefix only

    assert capture_pages.output_folder(unit, None, repository) == unit_dir.resolve() / "captures" / "working"
    validation = unit_dir / "captures" / "validation"
    assert capture_pages.output_folder(unit, validation, repository) == validation.resolve()
    assert capture_pages.output_folder(unit, beside_run, repository) == beside_run.resolve()
    assert capture_pages.output_folder(unit, tmp_path / "elsewhere", repository) == (tmp_path / "elsewhere").resolve()


def _project_folder(unit_dir: Path, name: str) -> Path:
    if name == "other-unit-baseline":
        folder = unit_dir.parent / "report-two" / "baseline"
        folder.mkdir(parents=True)
        return folder
    return unit_dir / name


@pytest.mark.parametrize("target", ["baseline", "working", "other-unit-baseline"])
def test_capture_output_refuses_a_captures_junction_into_a_project_folder(
    unit_dir: Path, repository: Path, junction, target: str
) -> None:
    unit = desktop_gate.verified_unit(unit_dir, repository)
    destination = _project_folder(unit_dir, target)
    before = sorted(destination.rglob("*"))
    junction(unit_dir / "captures", destination)

    for requested in (None, unit_dir / "captures" / "validation"):
        with pytest.raises(GateRefusal, match="link, junction or reparse point"):
            capture_pages.output_folder(unit, requested, repository)

    assert sorted(destination.rglob("*")) == before


def test_capture_output_refuses_a_junction_below_captures(tmp_path: Path, unit_dir: Path, repository: Path, junction):
    unit = desktop_gate.verified_unit(unit_dir, repository)
    (unit_dir / "captures").mkdir(exist_ok=True)
    (tmp_path / "elsewhere").mkdir()
    junction(unit_dir / "captures" / "validation", tmp_path / "elsewhere")

    with pytest.raises(GateRefusal, match="link, junction or reparse point in the run"):
        capture_pages.output_folder(unit, unit_dir / "captures" / "validation" / "shots", repository)


def test_capture_output_refuses_a_folder_the_captures_junction_would_allow(
    unit_dir: Path, repository: Path, junction
) -> None:
    unit = desktop_gate.verified_unit(unit_dir, repository)
    (unit_dir / "notes").mkdir()
    junction(unit_dir / "captures", unit_dir / "notes")

    with pytest.raises(GateRefusal, match="captures folder is a link, junction or reparse point"):
        capture_pages.output_folder(unit, unit_dir / "notes" / "shots", repository)


@pytest.mark.parametrize("target", ["baseline", "working", "other-unit-baseline"])
def test_capture_output_refuses_project_folders_even_when_a_link_goes_unnoticed(
    unit_dir: Path, repository: Path, junction, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    unit = desktop_gate.verified_unit(unit_dir, repository)
    junction(unit_dir / "captures", _project_folder(unit_dir, target))
    monkeypatch.setattr(desktop_gate, "reparse_point_on_path", lambda path, root: None)

    with pytest.raises(GateRefusal, match="inside a unit's baseline or working folder"):
        capture_pages.output_folder(unit, None, repository)


def test_reparse_point_on_path_ignores_links_above_the_root(tmp_path: Path, junction) -> None:
    (tmp_path / "real" / "run" / "inner").mkdir(parents=True)
    alias = junction(tmp_path / "alias", tmp_path / "real")

    assert desktop_gate.reparse_point_on_path(alias / "run" / "inner" / "new", alias / "run") is None
    assert desktop_gate.reparse_point_on_path(alias / "run" / "inner", tmp_path) == alias


def _previous_capture(out: Path, names: list[str], extra: dict[str, bytes] | None = None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name in names:
        (out / name).write_bytes(b"png")
    for name, content in (extra or {}).items():
        (out / name).write_bytes(content)
    record = {"status": "ok", "pages": [{"file": name, "bytes": 3, "suspect": True} for name in names]}
    (out / capture_pages.RECORD).write_text(json.dumps(record), encoding="utf-8")


def test_clear_previous_removes_only_the_recorded_capture(tmp_path: Path) -> None:
    out = tmp_path / "captures"
    _previous_capture(out, ["Page 1.png", "Page 2.png"])

    capture_pages.clear_previous(out)

    assert list(out.iterdir()) == []


def test_clear_previous_keeps_an_unlisted_png_and_deletes_nothing(tmp_path: Path) -> None:
    out = tmp_path / "captures"
    _previous_capture(out, ["Page1.png"], {"my-annotated-evidence.PNG": b"human"})

    with pytest.raises(GateRefusal, match="my-annotated-evidence.PNG"):
        capture_pages.clear_previous(out)

    assert sorted(p.name for p in out.iterdir()) == sorted(
        [capture_pages.RECORD, "Page1.png", "my-annotated-evidence.PNG"]
    )


@pytest.mark.parametrize("content", ["notes.txt", "subfolder", "no-record", "bad-record", "record-names-a-path"])
def test_clear_previous_refuses_folders_holding_other_files(tmp_path: Path, content: str) -> None:
    out = tmp_path / "captures"
    _previous_capture(out, ["Page 1.png"])
    if content == "notes.txt":
        (out / "notes.txt").write_text("keep me", encoding="utf-8")
    elif content == "subfolder":
        (out / "sub").mkdir()
    elif content == "no-record":
        (out / capture_pages.RECORD).unlink()
    elif content == "bad-record":
        (out / capture_pages.RECORD).write_text("{not json", encoding="utf-8")
    else:
        (out / capture_pages.RECORD).write_text(json.dumps({"pages": [{"file": "..\\Page 1.png"}]}), encoding="utf-8")

    with pytest.raises(GateRefusal):
        capture_pages.clear_previous(out)
    assert (out / "Page 1.png").exists()


@pytest.mark.parametrize("named", ["notes.txt", "notes.png.txt", "notes.pngx", "sub"])
def test_clear_previous_refuses_a_record_naming_anything_but_a_png(tmp_path: Path, named: str) -> None:
    out = tmp_path / "captures"
    _previous_capture(out, ["Page 1.png"])
    (out / named).write_text("keep me", encoding="utf-8")
    record = {"status": "ok", "pages": [{"file": "Page 1.png"}, {"file": named}]}
    (out / capture_pages.RECORD).write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(GateRefusal, match="no readable"):
        capture_pages.clear_previous(out)
    assert (out / named).read_text(encoding="utf-8") == "keep me"
    assert (out / "Page 1.png").exists()


def test_clear_previous_removes_a_recorded_png_in_any_case(tmp_path: Path) -> None:
    out = tmp_path / "captures"
    _previous_capture(out, ["Page 1.PNG", "Page 2.Png"])

    capture_pages.clear_previous(out)

    assert list(out.iterdir()) == []


@pytest.mark.parametrize("kind", ["symlink", "reported-symlink"])
def test_clear_previous_refuses_a_recorded_name_that_is_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    out = tmp_path / "captures"
    _previous_capture(out, ["Page 1.png", "Page 2.png"])
    if kind == "symlink":
        (tmp_path / "evidence.png").write_bytes(b"human")
        (out / "Page 2.png").unlink()
        try:
            (out / "Page 2.png").symlink_to(tmp_path / "evidence.png")
        except OSError as exc:
            pytest.skip(f"file symlinks cannot be created here: {exc}")
    else:
        real = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "Page 2.png" or real(self))

    with pytest.raises(GateRefusal, match="Page 2.png"):
        capture_pages.clear_previous(out)
    assert os.path.lexists(out / "Page 2.png")
    assert (out / "Page 1.png").exists()


@pytest.mark.parametrize("record", [None, "{not json", '{"pages": 1}'])
def test_clear_previous_refuses_files_without_a_usable_record(tmp_path: Path, record: str | None) -> None:
    out = tmp_path / "captures"
    out.mkdir()
    (out / "Page 1.png").write_bytes(b"png")
    if record is not None:
        (out / capture_pages.RECORD).write_text(record, encoding="utf-8")

    with pytest.raises(GateRefusal, match="no readable"):
        capture_pages.clear_previous(out)
    assert (out / "Page 1.png").exists()


@pytest.mark.parametrize("record", ["{not json", '{"pages": 1}', '{"pages": [{"file": "notes.txt"}]}'])
def test_clear_previous_keeps_an_unusable_record_on_its_own(tmp_path: Path, record: str) -> None:
    out = tmp_path / "captures"
    out.mkdir()
    (out / capture_pages.RECORD).write_text(record, encoding="utf-8")

    with pytest.raises(GateRefusal, match="no readable"):
        capture_pages.clear_previous(out)
    assert (out / capture_pages.RECORD).read_text(encoding="utf-8") == record


def test_clear_previous_accepts_an_empty_existing_folder(tmp_path: Path) -> None:
    out = tmp_path / "captures"
    out.mkdir()

    capture_pages.clear_previous(out)

    assert out.is_dir()


def test_expected_pages_refuses_a_report_without_pages_json(unit_dir: Path, repository: Path) -> None:
    unit = desktop_gate.verified_unit(unit_dir, repository)
    assert capture_pages.expected_pages(unit) == PAGES

    (unit_dir / "working" / "Demo.Report" / "definition" / "pages" / "pages.json").unlink()
    with pytest.raises(GateRefusal, match="pages.json"):
        capture_pages.expected_pages(unit)


# --- capture_pages.main ------------------------------------------------------------------------------


@pytest.fixture
def capture_env(unit_dir: Path, patched: pytest.MonkeyPatch):
    """Fake Desktop, window and CLI; returns the list of CLI calls and a setter for what the CLI does."""
    pbip = unit_dir.resolve() / "working" / "Demo.pbip"
    calls: list[list[str]] = []
    behaviour = {"pngs": {f"{name}.png": 20000 for name in PAGES}, "returncode": 0, "raises": None}

    def run(cmd, **kwargs):
        calls.append(cmd)
        if behaviour["raises"] is not None:
            raise behaviour["raises"]
        out = Path(cmd[cmd.index("--output-dir") + 1])
        for name, size in behaviour["pngs"].items():
            (out / name).write_bytes(b"\x89PNG" + b"0" * (size - 4))
        return subprocess.CompletedProcess(cmd, behaviour["returncode"], stdout="", stderr="cli said no")

    patched.setattr(capture_pages, "USER32", object())
    patched.setattr(desktop_gate, "desktop_instance", lambda pid: _desktop(pbip))
    patched.setattr(capture_pages.shutil, "which", lambda name: "powerbi-desktop.cmd")
    patched.setattr(capture_pages, "main_window", lambda pid: 77)
    patched.setattr(capture_pages, "resize_window", lambda hwnd, width, height: None)
    patched.setattr(capture_pages.time, "sleep", lambda seconds: None)
    patched.setattr(capture_pages.subprocess, "run", run)
    return calls, behaviour


def _capture(unit_dir: Path) -> tuple[int, dict]:
    code = capture_pages.main(["--unit", str(unit_dir), "--pid", "4242"])
    record_path = unit_dir / "captures" / "working" / capture_pages.RECORD
    return code, json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {}


def test_capture_main_succeeds_when_every_page_is_captured(unit_dir: Path, capture_env) -> None:
    calls, _ = capture_env

    code, record = _capture(unit_dir)

    assert code == 0
    assert record["status"] == "ok"
    assert record["expectedPages"] == 2
    assert [page["file"] for page in record["pages"]] == [f"{name}.png" for name in PAGES]
    assert calls[0][1] == "screenshot-all"


def test_capture_main_replaces_its_own_previous_capture(unit_dir: Path, capture_env) -> None:
    assert _capture(unit_dir)[0] == 0
    (unit_dir / "captures" / "working" / f"{PAGES[0]}.png").write_bytes(b"stale")

    code, record = _capture(unit_dir)

    assert code == 0
    assert record["status"] == "ok"
    assert (unit_dir / "captures" / "working" / f"{PAGES[0]}.png").stat().st_size == 20000


def test_capture_main_flags_small_pngs_as_suspect(unit_dir: Path, capture_env) -> None:
    _, behaviour = capture_env
    behaviour["pngs"] = {f"{name}.png": 100 for name in PAGES}

    code, record = _capture(unit_dir)

    assert code == 1
    assert record["status"] == "suspect"


@pytest.mark.parametrize("count", [1, 3])
def test_capture_main_fails_when_the_png_count_differs_from_the_pages(unit_dir: Path, capture_env, count) -> None:
    _, behaviour = capture_env
    behaviour["pngs"] = {f"page{i}.png": 20000 for i in range(count)}

    code, record = _capture(unit_dir)

    assert code == 1
    assert record["status"] == "incomplete"
    assert f"captured {count} PNGs but the working report has 2 pages" in record["reason"]


def test_capture_main_fails_a_report_without_pages(unit_dir: Path, capture_env) -> None:
    calls, _ = capture_env
    pages = unit_dir / "working" / "Demo.Report" / "definition" / "pages" / "pages.json"
    pages.write_text(json.dumps({"pageOrder": []}), encoding="utf-8")

    code, record = _capture(unit_dir)

    assert code == 1
    assert record["status"] == "failed"
    assert "lists no pages" in record["reason"]
    assert calls == []


def test_capture_main_refuses_a_report_whose_pages_cannot_be_read(unit_dir: Path, capture_env) -> None:
    calls, _ = capture_env
    (unit_dir / "working" / "Demo.Report" / "definition" / "pages" / "pages.json").write_text("{", encoding="utf-8")

    assert _capture(unit_dir) == (2, {})
    assert calls == []


@pytest.mark.parametrize("spec", [{"pages": 1}, {"pageOrder": [1]}, {"pageOrder": "ReportSection1"}, []])
def test_capture_main_refuses_a_page_list_without_page_names(unit_dir: Path, capture_env, capsys, spec) -> None:
    calls, _ = capture_env
    pages = unit_dir / "working" / "Demo.Report" / "definition" / "pages" / "pages.json"
    pages.write_text(json.dumps(spec), encoding="utf-8")

    assert _capture(unit_dir) == (2, {})
    assert calls == []
    assert "CRASHED" not in capsys.readouterr().err


def test_capture_main_refuses_an_out_that_is_a_file(tmp_path: Path, unit_dir: Path, capture_env, capsys) -> None:
    calls, _ = capture_env
    out = tmp_path / "shots.png"
    out.write_bytes(b"human")

    assert capture_pages.main(["--unit", str(unit_dir), "--pid", "4242", "--out", str(out)]) == 2
    assert out.read_bytes() == b"human"
    assert calls == []
    assert "CRASHED" not in capsys.readouterr().err


def test_capture_main_writes_into_an_empty_existing_out(tmp_path: Path, unit_dir: Path, capture_env) -> None:
    out = tmp_path / "shots"
    out.mkdir()

    assert capture_pages.main(["--unit", str(unit_dir), "--pid", "4242", "--out", str(out)]) == 0
    assert json.loads((out / capture_pages.RECORD).read_text(encoding="utf-8"))["status"] == "ok"


def test_capture_main_refuses_a_captures_junction_and_writes_nothing(unit_dir: Path, capture_env, junction) -> None:
    calls, _ = capture_env
    junction(unit_dir / "captures", unit_dir / "baseline")
    before = sorted(p.name for p in (unit_dir / "baseline").iterdir())

    assert capture_pages.main(["--unit", str(unit_dir), "--pid", "4242"]) == 2
    assert calls == []
    assert sorted(p.name for p in (unit_dir / "baseline").iterdir()) == before


def test_capture_main_records_an_interrupted_capture_and_the_next_run_replaces_it(
    unit_dir: Path, capture_env, patched
) -> None:
    run = capture_pages.subprocess.run

    def interrupted(cmd, **kwargs):
        (Path(cmd[cmd.index("--output-dir") + 1]) / f"{PAGES[0]}.png").write_bytes(b"\x89PNG" + b"0" * 20000)
        raise KeyboardInterrupt

    patched.setattr(capture_pages.subprocess, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _capture(unit_dir)
    record = json.loads((unit_dir / "captures" / "working" / capture_pages.RECORD).read_text(encoding="utf-8"))
    assert record["status"] == "interrupted"
    assert [page["file"] for page in record["pages"]] == [f"{PAGES[0]}.png"]

    patched.setattr(capture_pages.subprocess, "run", run)
    code, record = _capture(unit_dir)

    assert code == 0
    assert record["status"] == "ok"


@pytest.mark.parametrize(
    ("returncode", "raises", "reason"),
    [
        (1, None, "screenshot-all exit 1: cli said no"),
        (0, subprocess.TimeoutExpired("powerbi-desktop", 300), "did not finish within"),
        (0, FileNotFoundError("powerbi-desktop.cmd"), "could not be started"),
    ],
)
def test_capture_main_fails_when_the_cli_fails(unit_dir: Path, capture_env, returncode, raises, reason) -> None:
    _, behaviour = capture_env
    behaviour.update(returncode=returncode, raises=raises)

    code, record = _capture(unit_dir)

    assert code == 1
    assert record["status"] == "failed"
    assert reason in record["reason"]


def test_capture_main_fails_without_a_window(unit_dir: Path, capture_env, patched) -> None:
    patched.setattr(capture_pages, "main_window", lambda pid: None)

    code, record = _capture(unit_dir)

    assert code == 1
    assert record["status"] == "failed"


def test_capture_main_reports_an_unexpected_error_as_a_crash(unit_dir: Path, capture_env, capsys) -> None:
    _, behaviour = capture_env
    behaviour["raises"] = RuntimeError("bridge exploded")

    code, record = _capture(unit_dir)

    assert code == 3
    assert record["status"] == "crashed"
    assert "bridge exploded" in record["reason"]
    assert "CRASHED: RuntimeError: bridge exploded" in capsys.readouterr().err


def test_capture_main_refuses_another_desktop_and_keeps_the_previous_capture(
    tmp_path: Path, unit_dir: Path, patched
) -> None:
    out = tmp_path / "captures"
    _previous_capture(out, ["Page 1.png"])
    patched.setattr(capture_pages, "USER32", object())
    patched.setattr(desktop_gate, "desktop_instance", lambda pid: _desktop(tmp_path / "elsewhere" / "Demo.pbip"))

    code = capture_pages.main(["--unit", str(unit_dir), "--pid", "4242", "--out", str(out)])

    assert code == 2
    assert (out / "Page 1.png").exists()
