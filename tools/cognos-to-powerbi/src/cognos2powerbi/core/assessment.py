"""Estate-level assessment of Cognos report specifications, before migrating.

Parses every report specification found in a set of files and folders with the report parser only
(no Power BI output is generated, so it is fast), then aggregates the review flags so a team can
see which Cognos features dominate the estate and which reports need the most manual work. Each
report gets a readiness tier from a fixed, documented rule (see :func:`assign_tier`); the results
are written as ``assessment.json``, ``assessment.md`` and ``assessment.csv``.

Only report specifications are assessed. Other detected sources (Framework Manager models, data
modules, dashboards) and files that are not report specifications are listed as skipped.
"""

from __future__ import annotations

import codecs
import csv
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from cognos2powerbi import __version__
from cognos2powerbi.core.batch import collect_sources
from cognos2powerbi.core.detect import SourceKind, detect_source_kind
from cognos2powerbi.core.ir.models import MigrationProject, ReviewFlag, Severity
from cognos2powerbi.core.parsers import CognosReportParser


class ReadinessTier(str, Enum):
    """How much manual work a report needs after conversion, from its review flags."""

    READY = "ready"
    REVIEW = "review"
    REWORK = "rework"
    NO_DATA = "no-data"


# Flags that never change a report's tier. ``package-source`` is raised once for every report that
# reads a Cognos package, which is nearly every report, and its action (point the generated
# partitions at the real tables or views) is done once per data source, not once per report.
TIER_IGNORED_CODES = frozenset({"package-source"})

# Flags whose message says an object on a report page "was not migrated": the Power BI page lacks
# something the Cognos page shows, so it has to be rebuilt by hand. Their severity is WARNING, so
# without this list they would only put a report in the review tier.
NOT_MIGRATED_CODES = frozenset(
    {
        "custom-control",
        "html-item",
        "layout-component-reference",
        "layout-object-not-migrated",
        "report-page-prompt-control",
    }
)

# A report without queries has no data to migrate (typically a style template, an instructions
# page or a JavaScript demo). The parser rates it an ERROR, but it is a retire-or-rebuild decision
# rather than rework, so it gets a tier of its own.
NO_DATA_CODE = "no-queries"

TIER_MEANINGS: dict[str, str] = {
    ReadinessTier.READY.value: (
        "No error or warning flags apart from the allow-listed ones; review the generated report "
        "as usual."
    ),
    ReadinessTier.REVIEW.value: (
        "Every report-page object was generated, but warnings need manual completion or checking, "
        "for example detail filters to recreate (figures differ until they are), calculations to "
        "translate, or formats to set."
    ),
    ReadinessTier.REWORK.value: (
        "At least one report-page object was not generated (a custom control, HTML item, layout "
        "object, unmapped chart, report-page prompt control or layout component reference), or "
        "the parser raised an error: part of the report must be rebuilt by hand."
    ),
    ReadinessTier.NO_DATA.value: (
        "The specification has no queries, so there is no data to migrate; decide whether to "
        "retire it or rebuild it as a static page."
    ),
}

# Order used to present tiers, worst first.
TIER_ORDER = [
    ReadinessTier.REWORK.value,
    ReadinessTier.REVIEW.value,
    ReadinessTier.NO_DATA.value,
    ReadinessTier.READY.value,
]

# A report specification's root element, optionally namespace-prefixed (not <reportPages> etc.).
_REPORT_ROOT = re.compile(r"<(?:[A-Za-z_][\w.-]*:)?report[\s/>]")
_SEVERITY_ORDER = {Severity.ERROR.value: 0, Severity.WARNING.value: 1, Severity.INFO.value: 2}
# Report specifications are often exported as .txt, which batch discovery does not collect.
_EXTRA_REPORT_SUFFIXES = {".txt"}
_EXAMPLE_LIMIT = 400


