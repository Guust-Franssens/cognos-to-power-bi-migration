"""Generate a Power BI Project (PBIP) from a :class:`MigrationProject`.

The generator writes the modern, Git-friendly PBIP layout:

    <Name>.pbip
    <Name>.SemanticModel/
        .platform
        definition.pbism
        definition/
            database.tmdl
            model.tmdl
            tables/<Table>.tmdl
            tables/_Measures.tmdl   (every measure, foldered by Cognos query)
    <Name>.Report/
        .platform
        definition.pbir
        report.json

TMDL (Tabular Model Definition Language) describes the semantic model; PBIR describes the
report. The output is a deterministic starting point. Visual fidelity and complex expressions
are best-effort and are refined by the optional AI stage; unmapped items are listed in
review_flags and surfaced to the user.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from cognos2powerbi.core.generators.metadata import write_migration_metadata
from cognos2powerbi.core.ir.models import (
    Cardinality,
    Column,
    CrossFilterDirection,
    DataSource,
    DataSourceKind,
    DataType,
    FilterCondition,
    FilterUse,
    Measure,
    MigrationProject,
    QueryFilter,
    Relationship,
    ReportPage,
    ReviewFlag,
    Severity,
    Table,
    Visual,
    VisualField,
    VisualType,
)
from cognos2powerbi.core.modeling.measure_names import normalize_measure_names
from cognos2powerbi.core.modeling.measure_table import (
    MeasureTable,
    measure_table_for,
    relocate_measure_references,
)
from cognos2powerbi.core.translate.filters import UntranslatableFilter, not_applied_message
from cognos2powerbi.core.translate.pbir_filters import (
    FilterField,
    check_literals,
    compares_text,
    filter_container,
    filter_name,
)

_COMPATIBILITY_LEVEL = 1606

# Power Query type for each column data type, used to type the empty placeholder partition.
_M_TYPE_FOR_DATA_TYPE = {
    DataType.STRING: "text",
    DataType.INT64: "Int64.Type",
    DataType.DOUBLE: "number",
    DataType.DECIMAL: "Currency.Type",
    DataType.BOOLEAN: "logical",
    DataType.DATE_TIME: "datetime",
}

# An unquoted TMDL identifier: ASCII letter or underscore, then ASCII letters, digits, underscores.
_TMDL_BARE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# PBIR (enhanced report format) JSON schema URLs. Current Power BI Desktop renders PBIR; the legacy
# single-file report.json is retired and fails to render in recent builds.
_PBIR_DEFINITION_PROPERTIES_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definitionProperties/2.0.0/schema.json"
)
_PBIR_VERSION_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/versionMetadata/1.0.0/schema.json"
)
_PBIR_REPORT_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/report/3.3.0/schema.json"
)
_PBIR_PAGES_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/pagesMetadata/1.1.0/schema.json"
)
_PBIR_PAGE_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/page/2.1.0/schema.json"
)
_PBIR_VISUAL_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/visualContainer/2.9.0/schema.json"
)
# A built-in (SharedResources) base theme shipped with Power BI, and the report-format versions
# recorded at import time. These mirror what current Power BI Desktop writes.
_BASE_THEME_NAME = "CY26SU05"
_BASE_THEME_VERSION_AT_IMPORT = {"visual": "2.9.0", "report": "3.3.0", "page": "2.3.1"}
# The base theme must be registered as a resource package or Power BI cannot resolve it, which
# fails the whole report exploration (a "visualContainers undefined" render error).
_REPORT_RESOURCE_PACKAGES = [
    {
        "name": "SharedResources",
        "type": "SharedResources",
        "items": [
            {
                "name": _BASE_THEME_NAME,
                "path": f"BaseThemes/{_BASE_THEME_NAME}.json",
                "type": "BaseTheme",
            }
        ],
    }
]
_REPORT_SETTINGS = {
    "useStylableVisualContainerHeader": True,
    "exportDataMode": "AllowSummarized",
    "defaultDrillFilterOtherVisuals": True,
    "allowChangeFilterTypes": True,
    "useEnhancedTooltips": True,
    "useDefaultAggregateDisplayName": True,
}

_CHART_VISUAL_TYPES = {
    VisualType.COLUMN_CHART,
    VisualType.BAR_CHART,
    VisualType.LINE_CHART,
    VisualType.PIE_CHART,
    VisualType.CLUSTERED_COLUMN_CHART,
    VisualType.CLUSTERED_BAR_CHART,
    VisualType.HUNDRED_PERCENT_STACKED_COLUMN_CHART,
    VisualType.HUNDRED_PERCENT_STACKED_BAR_CHART,
    VisualType.AREA_CHART,
    VisualType.STACKED_AREA_CHART,
    VisualType.HUNDRED_PERCENT_STACKED_AREA_CHART,
    VisualType.DONUT_CHART,
}

# Visuals whose PBIR data roles differ from the Category/Series/Y of the charts above:
# visual type -> (generic field role -> PBIR role, PBIR role for any other field role).
_VISUAL_ROLES: dict[VisualType, tuple[dict[str, str], str]] = {
    VisualType.TREEMAP: ({"category": "Group", "details": "Details", "values": "Values"}, "Values"),
    VisualType.SCATTER_CHART: (
        {"category": "Category", "series": "Series", "x": "X", "y": "Y", "size": "Size"},
        "Y",
    ),
    VisualType.WATERFALL_CHART: (
        {"category": "Category", "breakdown": "Breakdown", "values": "Y", "y": "Y"},
        "Y",
    ),
    **{
        combo: (
            {
                "category": "Category",
                "series": "Series",
                "values": "Y",
                "y": "Y",
                "line_values": "Y2",
            },
            "Y",
        )
        for combo in (
            VisualType.LINE_CLUSTERED_COLUMN_COMBO_CHART,
            VisualType.LINE_STACKED_COLUMN_COMBO_CHART,
        )
    },
}

# PBIR aggregation of a column bound where a visual needs one value: function code, then the
# queryRef function and display name prefix Power BI Desktop writes for it.
_AGGREGATIONS = {
    "sum": (0, "Sum", "Sum of "),
    "min": (3, "Min", "First "),
    "count": (5, "CountNonNull", "Count of "),
}

# Review flags for mandatory detail filters: the parser raises "detail-filter" (not applied) and
# the generator rewrites it once it applies the filter; the other two are regenerated each run.
_FILTER_FLAG_CODES = {"detail-filter", "detail-filter-applied"}
_FILTER_NOT_INHERITED = "detail-filter-not-inherited"
_FILTER_TEXT_CAVEAT = "detail-filter-text-comparison"


@dataclass
class _FilterPlan:
    """The visuals that apply one mandatory detail filter, decided before measures are renamed.

    ``measures`` maps a clause index to the measure a post-aggregation clause filters, so the
    filter follows the measure's model-wide name. ``consumers`` lists the queries that read the
    filtered query and so do not inherit the filter.
    """

    record: QueryFilter
    visuals: list[tuple[str, str]]
    measures: dict[int, Measure]
    consumers: list[str]


class PbipGenerator:
    """Render a migration project to PBIP files on disk."""

    # Auto-placed PBIR visuals now match the format current Power BI Desktop writes (schema 2.0.0).
    emit_visuals: bool = True

    def __init__(self) -> None:
        # Visual-level filters to write, keyed by (page name, visual name).
        self._visual_filters: dict[tuple[str, str], list[dict]] = {}

    def generate(self, project: MigrationProject, out_dir: str | Path) -> Path:
        plans = self._plan_detail_filters(project)
        self._add_filter_columns(project, plans)
        normalize_measure_names(project)
        relocate_measure_references(project)
        self._visual_filters = self._render_detail_filters(project, plans)
        self._update_filter_flags(project, plans)
        root = Path(out_dir)
        name = project.name
        model_dir = root / f"{name}.SemanticModel"
        report_dir = root / f"{name}.Report"
        (model_dir / "definition" / "tables").mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)

        self._write_pbip_root(root, name)
        self._write_semantic_model(model_dir, project)
        self._write_report(report_dir, model_dir, project)
        self._write_review_report(root, project)
        write_migration_metadata(project, root)
        return root / f"{name}.pbip"

    # ------------------------------------------------------------------ PBIP root

    def _write_pbip_root(self, root: Path, name: str) -> None:
        pbip = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
            "version": "1.0",
            "artifacts": [{"report": {"path": f"{name}.Report"}}],
            "settings": {"enableAutoRecovery": True},
        }
        _write_json(root / f"{name}.pbip", pbip)

    # ------------------------------------------------------------ Semantic model

    def _write_semantic_model(self, model_dir: Path, project: MigrationProject) -> None:
        _write_json(
            model_dir / ".platform",
            _platform("SemanticModel", project.name),
        )
        _write_json(model_dir / "definition.pbism", {"version": "4.0", "settings": {}})

        definition = model_dir / "definition"
        _write_text(
            definition / "database.tmdl",
            f"database\n\tcompatibilityLevel: {_COMPATIBILITY_LEVEL}\n",
        )
        _write_text(definition / "model.tmdl", self._render_model_tmdl(project))

        for table in project.tables:
            _write_text(
                definition / "tables" / f"{table.name}.tmdl",
                self._render_table_tmdl(table, project.data_source),
            )
        measure_table = measure_table_for(project)
        if measure_table:
            _write_text(
                definition / "tables" / f"{measure_table.name}.tmdl",
                self._render_measure_table_tmdl(project, measure_table),
            )

    def _render_model_tmdl(self, project: MigrationProject) -> str:
        lines = [
            "model Model",
            "\tculture: en-US",
            "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
            "",
        ]
        source = project.data_source
        if source.kind == DataSourceKind.SQL_SERVER:
            lines.append(
                f'expression Server = "{source.server}" meta '
                '[IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]'
            )
            lines.append(
                f'expression Database = "{source.database}" meta '
                '[IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]'
            )
            lines.append("")
        measure_table = measure_table_for(project)
        if measure_table:
            lines.append(f"ref table {_escape_tmdl_name(measure_table.name)}")
        for table in project.tables:
            lines.append(f"ref table {_escape_tmdl_name(table.name)}")
        lines.append("")
        for relationship in project.relationships:
            lines.extend(self._render_relationship_tmdl(relationship))
        return "\n".join(lines)

    def _render_relationship_tmdl(self, relationship: Relationship) -> list[str]:
        from_ref = (
            f"{_escape_tmdl_name(relationship.from_table)}."
            f"{_escape_tmdl_name(relationship.from_column)}"
        )
        to_ref = (
            f"{_escape_tmdl_name(relationship.to_table)}."
            f"{_escape_tmdl_name(relationship.to_column)}"
        )
        lines = [f"relationship {uuid.uuid4()}"]
        if not relationship.is_active:
            lines.append("\tisActive: false")
        lines.append(f"\tfromColumn: {from_ref}")
        lines.append(f"\ttoColumn: {to_ref}")
        # many-to-one is the Power BI default and needs no explicit cardinality tokens.
        if relationship.cardinality == Cardinality.MANY_TO_MANY:
            lines.append("\tfromCardinality: many")
            lines.append("\ttoCardinality: many")
        elif relationship.cardinality == Cardinality.ONE_TO_ONE:
            lines.append("\tfromCardinality: one")
            lines.append("\ttoCardinality: one")
        elif relationship.cardinality == Cardinality.ONE_TO_MANY:
            lines.append("\tfromCardinality: one")
            lines.append("\ttoCardinality: many")
        if relationship.cross_filter == CrossFilterDirection.BOTH:
            lines.append("\tcrossFilteringBehavior: bothDirections")
        lines.append("")
        return lines

    def _render_table_tmdl(self, table: Table, data_source: DataSource) -> str:
        lines = [f"table {_escape_tmdl_name(table.name)}"]
        if table.data_category:
            lines.append(f"\tdataCategory: {table.data_category}")
        lines.append("")
        for column in table.columns:
            if _is_calculated_column(column):
                if column.dax_expression:
                    dax = _dax_single_line(column.dax_expression)
                    lines.append(f"\tcolumn {_escape_tmdl_name(column.name)} = {dax}")
                else:
                    lines.append(f"\tcolumn {_escape_tmdl_name(column.name)} = ```")
                    lines.extend(_untranslated_placeholder(column.cognos_expression))
                lines.append(f"\t\tdataType: {column.data_type.value}")
                lines.extend(_format_string_lines(column))
                if column.is_hidden:
                    lines.append("\t\tisHidden")
                if column.summarize_by:
                    lines.append(f"\t\tsummarizeBy: {column.summarize_by}")
                if column.data_category:
                    lines.append(f"\t\tdataCategory: {column.data_category}")
                annotation = _cognos_expression_annotation(column.cognos_expression)
                if annotation:
                    lines.append(annotation)
                lines.append("")
                continue
            lines.append(f"\tcolumn {_escape_tmdl_name(column.name)}")
            lines.append(f"\t\tdataType: {column.data_type.value}")
            lines.extend(_format_string_lines(column))
            lines.append(f"\t\tsourceColumn: {column.source_column or column.name}")
            if column.is_hidden:
                lines.append("\t\tisHidden")
            if column.summarize_by:
                lines.append(f"\t\tsummarizeBy: {column.summarize_by}")
            if column.data_category:
                lines.append(f"\t\tdataCategory: {column.data_category}")
            lines.append("")
        lines.append(f"\tpartition {_escape_tmdl_name(table.name)} = m")
        lines.append("\t\tmode: import")
        lines.append("\t\tsource =")
        lines.extend(self._render_partition_source(table, data_source))
        lines.append("")
        return "\n".join(lines)

    def _render_measure_table_tmdl(
        self, project: MigrationProject, measure_table: MeasureTable
    ) -> str:
        """Every measure on one single-row calculated table, foldered by its Cognos query."""
        name = _escape_tmdl_name(measure_table.name)
        value = measure_table.value_column
        lines = [
            f"table {name}",
            "",
            f"\tcolumn {_escape_tmdl_name(value)}",
            "\t\tdataType: string",
            "\t\tisHidden",
            "\t\tsummarizeBy: none",
            f"\t\tsourceColumn: [{value}]",
            "",
        ]
        for table in project.tables:
            for measure in table.measures:
                if measure.dax_expression and not measure.needs_review:
                    expression = _dax_single_line(measure.dax_expression)
                    lines.append(f"\tmeasure {_escape_tmdl_name(measure.name)} = {expression}")
                else:
                    # A measure still under review may hold invalid DAX (wrong arity, an
                    # untranslated function, an unexpanded cycle). Like a pending calculated
                    # column, it loads as BLANK() and keeps the draft only as a comment.
                    lines.append(f"\tmeasure {_escape_tmdl_name(measure.name)} = ```")
                    lines.extend(
                        _untranslated_placeholder(
                            measure.cognos_expression, draft_dax=measure.dax_expression
                        )
                    )
                lines.extend(_format_string_lines(measure))
                lines.append(f"\t\tdisplayFolder: {table.name}")
                annotation = _cognos_expression_annotation(measure.cognos_expression)
                if annotation:
                    lines.append(annotation)
                lines.append("")
        lines.extend(
            [
                f"\tpartition {name} = calculated",
                "\t\tmode: import",
                f'\t\tsource = ROW("{value}", BLANK())',
                "",
            ]
        )
        return "\n".join(lines)

    def _render_partition_source(self, table: Table, data_source: DataSource) -> list[str]:
        if data_source.kind == DataSourceKind.SQL_SERVER:
            item = table.source_query or table.name
            return [
                "\t\t\tlet",
                "\t\t\t\tSource = Sql.Database(Server, Database),",
                f'\t\t\t\tNavigation = Source{{[Schema="{data_source.schema_name}", '
                f'Item="{item}"]}}[Data]',
                "\t\t\tin",
                "\t\t\t\tNavigation",
            ]
        # An empty table typed with every source column, so the model refreshes before data exists.
        fields: list[str] = []
        seen: set[str] = set()
        for column in table.columns:
            if _is_calculated_column(column):
                continue
            source = column.source_column or column.name
            if source.casefold() in seen:
                continue
            seen.add(source.casefold())
            m_type = _M_TYPE_FOR_DATA_TYPE.get(column.data_type, "any")
            fields.append(f'#"{source.replace(chr(34), chr(34) * 2)}" = {m_type}')
        return [
            "\t\t\tlet",
            f"\t\t\t\tSource = #table(type table [{', '.join(fields)}], {{}})"
            "  // TODO: replace with the Power Query for this Cognos query",
            "\t\t\tin",
            "\t\t\t\tSource",
        ]

    # -------------------------------------------------------------------- Report

    def _write_report(self, report_dir: Path, model_dir: Path, project: MigrationProject) -> None:
        """Write the report as PBIR (the modern ``definition/`` folder), not legacy report.json.

        Power BI Desktop (2026) renders the PBIR format; the legacy single-file report.json is
        being retired and fails to render in current builds.
        """
        _write_json(report_dir / ".platform", _platform("Report", project.name))
        _write_json(
            report_dir / "definition.pbir",
            {
                "$schema": _PBIR_DEFINITION_PROPERTIES_SCHEMA,
                "version": "4.0",
                "datasetReference": {"byPath": {"path": f"../{model_dir.name}"}},
            },
        )
        definition = report_dir / "definition"
        _write_json(
            definition / "version.json",
            {"$schema": _PBIR_VERSION_SCHEMA, "version": "2.0.0"},
        )
        _write_json(
            definition / "report.json",
            {
                "$schema": _PBIR_REPORT_SCHEMA,
                "themeCollection": {
                    "baseTheme": {
                        "name": _BASE_THEME_NAME,
                        "reportVersionAtImport": _BASE_THEME_VERSION_AT_IMPORT,
                        "type": "SharedResources",
                    }
                },
                "resourcePackages": _REPORT_RESOURCE_PACKAGES,
                "settings": _REPORT_SETTINGS,
            },
        )
        self._write_pages(definition / "pages", project)

    def _write_pages(self, pages_dir: Path, project: MigrationProject) -> None:
        page_names: list[str] = []
        for page_name, page, visuals in _report_layout(project):
            page_names.append(page_name)
            self._write_page(pages_dir / page_name, page_name, page, visuals, project)
        _write_json(
            pages_dir / "pages.json",
            {
                "$schema": _PBIR_PAGES_SCHEMA,
                "pageOrder": page_names,
                "activePageName": page_names[0] if page_names else "",
            },
        )

    def _write_page(
        self,
        page_dir: Path,
        page_name: str,
        page: ReportPage,
        visuals: list[tuple[str, Visual]],
        project: MigrationProject,
    ) -> None:
        _write_json(
            page_dir / "page.json",
            {
                "$schema": _PBIR_PAGE_SCHEMA,
                "name": page_name,
                "displayName": page.display_name or page_name,
                "displayOption": "FitToPage",
                "height": 720,
                "width": 1280,
            },
        )
        if not self.emit_visuals:
            return
        for visual_name, visual in visuals:
            self._write_visual(
                page_dir / "visuals" / visual_name,
                visual_name,
                visual,
                project,
                self._visual_filters.get((page_name, visual_name), []),
            )

    def _write_visual(
        self,
        visual_dir: Path,
        visual_name: str,
        visual: Visual,
        project: MigrationProject,
        filters: list[dict] | None = None,
    ) -> None:
        visual_config: dict[str, object] = {"visualType": visual.visual_type.value}
        if visual.visual_type == VisualType.MATRIX and visual.values_on_rows:
            visual_config["objects"] = {
                "values": [
                    {"properties": {"valuesOnRow": {"expr": {"Literal": {"Value": "true"}}}}}
                ]
            }
        query_state = self._build_query_state(visual, project)
        if query_state:
            visual_config["query"] = {"queryState": query_state}
        visual_config["drillFilterOtherVisuals"] = True
        container: dict[str, object] = {
            "$schema": _PBIR_VISUAL_SCHEMA,
            "name": visual_name,
            "position": {
                "x": visual.x,
                "y": visual.y,
                "z": 1000,
                "width": visual.width,
                "height": visual.height,
                "tabOrder": 0,
            },
            "visual": visual_config,
        }
        if filters:
            container["filterConfig"] = {"filters": filters}
        _write_json(visual_dir / "visual.json", container)

    def _build_query_state(self, visual: Visual, project: MigrationProject) -> dict:
        role_projections: dict[str, list[dict]] = {}
        for field in visual.fields:
            role = _pbir_role(visual.visual_type, field.role)
            expression, entity = self._field_expression(field, project)
            query_ref, native_ref = f"{entity}.{field.name}", field.name
            aggregation = _AGGREGATIONS.get(field.aggregation) if field.aggregation else None
            if "Column" in expression and aggregation is not None:
                function, name, prefix = aggregation
                expression = {"Aggregation": {"Expression": expression, "Function": function}}
                query_ref, native_ref = f"{name}({query_ref})", f"{prefix}{field.name}"
            projection: dict[str, object] = {
                "field": expression,
                "queryRef": query_ref,
                "nativeQueryRef": native_ref,
            }
            projection["active"] = True
            role_projections.setdefault(role, []).append(projection)
        return {
            role: {"projections": projections} for role, projections in role_projections.items()
        }

    def _field_expression(self, field: VisualField, project: MigrationProject) -> tuple[dict, str]:
        """Return the PBIR field expression and the table (Entity) it binds to."""
        table = next((t for t in project.tables if t.name == field.table), None)
        is_measure = bool(table and any(m.name == field.name for m in table.measures))
        measure_table = measure_table_for(project) if is_measure else None
        entity = measure_table.name if measure_table else field.table
        inner = {"Expression": {"SourceRef": {"Entity": entity}}, "Property": field.name}
        expression = {"Measure": inner} if is_measure else {"Column": inner}
        return expression, entity

    # ------------------------------------------------------------- Detail filters

    def _plan_detail_filters(self, project: MigrationProject) -> list[_FilterPlan]:
        """Decide which visuals apply each translated mandatory Cognos detail filter.

        A filter applies to every visual that reads its query, so Power BI shows the rows the
        Cognos query returned. A filter that cannot be applied keeps its translation and records
        why on its :class:`QueryFilter`. Runs before measure names are normalized, while visual
        fields and measures still use the same names.
        """
        visuals_by_query: dict[str, list[tuple[str, str, Visual]]] = {}
        if self.emit_visuals:
            for page_name, _page, visuals in _report_layout(project):
                for visual_name, visual in visuals:
                    for table in dict.fromkeys(item.table for item in visual.fields):
                        visuals_by_query.setdefault(table, []).append(
                            (page_name, visual_name, visual)
                        )
        consumers: dict[str, list[str]] = {}
        for edge in project.query_graph.edges:
            readers = consumers.setdefault(edge.to_query, [])
            if edge.from_query not in readers:
                readers.append(edge.from_query)
        plans: list[_FilterPlan] = []
        for record in project.filters:
            if record.use is not FilterUse.REQUIRED or not record.clauses:
                continue
            record.applied_to = []
            record.untranslated_code = record.untranslated_reason = None
            try:
                plans.append(
                    self._plan_detail_filter(
                        record,
                        project,
                        visuals_by_query.get(record.query, []),
                        consumers.get(record.query, []),
                    )
                )
            except UntranslatableFilter as error:
                record.untranslated_code = error.code
                record.untranslated_reason = error.reason
        return plans

    def _plan_detail_filter(
        self,
        record: QueryFilter,
        project: MigrationProject,
        visuals: list[tuple[str, str, Visual]],
        consumers: list[str],
    ) -> _FilterPlan:
        if not visuals:
            if not self.emit_visuals:
                raise UntranslatableFilter("no-visual", "visuals are not generated")
            if consumers:
                raise UntranslatableFilter("query-operand", f"it feeds {_quoted(consumers)}")
            raise UntranslatableFilter("no-visual", record.query)
        table = next((t for t in project.tables if t.name == record.query), None)
        if table is None:
            raise UntranslatableFilter("unresolved-item", f"no model table '{record.query}'")
        measures: dict[int, Measure] = {}
        for index, clause in enumerate(record.clauses):
            target = clause.target
            if target.kind == "measure":
                measure = next((m for m in table.measures if m.name == target.name), None)
                if measure is None:
                    raise UntranslatableFilter("post-aggregation", f"no measure {target.name}")
                for page_name, visual_name, visual in visuals:
                    if not _shows_measure_by(visual, project, table, measure, target.group_by):
                        raise UntranslatableFilter(
                            "post-aggregation",
                            f"visual {page_name}/{visual_name} does not show [{measure.name}] "
                            f"grouped by exactly the grouping items of query '{table.name}'",
                        )
                measures[index] = measure
                field = FilterField(table.name, measure.name, "measure")
            else:
                column = table.column(target.name)
                if column is None and target.add_column:
                    data_type = target.data_type or DataType.STRING
                elif column is not None and (
                    not target.add_column or _reads_source(column, target.source_column)
                ):
                    data_type = column.data_type
                else:
                    raise UntranslatableFilter("unresolved-item", f"{table.name}[{target.name}]")
                field = FilterField(table.name, target.name, data_type=data_type)
            check_literals(clause.condition, field)
        return _FilterPlan(
            record=record,
            visuals=[(page_name, visual_name) for page_name, visual_name, _ in visuals],
            measures=measures,
            consumers=consumers,
        )

    @staticmethod
    def _add_filter_columns(project: MigrationProject, plans: list[_FilterPlan]) -> None:
        """Add the hidden source columns that applied filters test but no data item selects."""
        added: dict[str, list[str]] = {}
        for plan in plans:
            table = next(t for t in project.tables if t.name == plan.record.query)
            for clause in plan.record.clauses:
                target = clause.target
                if not target.add_column or table.column(target.name) is not None:
                    continue
                table.columns.append(
                    Column(
                        name=target.name,
                        data_type=target.data_type or DataType.STRING,
                        source_column=target.source_column,
                        is_hidden=True,
                    )
                )
                added.setdefault(table.name, []).append(target.name)
        for table_name, names in added.items():
            project.add_flag(
                "base-column-added",
                f"Added hidden source column(s) {', '.join(names)} to '{table_name}' because a "
                "mandatory Cognos detail filter tests them. Provide them in the source query or "
                "synthetic data.",
                Severity.INFO,
            )

    @staticmethod
    def _render_detail_filters(
        project: MigrationProject, plans: list[_FilterPlan]
    ) -> dict[tuple[str, str], list[dict]]:
        """Build each visual's PBIR filters: one per filtered field, its clauses ANDed."""
        measure_table = measure_table_for(project)
        by_visual: dict[tuple[str, str], dict[FilterField, list[tuple[FilterCondition, bool]]]]
        by_visual = {}
        for plan in plans:
            table = next(t for t in project.tables if t.name == plan.record.query)
            for index, clause in enumerate(plan.record.clauses):
                measure = plan.measures.get(index)
                if measure is not None:
                    # Follow the model-wide measure name and the table measures are emitted on.
                    assert measure_table is not None
                    clause.target.name = measure.name
                    field = FilterField(measure_table.name, measure.name, "measure")
                else:
                    column = table.column(clause.target.name)
                    data_type = column.data_type if column else clause.target.data_type
                    field = FilterField(table.name, clause.target.name, data_type=data_type)
                for key in plan.visuals:
                    clauses = by_visual.setdefault(key, {}).setdefault(field, [])
                    clauses.append((clause.condition, clause.keeps_null))
            plan.record.applied_to = [f"{page}/{visual}" for page, visual in plan.visuals]
        return {
            key: [
                filter_container(
                    filter_name(*key, field.kind, field.entity, field.name), field, clauses
                )
                for field, clauses in fields.items()
            ]
            for key, fields in by_visual.items()
        }

    @staticmethod
    def _update_filter_flags(project: MigrationProject, plans: list[_FilterPlan]) -> None:
        """Rewrite each translated mandatory filter's review flag to say what was applied where.

        The parser flags every mandatory filter as not applied. A filter applied here gets an
        INFO flag naming its visuals instead; one that could not be applied keeps a WARNING that
        says why. Rewriting in place keeps the flag order and makes a second run idempotent.
        """
        flags = project.review_flags
        flags[:] = [f for f in flags if f.code not in {_FILTER_NOT_INHERITED, _FILTER_TEXT_CAVEAT}]
        planned = {id(plan.record): plan for plan in plans}
        used: set[int] = set()
        extra: list[ReviewFlag] = []
        for record in project.filters:
            if record.use is not FilterUse.REQUIRED:
                continue
            prefix = f"Query '{record.query}' "
            index = next(
                (
                    i
                    for i, flag in enumerate(flags)
                    if i not in used
                    and flag.code in _FILTER_FLAG_CODES
                    and flag.source_ref == record.expression
                    and flag.message.startswith(prefix)
                ),
                None,
            )
            if index is not None:
                used.add(index)
            if not record.clauses:
                continue
            plan = planned.get(id(record))
            if plan is None:
                flag = ReviewFlag(
                    code="detail-filter",
                    message=not_applied_message(record.query, record.untranslated_reason),
                    severity=Severity.WARNING,
                    source_ref=record.expression,
                )
            else:
                flag = ReviewFlag(
                    code="detail-filter-applied",
                    message=_applied_message(record),
                    severity=Severity.INFO,
                    source_ref=record.expression,
                )
            if index is None:
                extra.append(flag)
            else:
                flags[index] = flag
        flags.extend(extra)
        for plan in plans:
            if plan.consumers:
                query = plan.record.query
                project.add_flag(
                    _FILTER_NOT_INHERITED,
                    f"Query '{query}' feeds {_quoted(plan.consumers)} (join, union or "
                    f"reference). Its mandatory detail filter was applied to the visuals of "
                    f"'{query}' only; the Power BI tables built for {_quoted(plan.consumers)} do "
                    "not inherit it. Apply the same condition in their Power Query source.",
                    Severity.WARNING,
                    source_ref=plan.record.expression,
                )
        if any(compares_text(c.condition) for plan in plans for c in plan.record.clauses):
            project.add_flag(
                _FILTER_TEXT_CAVEAT,
                "Detail filters applied as visual-level filters compare text values. Power BI "
                "compares text case-insensitively, while Cognos follows the source database "
                "collation (often case-sensitive), so a filtered visual can also keep rows that "
                "differ from a filter value only in letter case.",
                Severity.INFO,
            )

    # -------------------------------------------------------------- Review report

    def _write_review_report(self, root: Path, project: MigrationProject) -> None:
        if not project.review_flags:
            return
        lines = [
            f"# Migration review: {project.name}",
            "",
            "The following items were converted with reduced fidelity or could not be mapped",
            "deterministically. Review them in Power BI Desktop or run the AI refinement stage.",
            "",
            "| Severity | Code | Message | Source |",
            "| --- | --- | --- | --- |",
        ]
        for flag in project.review_flags:
            source = (flag.source_ref or "").replace("|", "\\|")
            message = flag.message.replace("|", "\\|")
            lines.append(f"| {flag.severity.value} | {flag.code} | {message} | {source} |")
        lines.append("")
        _write_text(root / "MIGRATION_REVIEW.md", "\n".join(lines))
        # Same flags, unescaped, for tools and agents that should not parse the markdown table.
        items = [
            {
                "severity": flag.severity.value,
                "code": flag.code,
                "message": flag.message,
                "source": flag.source_ref,
            }
            for flag in project.review_flags
        ]
        _write_text(
            root / "MIGRATION_REVIEW.json",
            json.dumps({"project": project.name, "items": items}, indent=2, ensure_ascii=False)
            + "\n",
        )


