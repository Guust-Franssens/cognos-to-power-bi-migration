"""
purpose: Shared guard for the Power BI Desktop gates (parity_check.py, dax_query.py, capture_pages.py).
         Before anything is queried or captured it proves that the report unit still matches its parent
         run.json and that the given PBIDesktop.exe pid was opened on that unit's working .pbip: the
         pid's command line must name exactly one .pbip argument, as an absolute path, and that path
         must be the working .pbip (case-insensitive). The working .pbip's report and that report's
         definition.pbir byPath model must both resolve inside the unit's working folder, so the .pbip
         cannot point Desktop at the baseline or another unit. It also holds the read-only query check, the
         data-source check (a shared M expression that is one text literal naming the data folder), the
         ADOMD.NET access and the culture-independent value normalisation the gates share. Not a CLI.
usage:   imported by the gate scripts in this folder.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PureWindowsPath

from create_migration_workspace import _is_reparse_point, repository_root, verify_unit

DEFAULT_ADOMD = Path(
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\DAX Studio\bin\Microsoft.AnalysisServices.AdomdClient.dll")
)
INSPECT_TIMEOUT_S = 60
# One command-line argument: a run of non-blank characters in which double-quoted parts may hold blanks.
ARGUMENT_RE = re.compile(r'(?:"[^"]*"?|[^\s"])+')
# Line comments (DAX // and --, M //) end at any of these line terminators.
LINE_BREAKS = "\n\r\u0085\u2028\u2029"
# A shared M expression in text-parameter shape: one string literal ("" escapes a quote), optionally
# followed by a meta record whose text holds no brackets outside its own string literals.
M_TEXT_PARAMETER_RE = re.compile(r'\s*"((?:[^"]|"")*)"\s*(?:meta\s*\[(?:[^\[\]"]|"(?:[^"]|"")*")*\]\s*)?', re.DOTALL)
DOTNET_INTEGERS = {"Byte", "SByte", "Int16", "UInt16", "Int32", "UInt32", "Int64", "UInt64"}
DOTNET_REALS = {"Decimal", "Double", "Single"}
# One PowerShell round trip: the pid's image name and command line, and the ports its msmdsrv child listens on.
INSTANCE_PS = r"""
$ErrorActionPreference = 'Stop'
$proc = Get-CimInstance Win32_Process -Filter "ProcessId={pid}"
$ports = @()
foreach ($engine in @(Get-CimInstance Win32_Process -Filter "ParentProcessId={pid} AND Name='msmdsrv.exe'")) {{
  $ports += @(Get-NetTCPConnection -OwningProcess $engine.ProcessId -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty LocalPort -Unique)
}}
@{{ pid = {pid}; name = $proc.Name; cmdline = $proc.CommandLine; ports = @($ports) }} | ConvertTo-Json -Compress
"""


class GateRefusal(RuntimeError):
    """The gate cannot prove what it would check; callers exit 2 and check nothing."""


class UnreadableValue(ValueError):
    """A result value the query returned could not be normalised; the query ran, its result is unusable."""


@dataclass(frozen=True)
class Unit:
    run_root: Path
    unit_dir: Path
    key: str
    working_pbip: Path


def utf8_output() -> None:
    """Write stdout and stderr as UTF-8, so model, page and server text never fails on a cp1252 pipe."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


def crashed(exc: BaseException) -> int:
    """Report an unexpected error on one stderr line and return the gates' crash exit code, 3."""
    message = " ".join(str(exc).split())
    print(f"CRASHED: {type(exc).__name__}: {message}", file=sys.stderr)
    return 3


def is_within(path: Path, root: Path) -> bool:
    """True when path is root or below it, compared case-insensitively on Windows."""
    child = os.path.normcase(str(path.resolve()))
    parent = os.path.normcase(str(root.resolve()))
    return child == parent or child.startswith(parent.rstrip("\\/") + os.sep)


def reparse_point_on_path(path: Path, root: Path) -> Path | None:
    """The first existing component of path that lies inside root and is a link, junction or other reparse point.

    Components are taken from the absolute path as written, top down, without following any of them;
    a component counts as inside root when the folder holding it resolves inside root.
    """
    absolute = Path(os.path.abspath(path))
    for component in reversed([absolute, *absolute.parents]):
        if not os.path.lexists(component):
            return None
        if component.parent != component and is_within(component.parent, root) and _is_reparse_point(component):
            return component
    return None