class TierRule(BaseModel):
    """The rule used to assign readiness tiers, recorded in the output for transparency."""

    precedence: list[str] = Field(
        default_factory=lambda: [
            ReadinessTier.NO_DATA.value,
            ReadinessTier.REWORK.value,
            ReadinessTier.REVIEW.value,
            ReadinessTier.READY.value,
        ]
    )
    no_data_code: str = NO_DATA_CODE
    not_migrated_codes: list[str] = Field(default_factory=lambda: sorted(NOT_MIGRATED_CODES))
    ignored_codes: list[str] = Field(default_factory=lambda: sorted(TIER_IGNORED_CODES))
    meanings: dict[str, str] = Field(default_factory=lambda: dict(TIER_MEANINGS))


class ReportAssessment(BaseModel):
    """The assessment of one report specification."""

    path: str
    name: str
    status: str = "parsed"  # parsed | error
    error: str | None = None
    tier: ReadinessTier | None = None
    tier_reasons: list[str] = Field(default_factory=list)
    pages: int = 0
    visuals: int = 0
    visual_types: dict[str, int] = Field(default_factory=dict)
    queries: int = 0
    tables: int = 0
    prompts: int = 0
    errors: int = 0
    warnings: int = 0
    infos: int = 0
    flag_codes: list[str] = Field(default_factory=list)
    flags: list[ReviewFlag] = Field(default_factory=list)


class SkippedSource(BaseModel):
    """A file that was found but is not a report specification."""

    path: str
    name: str
    kind: str
    reason: str


class FlagAggregate(BaseModel):
    """How widespread one review flag code, at one severity, is across the estate."""

    code: str
    severity: str
    reports_affected: int = 0
    occurrences: int = 0
    tier_effect: str = "none"
    example_report: str = ""
    example_message: str = ""


class AssessmentTotals(BaseModel):
    """Estate-wide totals."""

    files: int = 0
    reports: int = 0
    parsed: int = 0
    parse_errors: int = 0
    skipped: int = 0
    tiers: dict[str, int] = Field(default_factory=dict)
    pages: int = 0
    visuals: int = 0
    visual_types: dict[str, int] = Field(default_factory=dict)
    queries: int = 0
    prompts: int = 0


class EstateAssessment(BaseModel):
    """The assessment of every report specification found in the inputs."""

    tool_version: str = __version__
    inputs: list[str] = Field(default_factory=list)
    tier_rule: TierRule = Field(default_factory=TierRule)
    totals: AssessmentTotals = Field(default_factory=AssessmentTotals)
    flags: list[FlagAggregate] = Field(default_factory=list)
    reports: list[ReportAssessment] = Field(default_factory=list)
    skipped: list[SkippedSource] = Field(default_factory=list)


def discover_sources(paths: Iterable[str | Path], recursive: bool = True) -> list[tuple[Path, str]]:
    """Return ``(path, display name)`` for every candidate source in the given files and folders.

    Folders are scanned with batch discovery plus ``.txt`` files; a file given directly is always a
    candidate. The display name is relative to the folder it was found in. Duplicates are dropped.
    """
    found: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    def add(path: Path, name: str) -> None:
        key = path.resolve()
        if key not in seen:
            seen.add(key)
            found.append((path, name))

    for entry in map(Path, paths):
        if not entry.is_dir():
            add(entry, entry.name)
            continue
        pattern = "**/*" if recursive else "*"
        extra = [
            path
            for path in entry.glob(pattern)
            if path.is_file() and path.suffix.lower() in _EXTRA_REPORT_SUFFIXES
        ]
        for path in sorted({*collect_sources(entry, recursive=recursive), *extra}):
            add(path, path.relative_to(entry).as_posix())
    return found