# ----------------------------------------------------------------------- helpers


def _platform(item_type: str, display_name: str) -> dict:
    return {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": item_type, "displayName": display_name},
        "config": {"version": "2.0", "logicalId": str(uuid.uuid4())},
    }


def _tmdl_text_value(value: str) -> str:
    """Render a TMDL text property value (such as ``formatString``) on one line.

    TMDL trims an unquoted value and treats a leading double quote as the start of a quoted
    value, so a value with surrounding whitespace or any double quote is enclosed in double
    quotes with each inner double quote doubled. Backslashes need no escaping in TMDL.
    """
    if '"' in value or value != value.strip():
        return '"' + value.replace('"', '""') + '"'
    return value


def _format_string_lines(item: Column | Measure) -> list[str]:
    """Return the ``formatString`` property line of a column or measure, if it has a format.

    Power BI cannot apply a format string to a text or Boolean column, so none is written there
    (the parser never assigns one; it flags such formats as not applied instead).
    """
    if not item.format_string:
        return []
    if isinstance(item, Column) and item.data_type in {DataType.STRING, DataType.BOOLEAN}:
        return []
    return [f"\t\tformatString: {_tmdl_text_value(item.format_string)}"]


def _escape_tmdl_name(name: str) -> str:
    """Quote a TMDL object name unless it is a bare identifier.

    An unquoted TMDL identifier must start with an ASCII letter or underscore and contain only
    ASCII letters, digits, and underscores. Anything else (spaces, punctuation, accented letters,
    or a leading digit such as a column literally named "1") must be single-quoted, and any single
    quote inside the name is doubled.
    """
    if name and _TMDL_BARE_IDENTIFIER.match(name):
        return name
    return "'" + name.replace("'", "''") + "'"


