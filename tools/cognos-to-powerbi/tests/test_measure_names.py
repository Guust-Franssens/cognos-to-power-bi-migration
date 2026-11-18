"""Regression tests for model-wide measure identities and reference rewriting."""

from __future__ import annotations

from itertools import permutations

import pytest

from cognos2powerbi.core.ir.models import (
    Column,
    Measure,
    MigrationProject,
    ReportPage,
    Table,
    Visual,
    VisualField,
)
from cognos2powerbi.core.modeling.measure_names import normalize_measure_names


def _collision_project() -> MigrationProject:
    return MigrationProject(
        name="Collision",
        tables=[
            Table(
                name="North",
                columns=[
                    Column(name="Amount"),
                    Column(name="Computed", dax_expression="[Total] + South[total]"),
                ],
                measures=[
                    Measure(name="Total", dax_expression="SUM(North[Amount])"),
                    Measure(
                        name="Combined",
                        dax_expression=(
                            "[Total] + South[total] + MIN([Total], 100) + "
                            'SUM(North[Total]) /* [Total] */ & "[Total]"'
                        ),
                    ),
                ],
            ),
            Table(
                name="South",
                measures=[
                    Measure(name="total", dax_expression="SUM(South[Amount])"),
                    Measure(name="Total (North)", dax_expression="1"),
                ],
            ),
        ],
        pages=[
            ReportPage(
                name="Page",
                display_name="Page",
                visuals=[
                    Visual(
                        fields=[
                            VisualField(table="North", name="Total"),
                            VisualField(table="South", name="total"),
                            VisualField(table="North", name="Amount", role="rows"),
                        ]
                    )
                ],
            )
        ],
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_names_and_references_are_stable_across_query_order(reverse: bool) -> None:
    project = _collision_project()
    if reverse:
        project.tables.reverse()
    normalize_measure_names(project)
    tables = {table.name: table for table in project.tables}
    north, south = tables["North"], tables["South"]
    assert [measure.name for measure in north.measures] == ["Total (North) 2", "Combined"]
    assert [measure.name for measure in south.measures] == ["total (South)", "Total (North)"]
    assert north.measures[0].dax_expression == "SUM(North[Amount])"
    assert north.measures[1].dax_expression == (
        "[Total (North) 2] + South[total (South)] + MIN([Total (North) 2], 100) + "
        'SUM(North[Total]) /* [Total] */ & "[Total]"'
    )
    assert north.columns[1].dax_expression == "[Total (North) 2] + South[total (South)]"
    assert [field.name for field in project.pages[0].visuals[0].fields] == [
        "Total (North) 2",
        "total (South)",
        "Amount",
    ]
    snapshot = project.model_dump()
    normalize_measure_names(project)
    assert project.model_dump() == snapshot


def test_column_and_measure_references_are_not_confused() -> None:
    project = MigrationProject(
        name="Columns",
        tables=[
            Table(
                name="Fact",
                columns=[Column(name="Amount")],
                measures=[
                    Measure(name="Amount", dax_expression="SUM(Fact[Amount])"),
                    Measure(name="Twice", dax_expression="[Amount] * 2 + MAX(Fact[Amount])"),
                ],
            )
        ],
        pages=[
            ReportPage(
                name="Page",
                display_name="Page",
                visuals=[Visual(fields=[VisualField(table="Fact", name="Amount")])],
            )
        ],
    )
    normalize_measure_names(project)
    table = project.tables[0]
    assert table.columns[0].name == "Amount"
    assert table.measures[0].name == "Amount (Fact)"
    assert table.measures[0].dax_expression == "SUM(Fact[Amount])"
    assert table.measures[1].dax_expression == "[Amount (Fact)] * 2 + MAX(Fact[Amount])"
    assert project.pages[0].visuals[0].fields[0].name == "Amount (Fact)"


def test_escaped_identifiers_and_literals_are_preserved() -> None:
    project = MigrationProject(
        name="Escapes",
        tables=[
            Table(name="O'Brien", measures=[Measure(name="Net]Value", dax_expression="1")]),
            Table(
                name="Other",
                measures=[
                    Measure(name="Net]Value", dax_expression="'O''Brien'[Net]]Value]"),
                    Measure(name="Label", dax_expression='"[Net]]Value]" // [Net]]Value]'),
                ],
            ),
        ],
    )
    normalize_measure_names(project)
    assert project.tables[1].measures[0].dax_expression == "'O''Brien'[Net]]Value (O'Brien)]"
    assert project.tables[1].measures[1].dax_expression == '"[Net]]Value]" // [Net]]Value]'


def test_ambiguous_reference_fails_without_partially_renaming() -> None:
    project = _collision_project()
    project.tables.append(
        Table(name="Unknown", measures=[Measure(name="Unbound", dax_expression="[Total]")])
    )
    snapshot = project.model_dump()
    with pytest.raises(ValueError, match="Ambiguous unqualified measure"):
        normalize_measure_names(project)
    assert project.model_dump() == snapshot


def test_same_table_duplicates_are_not_silently_disambiguated() -> None:
    project = MigrationProject(
        name="Invalid",
        tables=[Table(name="Fact", measures=[Measure(name="Count"), Measure(name="count")])],
    )
    with pytest.raises(ValueError, match="within a table"):
        normalize_measure_names(project)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("VAR x = [Total] RETURN [Total]", "VAR x = [Total (A)] RETURN [Total (A)]"),
        (
            "VAR x = [Total] return /* result */ [Total]",
            "VAR x = [Total (A)] return /* result */ [Total (A)]",
        ),
        ("b /* qualifier */ [tOTAL]", "b /* qualifier */ [Total (B)]"),
        ("B // qualifier\n [Total]", "B // qualifier\n [Total (B)]"),
        ("'B' -- qualifier\n [Total]", "'B' -- qualifier\n [Total (B)]"),
        ("SUM([Total] /* column */)", "SUM([Total] /* column */)"),
        (
            "SUM /* function */ ( /* argument */ [Total] -- column\n)",
            "SUM /* function */ ( /* argument */ [Total] -- column\n)",
        ),
        ("MAX([Total] /* column */)", "MAX([Total] /* column */)"),
        ("MIN([Total] /* scalar */, 100)", "MIN([Total (A)] /* scalar */, 100)"),
        ("MAX(100, /* scalar */ [Total])", "MAX(100, /* scalar */ [Total (A)])"),
        (
            "A /* column qualifier */ [Total] + [Total]",
            "A /* column qualifier */ [Total] + [Total (A)]",
        ),
        (
            '"[Total] "" B[Total]" & [Total] /* B[Total] */',
            '"[Total] "" B[Total]" & [Total (A)] /* B[Total] */',
        ),
        ("Unknown /* explicit identity */ [Total]", "Unknown /* explicit identity */ [Total]"),
    ],
)
def test_reference_identity_survives_keywords_and_trivia(expression: str, expected: str) -> None:
    project = MigrationProject(
        name="Trivia",
        tables=[
            Table(
                name="A",
                columns=[Column(name="Total")],
                measures=[
                    Measure(name="Total", dax_expression="1"),
                    Measure(name="Derived", dax_expression=expression),
                ],
            ),
            Table(name="B", measures=[Measure(name="Total", dax_expression="2")]),
        ],
    )
    normalize_measure_names(project)
    assert project.tables[0].measures[1].dax_expression == expected
    snapshot = project.model_dump()
    normalize_measure_names(project)
    assert project.model_dump() == snapshot