def assign_tier(flags: Sequence[ReviewFlag]) -> tuple[ReadinessTier, list[str]]:
    """Return a report's readiness tier and the flag codes that decided it.

    The first matching rule wins:

    1. ``no-data``: the parser found no queries (``no-queries``).
    2. ``rework``: any ERROR flag, or any flag in :data:`NOT_MIGRATED_CODES`.
    3. ``review``: any WARNING flag.
    4. ``ready``: otherwise.

    Flags in :data:`TIER_IGNORED_CODES` are ignored, and INFO flags never change the tier.
    """
    counted = [flag for flag in flags if flag.code not in TIER_IGNORED_CODES]
    if any(flag.code == NO_DATA_CODE for flag in counted):
        return ReadinessTier.NO_DATA, [NO_DATA_CODE]
    rework = {
        flag.code
        for flag in counted
        if flag.severity == Severity.ERROR or flag.code in NOT_MIGRATED_CODES
    }
    if rework:
        return ReadinessTier.REWORK, sorted(rework)
    review = {flag.code for flag in counted if flag.severity == Severity.WARNING}
    if review:
        return ReadinessTier.REVIEW, sorted(review)
    return ReadinessTier.READY, []


def assess_report(path: Path, name: str) -> ReportAssessment | SkippedSource:
    """Assess one file: parse it if it is a report specification, else return why it was skipped.

    Any failure to read or parse the file is recorded on the returned row instead of raised, so an
    estate run always completes.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        return ReportAssessment(
            path=str(path), name=name, status="error", error=f"Could not read file: {exc}"
        )
    kind = detect_source_kind(data, filename=path.name)
    if kind is not SourceKind.REPORT:
        return SkippedSource(
            path=str(path),
            name=name,
            kind=kind.value,
            reason="Only report specifications are assessed.",
        )
    text = _decode(data)
    if not _REPORT_ROOT.search(text):
        return SkippedSource(
            path=str(path),
            name=name,
            kind=SourceKind.UNKNOWN.value,
            reason="No <report> element found, so it is not a report specification.",
        )
    try:
        project = CognosReportParser().parse_bytes(data, name=path.stem)
    except Exception as exc:  # any parser failure is recorded so the estate run continues
        return ReportAssessment(
            path=str(path), name=name, status="error", error=_parse_error_message(exc, text)
        )
    return _summarize(project, path, name)


def assess_sources(paths: Iterable[str | Path], recursive: bool = True) -> EstateAssessment:
    """Assess every report specification in the given files and folders."""
    inputs = [str(path) for path in paths]
    reports: list[ReportAssessment] = []
    skipped: list[SkippedSource] = []
    sources = discover_sources(inputs, recursive=recursive)
    for path, name in sources:
        row = assess_report(path, name)
        if isinstance(row, SkippedSource):
            skipped.append(row)
        else:
            reports.append(row)
    return EstateAssessment(
        inputs=inputs,
        totals=_totals(len(sources), reports, skipped),
        flags=aggregate_flags(reports),
        reports=reports,
        skipped=skipped,
    )


def aggregate_flags(reports: Sequence[ReportAssessment]) -> list[FlagAggregate]:
    """Aggregate review flags across reports by code and severity, most widespread first.

    A code can be raised at different severities (``report-variables`` is INFO when every use of
    the variables is flagged elsewhere, WARNING otherwise), so each severity is its own row.
    ``reports_affected`` counts each report once per row; ``occurrences`` counts every flag. The
    two differ because a report can raise a code several times (one per query, for example).
    """
    affected: Counter[tuple[str, str]] = Counter()
    occurrences: Counter[tuple[str, str]] = Counter()
    examples: dict[tuple[str, str], tuple[str, str]] = {}
    for report in reports:
        affected.update({(flag.code, flag.severity.value) for flag in report.flags})
        for flag in report.flags:
            key = (flag.code, flag.severity.value)
            occurrences[key] += 1
            examples.setdefault(key, (report.name, flag.message))
    aggregates = [
        FlagAggregate(
            code=code,
            severity=severity,
            reports_affected=count,
            occurrences=occurrences[(code, severity)],
            tier_effect=_tier_effect(code, severity),
            example_report=examples[(code, severity)][0],
            example_message=examples[(code, severity)][1],
        )
        for (code, severity), count in affected.items()
    ]
    aggregates.sort(
        key=lambda agg: (
            -agg.reports_affected,
            -agg.occurrences,
            agg.code,
            _SEVERITY_ORDER[agg.severity],
        )
    )
    return aggregates


def write_assessment(assessment: EstateAssessment, out_dir: str | Path) -> dict[str, Path]:
    """Write ``assessment.json``, ``assessment.md`` and ``assessment.csv`` to ``out_dir``."""
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    outputs = {
        "json": root / "assessment.json",
        "markdown": root / "assessment.md",
        "csv": root / "assessment.csv",
    }
    outputs["json"].write_text(assessment.model_dump_json(indent=2), encoding="utf-8")
    outputs["markdown"].write_text(render_markdown(assessment), encoding="utf-8")
    _write_csv(outputs["csv"], assessment)
    return outputs


def render_markdown(assessment: EstateAssessment) -> str:
    """Render the human-readable assessment summary."""
    totals = assessment.totals
    lines = [
        "# Cognos estate assessment",
        "",
        f"Assessed {totals.reports} report specification(s) out of {totals.files} file(s) found: "
        f"{totals.parsed} parsed, {totals.parse_errors} could not be parsed, {totals.skipped} "
        "skipped (not report specifications). Reports were parsed only; no Power BI output was "
        f"generated. Tool version {assessment.tool_version}.",
        "",
        "## Readiness",
        "",
        "| Tier | Reports | Share | Meaning |",
        "| --- | ---: | ---: | --- |",
    ]
    for tier in TIER_ORDER:
        count = totals.tiers.get(tier, 0)
        lines.append(
            f"| {tier} | {count} | {_share(count, totals.parsed)} | {TIER_MEANINGS[tier]} |"
        )
    if totals.parse_errors:
        lines.append(f"| parse error | {totals.parse_errors} | - | See Parse errors below. |")
    lines.extend(["", *_tier_rule_lines(assessment.tier_rule)])
    lines.extend(["", *_flag_lines(assessment)])
    lines.extend(["", *_visual_lines(totals)])
    lines.extend(["", *_report_lines(assessment.reports)])
    errors = [report for report in assessment.reports if report.status == "error"]
    if errors:
        lines.extend(["", "## Parse errors", ""])
        lines.extend(f"- `{report.name}`: {_inline(report.error or '')}" for report in errors)
    if assessment.skipped:
        lines.extend(
            ["", "## Skipped files", "", "| File | Kind | Reason |", "| --- | --- | --- |"]
        )
        lines.extend(
            f"| {_cell(item.name)} | {item.kind} | {_cell(item.reason)} |"
            for item in assessment.skipped
        )
    return "\n".join(lines) + "\n"


def _decode(data: bytes) -> str:
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="ignore")
    return data.decode("utf-8-sig", errors="ignore")


def _parse_error_message(exc: Exception, text: str) -> str:
    message = f"{type(exc).__name__}: {exc}"
    match = _REPORT_ROOT.search(text)
    if match is None:
        return message
    declaration = text.find("<?xml", 0, match.start())
    prefix = text[: declaration if declaration >= 0 else match.start()].strip()
    if prefix and not prefix.startswith("<"):
        preview = " ".join(prefix.split())[:60]
        message += (
            f" The file has content before the report XML ('{preview}'), which the parser does "
            "not accept: remove everything before '<?xml' or '<report' and assess it again."
        )
    return message


def _summarize(project: MigrationProject, path: Path, name: str) -> ReportAssessment:
    flags = project.review_flags
    tier, reasons = assign_tier(flags)
    visual_types = Counter(
        visual.visual_type.value for page in project.pages for visual in page.visuals
    )
    severities = Counter(flag.severity for flag in flags)
    codes = {flag.code for flag in flags}
    return ReportAssessment(
        path=str(path),
        name=name,
        tier=tier,
        tier_reasons=reasons,
        # With no report pages the parser adds a default page and a no-pages flag.
        pages=0 if "no-pages" in codes else len(project.pages),
        visuals=sum(visual_types.values()),
        visual_types=dict(sorted(visual_types.items())),
        queries=len(project.query_graph.nodes),
        tables=len(project.tables),
        prompts=len(project.prompts),
        errors=severities[Severity.ERROR],
        warnings=severities[Severity.WARNING],
        infos=severities[Severity.INFO],
        flag_codes=sorted(codes),
        flags=list(flags),
    )


def _totals(
    files: int, reports: Sequence[ReportAssessment], skipped: Sequence[SkippedSource]
) -> AssessmentTotals:
    parsed = [report for report in reports if report.status == "parsed"]
    tiers = Counter(report.tier.value for report in parsed if report.tier is not None)
    visual_types: Counter[str] = Counter()
    for report in parsed:
        visual_types.update(report.visual_types)
    return AssessmentTotals(
        files=files,
        reports=len(reports),
        parsed=len(parsed),
        parse_errors=len(reports) - len(parsed),
        skipped=len(skipped),
        tiers={tier: tiers.get(tier, 0) for tier in TIER_ORDER},
        pages=sum(report.pages for report in parsed),
        visuals=sum(report.visuals for report in parsed),
        visual_types=dict(visual_types.most_common()),
        queries=sum(report.queries for report in parsed),
        prompts=sum(report.prompts for report in parsed),
    )


def _tier_effect(code: str, severity: str) -> str:
    if code in TIER_IGNORED_CODES:
        return "none"
    if code == NO_DATA_CODE:
        return ReadinessTier.NO_DATA.value
    if code in NOT_MIGRATED_CODES or severity == Severity.ERROR.value:
        return ReadinessTier.REWORK.value
    if severity == Severity.WARNING.value:
        return ReadinessTier.REVIEW.value
    return "none"


def _tier_rule_lines(rule: TierRule) -> list[str]:
    def codes(values: Iterable[str]) -> str:
        return ", ".join(f"`{value}`" for value in values)

    return [
        "### How tiers are assigned",
        "",
        "Each parsed report gets the first tier whose rule matches its review flags:",
        "",
        f"1. **no-data**: the parser found no queries (`{rule.no_data_code}`).",
        "2. **rework**: any ERROR flag, or any flag saying a report-page object was not migrated "
        f"({codes(rule.not_migrated_codes)}).",
        "3. **review**: any WARNING flag.",
        "4. **ready**: otherwise.",
        "",
        f"Allow-listed flags ({codes(rule.ignored_codes)}) never change the tier: `package-source` "
        "is raised for every report that reads a Cognos package, and pointing the generated tables "
        "at the real data source is done once per source, not per report. INFO flags never change "
        "the tier. Tiers describe what the converter did not do; they are not effort estimates.",
    ]


def top_flags(assessment: EstateAssessment, top: int = 10) -> list[FlagAggregate]:
    """Return the most widespread flags that carry a warning or an error.

    Informational flags (for example ``base-column-added``) are widespread but need no action, so
    they would crowd out the flags that do.
    """
    return [agg for agg in assessment.flags if agg.severity != Severity.INFO.value][:top]


def _flag_lines(assessment: EstateAssessment, top: int = 10) -> list[str]:
    parsed = assessment.totals.parsed
    lines = [
        "## Flag prevalence",
        "",
        "Reports affected counts each report once; occurrences counts every flag. One flag can "
        "cover several objects (for example all of a report's unmigrated singletons), so "
        "occurrences are not object counts. A code raised at two severities has a row for each.",
        "",
        "| Flag | Severity | Reports affected | Share of parsed reports | Occurrences "
        "| Tier effect |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for agg in assessment.flags:
        lines.append(
            f"| `{agg.code}` | {agg.severity} | {agg.reports_affected} | "
            f"{_share(agg.reports_affected, parsed)} | {agg.occurrences} | {agg.tier_effect} |"
        )
    if not assessment.flags:
        lines.append("| (none) | - | 0 | - | 0 | - |")
    lines.extend(
        [
            "",
            "### What the top flags mean",
            "",
            f"The {top} most widespread warning and error flags. Each line quotes the flag message "
            "from the first report that raised it; `assessment.json` has every message of every "
            "report.",
            "",
        ]
    )
    for agg in top_flags(assessment, top):
        message = " ".join(agg.example_message.split())
        if len(message) > _EXAMPLE_LIMIT:
            message = message[:_EXAMPLE_LIMIT].rstrip() + "..."
        lines.append(
            f"- `{agg.code}` ({agg.severity}, {agg.reports_affected} report(s); "
            f"e.g. `{agg.example_report}`): {_inline(message)}"
        )
    return lines


def _visual_lines(totals: AssessmentTotals) -> list[str]:
    lines = [
        "## Visuals by type",
        "",
        f"Across the parsed reports: {totals.pages} page(s), {totals.visuals} visual(s), "
        f"{totals.queries} query(ies), {totals.prompts} prompt(s).",
        "",
        "| Power BI visual type | Visuals |",
        "| --- | ---: |",
    ]
    lines.extend(f"| {kind} | {count} |" for kind, count in totals.visual_types.items())
    return lines


def _report_lines(reports: Sequence[ReportAssessment]) -> list[str]:
    order = {tier: index for index, tier in enumerate(TIER_ORDER)}

    def key(report: ReportAssessment) -> tuple[int, int, str]:
        rank = -1 if report.tier is None else order[report.tier.value]
        return rank, -(report.errors + report.warnings), report.name

    lines = [
        "## Reports",
        "",
        "Worst first: parse errors, then rework, review, no-data and ready, then by number of "
        "error and warning flags.",
        "",
        "| Tier | Report | Pages | Visuals | Queries | Prompts | Errors | Warnings | Info "
        "| Tier drivers |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for report in sorted(reports, key=key):
        tier = report.tier.value if report.tier is not None else "parse error"
        drivers = ", ".join(report.tier_reasons) or "-"
        lines.append(
            f"| {tier} | {_cell(report.name)} | {report.pages} | {report.visuals} | "
            f"{report.queries} | {report.prompts} | {report.errors} | {report.warnings} | "
            f"{report.infos} | {drivers} |"
        )
    return lines


def _write_csv(path: Path, assessment: EstateAssessment) -> None:
    columns = [
        "name",
        "path",
        "status",
        "tier",
        "tier_reasons",
        "pages",
        "visuals",
        "visual_types",
        "queries",
        "tables",
        "prompts",
        "errors",
        "warnings",
        "infos",
        "flag_codes",
        "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for report in assessment.reports:
            writer.writerow(
                [
                    report.name,
                    report.path,
                    report.status,
                    report.tier.value if report.tier is not None else "",
                    ";".join(report.tier_reasons),
                    report.pages,
                    report.visuals,
                    ";".join(f"{kind}={count}" for kind, count in report.visual_types.items()),
                    report.queries,
                    report.tables,
                    report.prompts,
                    report.errors,
                    report.warnings,
                    report.infos,
                    ";".join(report.flag_codes),
                    report.error or "",
                ]
            )


def _share(count: int, total: int) -> str:
    return f"{count / total:.0%}" if total else "-"


def _inline(text: str) -> str:
    """Collapse text to one markdown line, escaping '<' so it is not read as an HTML tag."""
    return " ".join(text.split()).replace("<", "&lt;")


def _cell(text: str) -> str:
    return _inline(text).replace("|", "\\|")