def _dax_single_line(expression: str) -> str:
    """Collapse a DAX expression to a single line.

    TMDL renders an inline ``column X = <expr>`` / ``measure X = <expr>`` on one line. A multi-line
    expression (for example from an AI provider) would break TMDL indentation, so newlines and runs
    of whitespace are collapsed to single spaces. ``//`` and ``/* */`` comments are removed first so
    collapsing does not comment out the rest. DAX is whitespace-insensitive between tokens, so this
    preserves the semantics.
    """
    without_line_comments = re.sub(r"//[^\n]*", " ", expression)
    without_block_comments = re.sub(r"/\*.*?\*/", " ", without_line_comments, flags=re.DOTALL)
    return re.sub(r"\s+", " ", without_block_comments).strip()


def _is_calculated_column(column: Column) -> bool:
    """A column computed in DAX rather than loaded: translated, or a Cognos calculation pending."""
    return column.is_calculated or column.needs_calculation


def _untranslated_placeholder(
    cognos_expression: str | None, draft_dax: str | None = None
) -> list[str]:
    """Body of a multi-line TMDL expression that loads as ``BLANK()`` until it is translated.

    The Cognos source is kept as ``//`` comments, one per line, so a reviewer (or the AI stage)
    sees exactly what to translate. An unverified ``draft_dax`` is appended the same way.
    Commenting every line also neutralises backticks or TMDL keywords in either text.
    """
    source = cognos_expression or "(source expression unavailable)"
    lines = [
        "\t\t\tBLANK()",
        "\t\t\t// TODO: translate the original Cognos expression:",
        *(f"\t\t\t// {line}" for line in source.splitlines()),
    ]
    if draft_dax and draft_dax.strip():
        lines.append("\t\t\t// Unverified draft DAX (needs review, not evaluated):")
        lines.extend(f"\t\t\t// {line}" for line in draft_dax.splitlines())
    lines.append("\t\t\t```")
    return lines


