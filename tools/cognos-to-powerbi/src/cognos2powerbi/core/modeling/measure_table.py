"""Host every measure on one dedicated measure table instead of its Cognos query table.

The IR keeps each measure on the table of the Cognos query it came from (its provenance). The
PBIP generator emits all of them on a single-row calculated table whose only column is hidden,
which Power BI shows as a measure table. Moving a measure changes how DAX resolves its
references, so this module also rewrites the DAX to stay valid on the new host:

* ``Home[Measure]`` becomes ``[Measure]``: a measure qualified with a table that no longer hosts
  it fails to resolve.
* In a measure, an unqualified ``[Column]`` of the home table becomes ``'Home'[Column]``: outside
  a row context an unqualified column only resolves on the table that hosts the measure.

Calculated columns stay on their home table because they need its row context.
"""

from __future__ import annotations

from dataclasses import dataclass

from cognos2powerbi.core.ir.models import MigrationProject, Severity
from cognos2powerbi.core.modeling.measure_names import DAX_KEYWORDS, DAX_TOKEN

MEASURE_TABLE_NAME = "_Measures"
_VALUE_COLUMN_NAME = "Value"


@dataclass(frozen=True)
class MeasureTable:
    """Names of the generated measure table and its single hidden column."""

    name: str
    value_column: str


def measure_table_for(project: MigrationProject) -> MeasureTable | None:
    """Return the measure table to emit, or ``None`` when the project has no measures."""
    measure_names = {m.name.casefold() for t in project.tables for m in t.measures}
    if not measure_names:
        return None
    return MeasureTable(
        name=_unique(MEASURE_TABLE_NAME, {t.name.casefold() for t in project.tables}),
        value_column=_unique(_VALUE_COLUMN_NAME, measure_names),
    )


def relocate_measure_references(project: MigrationProject) -> None:
    """Rewrite DAX in the IR so measures can live on the measure table. Idempotent.

    Run after measure names are model-wide unique. References that cannot be qualified safely
    are left unchanged and flagged for review.
    """
    columns = {t.name.casefold(): {c.name.casefold() for c in t.columns} for t in project.tables}
    measures = {t.name.casefold(): {m.name.casefold() for m in t.measures} for t in project.tables}
    all_measures = set().union(*measures.values()) if measures else set()
    flagged = {(f.code, f.source_ref) for f in project.review_flags}

    def rewrite(expression: str, home: str, *, qualify_columns: bool) -> tuple[str, list[str]]:
        tokens = [t for t in DAX_TOKEN.finditer(expression) if t.lastgroup != "trivia"]
        parts: list[str] = []
        ambiguous: list[str] = []
        end = 0
        home_key = home.casefold()
        for index, match in enumerate(tokens):
            if match.lastgroup != "reference":
                continue
            reference = match.group()
            name = reference[1:-1].replace("]]", "]").casefold()
            previous = tokens[index - 1] if index else None
            if previous is not None and (
                previous.lastgroup == "table"
                or (
                    previous.lastgroup == "word" and previous.group().casefold() not in DAX_KEYWORDS
                )
            ):
                qualifier = previous.group()
                table_key = (
                    qualifier[1:-1].replace("''", "'") if qualifier.startswith("'") else qualifier
                ).casefold()
                if name in measures.get(table_key, set()):
                    parts.extend((expression[end : previous.start()], reference))
                    end = match.end()
                continue
            if not qualify_columns or name not in columns.get(home_key, set()):
                continue
            if name in all_measures or any(
                name in table_columns
                for table_key, table_columns in columns.items()
                if table_key != home_key
            ):
                ambiguous.append(reference)
                continue
            parts.extend((expression[end : match.start()], _dax_table_name(home) + reference))
            end = match.end()
        parts.append(expression[end:])
        return "".join(parts), ambiguous

    for table in project.tables:
        for column in table.columns:
            if column.dax_expression:
                column.dax_expression, _ = rewrite(
                    column.dax_expression, table.name, qualify_columns=False
                )
        for measure in table.measures:
            if not measure.dax_expression:
                continue
            measure.dax_expression, ambiguous = rewrite(
                measure.dax_expression, table.name, qualify_columns=True
            )
            source_ref = f"{table.name}[{measure.name}]"
            if ambiguous and ("measure-table-ambiguous-reference", source_ref) not in flagged:
                project.add_flag(
                    "measure-table-ambiguous-reference",
                    f"Measure '{measure.name}' moved from '{table.name}' to the measure table "
                    f"uses unqualified {', '.join(sorted(set(ambiguous)))}, which also names a "
                    "column of another table or a measure. Qualify it with its table.",
                    Severity.WARNING,
                    source_ref=source_ref,
                )


def _unique(base: str, taken: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate.casefold() in taken:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _dax_table_name(name: str) -> str:
    """Quote a table name for DAX. Quoting is always valid, unlike bare names."""
    return "'" + name.replace("'", "''") + "'"