def _project_pointer(folder: Path, document: Path, kind: str, relative: object, working: Path) -> Path:
    """Resolve a relative project pointer and refuse one that leaves the unit's working folder."""
    if not isinstance(relative, str) or not relative.strip():
        raise GateRefusal(f"{document} names no {kind} path")
    if PureWindowsPath(relative).drive or PureWindowsPath(relative).root:
        raise GateRefusal(
            f"{document} names the absolute {kind} path {relative}; only a path inside working is allowed"
        )
    target = (folder / relative).resolve()
    if not is_within(target, working) or os.path.normcase(str(target)) == os.path.normcase(str(working.resolve())):
        raise GateRefusal(f"{document} points its {kind} at {target}, outside the unit's working folder")
    return target


def _json_document(path: Path) -> dict:
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateRefusal(f"cannot read {path} as UTF-8 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GateRefusal(f"{path} is not a JSON object")
    return data


def require_working_pointers(pbip: Path, working: Path) -> None:
    """Refuse unless the .pbip's one report, and that report's byPath model, both lie inside working."""
    artifacts = _json_document(pbip).get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 1 or not isinstance(artifacts[0], dict):
        raise GateRefusal(f"{pbip} must list exactly one artifact, its report")
    report = artifacts[0].get("report")
    relative = report.get("path") if isinstance(report, dict) else None
    report_dir = _project_pointer(pbip.parent, pbip, "report", relative, working)
    pbir = report_dir / "definition.pbir"
    reference = _json_document(pbir).get("datasetReference")
    by_path = reference.get("byPath") if isinstance(reference, dict) else None
    if not isinstance(by_path, dict):
        raise GateRefusal(f"{pbir} has no datasetReference.byPath; only a local model in working can be checked")
    _project_pointer(report_dir, pbir, "model", by_path.get("path"), working)


def verified_unit(unit_dir: Path, repo_root: Path | None = None) -> Unit:
    """Verify the unit against its parent run.json and return it with its single working .pbip."""
    repository = repo_root if repo_root is not None else repository_root()
    try:
        verify_unit(unit_dir, repository)
        unit = unit_dir.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise GateRefusal(f"unit failed verification against its run.json: {exc}") from exc
    expected = sorted(path.name for path in (unit / "baseline").glob("*.pbip"))
    if len(expected) != 1:
        raise GateRefusal(f"baseline must hold exactly one .pbip, found {len(expected)}")
    found = sorted((unit / "working").glob("*.pbip"))
    if [path.name.casefold() for path in found] != [expected[0].casefold()]:
        raise GateRefusal(
            f"working must hold exactly the baseline project {expected[0]}, found {[p.name for p in found]}"
        )
    require_working_pointers(found[0], unit / "working")
    return Unit(run_root=unit.parent.parent, unit_dir=unit, key=unit.name, working_pbip=found[0])


def desktop_instance(pid: int) -> dict:
    """Image name, command line and msmdsrv listening ports of a process, read through PowerShell.

    Any failure to inspect the pid (PowerShell missing, timing out, failing or printing something other
    than the expected JSON object) is a GateRefusal: nothing about the pid could be proven.
    """
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", INSTANCE_PS.format(pid=int(pid))],
            capture_output=True,
            text=True,
            timeout=INSPECT_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GateRefusal(f"could not inspect pid {pid}: PowerShell did not answer within {exc.timeout} s") from exc
    except OSError as exc:
        raise GateRefusal(f"could not inspect pid {pid}: {exc}") from exc
    if done.returncode != 0 or not (done.stdout or "").strip():
        raise GateRefusal(f"could not inspect pid {pid}: {(done.stderr or done.stdout or '').strip()[-400:]}")
    try:
        instance = json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise GateRefusal(
            f"could not inspect pid {pid}: PowerShell printed no JSON ({done.stdout.strip()[:200]})"
        ) from exc
    if not isinstance(instance, dict):
        raise GateRefusal(f"could not inspect pid {pid}: PowerShell printed {type(instance).__name__}, not an object")
    ports = instance.get("ports")
    ports = [] if ports is None else [ports] if isinstance(ports, int) else ports
    if not isinstance(ports, list) or not all(isinstance(p, int) and not isinstance(p, bool) for p in ports):
        raise GateRefusal(f"could not inspect pid {pid}: unexpected port list {instance.get('ports')!r}")
    for field in ("name", "cmdline"):
        if instance.get(field) is not None and not isinstance(instance[field], str):
            raise GateRefusal(f"could not inspect pid {pid}: {field} is not text")
    instance["ports"] = ports
    return instance


def pbip_arguments(cmdline: str) -> list[str]:
    """Command-line arguments (quotes removed) that mention .pbip anywhere."""
    arguments = [token.replace('"', "") for token in ARGUMENT_RE.findall(cmdline)]
    return [argument for argument in arguments if ".pbip" in argument.casefold()]


def verify_identity(instance: dict, expected_pbip: Path) -> str | None:
    """Return why the instance is not Desktop serving expected_pbip, or None when it is."""
    pid = instance.get("pid")
    if (instance.get("name") or "").casefold() != "pbidesktop.exe":
        return f"pid {pid} is {instance.get('name') or 'not running'}, not PBIDesktop.exe"
    opened = pbip_arguments(instance.get("cmdline") or "")
    if not opened:
        return (
            f"pid {pid} has no .pbip on its command line, so its file cannot be proven; "
            f'reopen it with Start-Process "{expected_pbip}"'
        )
    if len(opened) > 1:
        return f"pid {pid} names {len(opened)} .pbip arguments ({', '.join(opened)}), so its file cannot be proven"
    argument = opened[0]
    if not argument.casefold().endswith(".pbip"):
        return f"pid {pid} argument {argument} is not a .pbip file"
    if not PureWindowsPath(argument).is_absolute():
        return f"pid {pid} opened the relative path {argument}, so its file cannot be proven"
    target = os.path.normcase(os.path.normpath(str(expected_pbip.resolve())))
    if os.path.normcase(os.path.normpath(str(Path(argument).resolve()))) != target:
        return f"pid {pid} has {argument} open, not {expected_pbip}"
    if not instance.get("ports"):
        return f"pid {pid} has no listening msmdsrv port yet (still loading?)"
    return None


def require_desktop(pid: int, expected_pbip: Path) -> dict:
    instance = desktop_instance(pid)
    problem = verify_identity(instance, expected_pbip)
    if problem:
        raise GateRefusal(problem)
    return instance


def open_connection(port: int, adomd_dll: Path):
    """Open an ADOMD.NET connection to Desktop's local engine (pythonnet and DAX Studio's client DLL)."""
    if not adomd_dll.is_file():
        raise GateRefusal(f"ADOMD.NET client not found at {adomd_dll}; install DAX Studio or pass --adomd")
    try:
        from pythonnet import load
    except ImportError as exc:
        raise GateRefusal("pythonnet is not installed; run the gate with `uv run --with pythonnet python ...`") from exc
    try:
        load("netfx")
        import clr  # noqa: E402  (pythonnet must be loaded first)

        clr.AddReference(str(adomd_dll))
        from Microsoft.AnalysisServices.AdomdClient import AdomdConnection  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001  (the CLR host and assembly loader raise varied errors)
        raise GateRefusal(f"could not load the ADOMD.NET client {adomd_dll}: {dotnet_message(exc)}") from exc

    conn = AdomdConnection(f"Data Source=localhost:{port}")
    try:
        conn.Open()
    except Exception as exc:  # noqa: BLE001  (ADOMD raises .NET exceptions)
        raise GateRefusal(f"could not connect to localhost:{port}: {dotnet_message(exc)}") from exc
    return conn


def connect_verified(unit: Unit, pid: int, adomd_dll: Path):
    """Prove the pid serves the unit's working .pbip, then connect to its engine."""
    instance = require_desktop(pid, unit.working_pbip)
    return open_connection(instance["ports"][0], adomd_dll), instance


def dotnet_message(exc: BaseException) -> str:
    message = getattr(exc, "Message", None) or str(exc)
    return message.strip().splitlines()[0] if message.strip() else type(exc).__name__


def _blank_dax_noise(text: str) -> str | None:
    """Replace DAX comments, strings, 'table' and [column] names with a blank; None if one is unterminated."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        pair = text[i : i + 2]
        if pair in ("//", "--"):
            i += 2
            while i < n and text[i] not in LINE_BREAKS:
                i += 1
            out.append(" ")
        elif pair == "/*":
            end = text.find("*/", i + 2)
            if end < 0:
                return None
            out.append(" ")
            i = end + 2
        elif text[i] in "\"'[":
            close = "]" if text[i] == "[" else text[i]
            end = i + 1
            while True:
                end = text.find(close, end)
                if end < 0:
                    return None
                if text[end + 1 : end + 2] != close:
                    break
                end += 2  # a doubled closing character is an escaped one
            out.append(" ")
            i = end + 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def is_read_only(text: str) -> bool:
    """True for one DAX query (EVALUATE or DEFINE ... EVALUATE); false for TMSL, XMLA, DMV, MDX or a batch.

    Outside comments and literals the text must start with EVALUATE or DEFINE, hold at least one
    EVALUATE and hold no ';'. An unterminated comment or literal is refused.
    """
    code = _blank_dax_noise(text)
    if code is None or ";" in code:
        return False
    if re.match(r"\s*(?:EVALUATE|DEFINE)\b", code, re.IGNORECASE) is None:
        return False
    return re.search(r"\bEVALUATE\b", code, re.IGNORECASE) is not None


def invariant_culture():
    """.NET's CultureInfo.InvariantCulture (pythonnet is already loaded when a .NET value exists)."""
    from System.Globalization import CultureInfo  # type: ignore[import-not-found]

    return CultureInfo.InvariantCulture


def _dotnet_type(value) -> str | None:
    kind = type(value)
    return kind.__name__ if getattr(kind, "__module__", None) == "System" else None


def plain(value):
    """Normalise an ADOMD value to a JSON value without ever depending on the current culture.

    .NET integers become int; System.Decimal, Double and Single become float, parsed from their
    invariant-culture text (so 936.5000 compares equal to an oracle's 936.5); DateTime becomes ISO 8601
    text. Any other .NET value is formatted with the invariant culture when it accepts one. Number text
    that cannot be parsed raises UnreadableValue.
    """
    if value is None or type(value).__name__ == "DBNull":
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    kind = _dotnet_type(value)
    if kind == "DateTime":
        return dt.datetime(
            value.Year, value.Month, value.Day, value.Hour, value.Minute, value.Second, value.Millisecond * 1000
        ).isoformat()
    if kind in DOTNET_INTEGERS:
        return int(value.ToString(invariant_culture()))
    if kind in DOTNET_REALS:
        text = value.ToString(invariant_culture()) if kind == "Decimal" else value.ToString("R", invariant_culture())
        try:
            return float(Decimal(text))
        except InvalidOperation as exc:
            raise UnreadableValue(f"unreadable .NET {kind} text {text!r}") from exc
    if kind is not None:
        try:
            return str(value.ToString(invariant_culture()))
        except Exception:  # noqa: BLE001  (not IFormattable: its ToString takes no culture)
            return str(value)
    return str(value)


def read(conn, text: str) -> tuple[list[str], list[list]]:
    """Run one query and return its column names and normalised rows."""
    cmd = conn.CreateCommand()
    cmd.CommandText = text
    reader = cmd.ExecuteReader()
    try:
        names = [reader.GetName(i) for i in range(reader.FieldCount)]
        rows = []
        while reader.Read():
            rows.append([plain(reader.GetValue(i)) for i in range(reader.FieldCount)])
    finally:
        reader.Close()
    return names, rows


def _m_without_comments(expression: str) -> str | None:
    """Drop M comments (// to a line break, /* */) outside string literals; None if one is unterminated."""
    out: list[str] = []
    i, n = 0, len(expression)
    while i < n:
        pair = expression[i : i + 2]
        if pair == "//":
            i += 2
            while i < n and expression[i] not in LINE_BREAKS:
                i += 1
            out.append(" ")
        elif pair == "/*":
            end = expression.find("*/", i + 2)
            if end < 0:
                return None
            out.append(" ")
            i = end + 2
        elif expression[i] == '"':
            end = i + 1
            while True:
                end = expression.find('"', end)
                if end < 0:
                    return None
                if expression[end + 1 : end + 2] != '"':
                    break
                end += 2
            out.append(expression[i : end + 1])
            i = end + 1
        else:
            out.append(expression[i])
            i += 1
    return "".join(out)


def _normal_path(text: str) -> str:
    return os.path.normcase(os.path.normpath(text))


def data_source_problem(expression: str, data_path: Path) -> str | None:
    """Return why a shared M expression is not a text parameter naming data_path, or None when it is.

    Only the text-parameter shape is supported: after M comments are removed the expression is exactly
    one string literal, optionally followed by meta [...]. Its path must equal data_path after
    normalisation (case-insensitive on Windows, trailing separators, '.' and '..'); a subfolder, a
    parent or another folder is refused.
    """
    code = _m_without_comments(expression)
    match = M_TEXT_PARAMETER_RE.fullmatch(code) if code is not None else None
    if match is None:
        return (
            "the data-source guard supports only a text parameter (one M string literal, optionally with "
            f"meta [...]), not: {expression[:200]}"
        )
    literal = match.group(1).replace('""', '"')
    if not literal or _normal_path(literal) != _normal_path(str(data_path.resolve())):
        return f"it names {literal!r}, not {data_path.resolve()}"
    return None


def require_data_source(conn, expression_name: str, data_path: Path) -> None:
    """Refuse unless the model's named shared M expression is a text parameter naming data_path."""
    try:
        _, rows = read(conn, "SELECT [Name], [Expression] FROM $SYSTEM.TMSCHEMA_EXPRESSIONS")
    except Exception as exc:  # noqa: BLE001  (ADOMD raises .NET exceptions)
        raise GateRefusal(f"could not read the model's shared expressions: {dotnet_message(exc)}") from exc
    expression = next((text for name, text in rows if name == expression_name), None)
    if expression is None:
        raise GateRefusal(f"the model has no shared expression named {expression_name}")
    problem = data_source_problem(str(expression), data_path)
    if problem:
        raise GateRefusal(f"{expression_name} does not point at {data_path.resolve()}: {problem}")