def _cognos_expression_annotation(expression: str | None) -> str | None:
    """Render a one-line annotation, preserving whitespace inside quoted literals."""
    if not expression or not expression.strip():
        return None
    parts: list[str] = []
    quote: str | None = None
    pending_space = False
    index = 0
    while index < len(expression):
        char = expression[index]
        if quote:
            parts.append(char)
            if char == quote:
                if index + 1 < len(expression) and expression[index + 1] == quote:
                    index += 1
                    parts.append(expression[index])
                else:
                    quote = None
        elif char in {"'", '"'}:
            if pending_space and parts:
                parts.append(" ")
            pending_space = False
            quote = char
            parts.append(char)
        elif char.isspace():
            pending_space = True
        else:
            if pending_space and parts:
                parts.append(" ")
            pending_space = False
            parts.append(char)
        index += 1
    value = "".join(parts).strip()
    return f"\t\tannotation CognosExpression = {value}"


def _safe_report_name(base: str, used: set[str], fallback: str) -> str:
    """Return a unique PBIR object name (word characters or hyphens, max 50 chars)."""
    cleaned = re.sub(r"[^0-9A-Za-z_-]+", "", base)[:50] or fallback
    candidate = cleaned
    suffix = 2
    while candidate in used:
        candidate = f"{cleaned}{suffix}"[:50]
        suffix += 1
    used.add(candidate)
    return candidate