def test_escaped_qualifier_and_reference_survive_comments() -> None:
    project = MigrationProject(
        name="Escaped trivia",
        tables=[
            Table(name="O'Brien", measures=[Measure(name="Net]Value", dax_expression="1")]),
            Table(
                name="Other",
                measures=[
                    Measure(
                        name="Net]Value",
                        dax_expression="'O''BRIEN' /* [Net]]Value] */ [net]]value]",
                    ),
                ],
            ),
        ],
    )
    normalize_measure_names(project)
    assert project.tables[1].measures[0].dax_expression == (
        "'O''BRIEN' /* [Net]]Value] */ [Net]]Value (O'Brien)]"
    )


def test_return_does_not_hide_ambiguous_reference() -> None:
    project = _collision_project()
    project.tables.append(
        Table(
            name="Unknown",
            measures=[Measure(name="Unbound", dax_expression="VAR x = 1 RETURN [Total]")],
        )
    )
    snapshot = project.model_dump()
    with pytest.raises(ValueError, match="Ambiguous unqualified measure"):
        normalize_measure_names(project)
    assert project.model_dump() == snapshot


def test_competing_generated_names_are_independent_of_allocation_order() -> None:
    identities = (
        ("A) (B", "Total"),
        ("B", "Total (A)"),
        ("Z", "Total"),
        ("Y", "Total (A)"),
    )
    expected = {
        ("A) (B", "Total"): "Total (A) (B)",
        ("B", "Total (A)"): "Total (A) (B) 2",
        ("Z", "Total"): "Total (Z)",
        ("Y", "Total (A)"): "Total (A) (Y)",
    }
    for order in permutations(identities):
        for reverse_measures in (False, True):
            project = MigrationProject(
                name="Competing candidates",
                tables=[
                    Table(
                        name=table,
                        measures=[
                            Measure(name=name, cognos_expression=name),
                            Measure(name=f"Other {table}", cognos_expression=f"Other {table}"),
                        ][:: -1 if reverse_measures else 1],
                    )
                    for table, name in order
                ],
            )
            normalize_measure_names(project)
            actual = {
                (table.name, measure.cognos_expression): measure.name
                for table in project.tables
                for measure in table.measures
                if (table.name, measure.cognos_expression) in expected
            }
            assert actual == expected, (order, reverse_measures)
