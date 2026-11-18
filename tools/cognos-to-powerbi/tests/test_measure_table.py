"""Tests for moving measures onto the dedicated measure table."""

from __future__ import annotations

from cognos2powerbi.core.ir.models import Column, Measure, MigrationProject, Table
from cognos2powerbi.core.modeling.measure_table import (
    MeasureTable,
    measure_table_for,
    relocate_measure_references,
)


def _project() -> MigrationProject:
    return MigrationProject(
        name="Relocate",
        tables=[
            Table(
                name="Home Q",
                columns=[
                    Column(name="Amount"),
                    Column(name="Shared"),
                    Column(name="Total"),
                    Column(
                        name="Calc",
                        is_calculated=True,
                        dax_expression="'Home Q'[Base] & [Amount]",
                    ),
                ],
                measures=[
                    Measure(name="Base", dax_expression="SUM([Amount])"),
                    Measure(
                        name="Ratio",
                        dax_expression=(
                            "DIVIDE('Home Q'[Base], Other[Count]) "
                            "// [Amount] stays\n& \"[Amount]\" & SUM('Home Q'[Amount])"
                        ),
                    ),
                    Measure(name="Mixed", dax_expression="SUMX(Other, [Shared]) + [Total]"),
                ],
            ),
            Table(
                name="Other",
                columns=[Column(name="Shared")],
                measures=[Measure(name="Count", dax_expression="COUNTROWS(Other)")],
            ),
            Table(name="Totals", measures=[Measure(name="Total", dax_expression="1")]),
        ],
    )


def _dax(project: MigrationProject, table: str, name: str) -> str | None:
    owner = next(t for t in project.tables if t.name == table)
    item = next(i for i in [*owner.columns, *owner.measures] if i.name == name)
    return item.dax_expression


def test_relocation_unqualifies_measures_and_qualifies_home_columns() -> None:
    project = _project()
    relocate_measure_references(project)

    assert _dax(project, "Home Q", "Base") == "SUM('Home Q'[Amount])"
    # Comments, strings and qualified column references are left alone.
    assert _dax(project, "Home Q", "Ratio") == (
        "DIVIDE([Base], [Count]) // [Amount] stays\n& \"[Amount]\" & SUM('Home Q'[Amount])"
    )
    # A calculated column keeps its row context, so only measure qualifiers are dropped.
    assert _dax(project, "Home Q", "Calc") == "[Base] & [Amount]"


def test_relocation_flags_ambiguous_unqualified_references() -> None:
    project = _project()
    relocate_measure_references(project)

    # [Shared] is also a column of Other, [Total] is also a measure: neither is guessed.
    assert _dax(project, "Home Q", "Mixed") == "SUMX(Other, [Shared]) + [Total]"
    flags = [f for f in project.review_flags if f.code == "measure-table-ambiguous-reference"]
    assert [f.source_ref for f in flags] == ["Home Q[Mixed]"]
    assert "[Shared], [Total]" in flags[0].message


def test_relocation_is_idempotent() -> None:
    project = _project()
    relocate_measure_references(project)
    first = [(i.name, i.dax_expression) for t in project.tables for i in [*t.columns, *t.measures]]
    relocate_measure_references(project)

    assert [
        (i.name, i.dax_expression) for t in project.tables for i in [*t.columns, *t.measures]
    ] == first
    assert len(project.review_flags) == 1


def test_measure_table_names_avoid_collisions() -> None:
    assert measure_table_for(MigrationProject(name="Empty", tables=[Table(name="Q")])) is None

    project = MigrationProject(
        name="Clash",
        tables=[
            Table(name="_measures"),
            Table(name="_Measures_2"),
            Table(name="Q", measures=[Measure(name="value", dax_expression="1")]),
        ],
    )
    assert measure_table_for(project) == MeasureTable(name="_Measures_3", value_column="Value_2")