def _report_layout(
    project: MigrationProject,
) -> list[tuple[str, ReportPage, list[tuple[str, Visual]]]]:
    """Every page with its PBIR name, and its visuals with theirs, in the order written."""
    layout: list[tuple[str, ReportPage, list[tuple[str, Visual]]]] = []
    used_pages: set[str] = set()
    for index, page in enumerate(project.pages, start=1):
        base = page.name or page.display_name or f"Page{index}"
        page_name = _safe_report_name(base, used_pages, f"Page{index}")
        used: set[str] = set()
        visuals = [
            (
                _safe_report_name(f"{visual.visual_type.value}{number}", used, f"visual{number}"),
                visual,
            )
            for number, visual in enumerate(page.visuals, start=1)
        ]
        layout.append((page_name, page, visuals))
    return layout


def _shows_measure_by(
    visual: Visual, project: MigrationProject, table: Table, measure: Measure, group_by: list[str]
) -> bool:
    """Whether a visual shows ``measure`` grouped by exactly the query's grouping columns.

    Cognos evaluates a post-aggregation filter on the query's groups; a visual-level measure
    filter is evaluated on the visual's groups, so the two agree only at the same grain.
    """
    measures = {(t.name, m.name) for t in project.tables for m in t.measures}
    bound = any(item.table == table.name and item.name == measure.name for item in visual.fields)
    grouping = {
        (item.table, item.name)
        for item in visual.fields
        if item.aggregation is None and (item.table, item.name) not in measures
    }
    return bound and grouping == {(table.name, name) for name in group_by}


def _reads_source(column: Column, source_column: str | None) -> bool:
    """Whether a column is a plain hidden column reading ``source_column`` (added earlier)."""
    return (
        column.is_hidden
        and not _is_calculated_column(column)
        and (column.source_column or "").casefold() == (source_column or "").casefold()
    )


def _applied_message(record: QueryFilter) -> str:
    fields = ", ".join(
        dict.fromkeys(
            f"measure [{clause.target.name}]"
            if clause.target.kind == "measure"
            else f"{clause.target.table}[{clause.target.name}]"
            for clause in record.clauses
        )
    )
    return (
        f"Query '{record.query}' has a mandatory Cognos detail filter that was applied as a "
        f"locked visual-level filter on {fields} in {len(record.applied_to)} visual(s): "
        f"{', '.join(record.applied_to)}."
    )


def _quoted(names: list[str]) -> str:
    return ", ".join(f"'{name}'" for name in names)


def _pbir_role(visual_type: VisualType, role: str) -> str:
    """Map a generic field role onto the PBIR data role for the given visual type."""
    key = (role or "").strip().lower()
    if visual_type == VisualType.MATRIX:
        return {
            "category": "Rows",
            "rows": "Rows",
            "series": "Columns",
            "columns": "Columns",
        }.get(key, "Values")
    if visual_type in _CHART_VISUAL_TYPES:
        return {"category": "Category", "series": "Series", "values": "Y", "y": "Y"}.get(key, "Y")
    if visual_type in _VISUAL_ROLES:
        roles, default = _VISUAL_ROLES[visual_type]
        return roles.get(key, default)
    return "Values"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def generate_pbip(project: MigrationProject, out_dir: str | Path) -> Path:
    """Convenience wrapper to render a migration project to PBIP files."""
    return PbipGenerator().generate(project, out_dir)
