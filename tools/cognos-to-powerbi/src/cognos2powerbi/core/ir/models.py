"""Pydantic models that define the vendor-neutral intermediate representation (IR)."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class DataType(str, Enum):
    """Power BI / TMDL column data types."""

    STRING = "string"
    INT64 = "int64"
    DOUBLE = "double"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE_TIME = "dateTime"

    @classmethod
    def from_cognos(cls, cognos_type: str | None) -> DataType:
        """Map a Cognos data-item usage/datatype hint to a TMDL data type."""
        if not cognos_type:
            return cls.STRING
        value = cognos_type.strip().lower()
        # Strip any precision/length suffix, for example "decimal(18,2)" or "varchar(50)".
        base = value.split("(", 1)[0].strip()
        if base in {"date", "datetime", "datetime2", "smalldatetime", "timestamp", "time"}:
            return cls.DATE_TIME
        if base in {"bool", "boolean", "bit"}:
            return cls.BOOLEAN
        if base in {"decimal", "numeric", "money", "smallmoney", "currency"}:
            return cls.DECIMAL
        if base in {"double", "float", "float64", "real", "number"}:
            return cls.DOUBLE
        if "int" in base or base in {"long", "short", "byte", "serial"}:
            return cls.INT64
        return cls.STRING


class Severity(str, Enum):
    """Severity of a manual-review flag raised during migration."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class VisualType(str, Enum):
    """Normalized Power BI visual types."""

    TABLE = "tableEx"
    MATRIX = "pivotTable"
    COLUMN_CHART = "columnChart"
    BAR_CHART = "barChart"
    LINE_CHART = "lineChart"
    PIE_CHART = "pieChart"
    CARD = "card"
    UNKNOWN = "tableEx"
    # "columnChart"/"barChart" above are Power BI's stacked variants.
    CLUSTERED_COLUMN_CHART = "clusteredColumnChart"
    CLUSTERED_BAR_CHART = "clusteredBarChart"
    HUNDRED_PERCENT_STACKED_COLUMN_CHART = "hundredPercentStackedColumnChart"
    HUNDRED_PERCENT_STACKED_BAR_CHART = "hundredPercentStackedBarChart"
    AREA_CHART = "areaChart"
    STACKED_AREA_CHART = "stackedAreaChart"
    HUNDRED_PERCENT_STACKED_AREA_CHART = "hundredPercentStackedAreaChart"
    DONUT_CHART = "donutChart"
    TREEMAP = "treemap"
    SCATTER_CHART = "scatterChart"
    WATERFALL_CHART = "waterfallChart"
    LINE_CLUSTERED_COLUMN_COMBO_CHART = "lineClusteredColumnComboChart"
    LINE_STACKED_COLUMN_COMBO_CHART = "lineStackedColumnComboChart"


class DataSourceKind(str, Enum):
    """Supported physical data sources for the generated Power BI model."""

    SQL_SERVER = "sqlServer"
    NONE = "none"


class TableRole(str, Enum):
    """The role a table plays in a star schema."""

    FACT = "fact"
    DIMENSION = "dimension"
    DATE = "date"
    BRIDGE = "bridge"
    UNKNOWN = "unknown"


class Cardinality(str, Enum):
    """Relationship cardinality, named so the value maps onto TMDL endpoints.

    A relationship in TMDL is oriented from the ``from`` (many) side to the ``to`` (one) side.
    ``MANY_TO_ONE`` is the Power BI default and needs no explicit cardinality tokens.
    """

    MANY_TO_ONE = "manyToOne"
    ONE_TO_MANY = "oneToMany"
    ONE_TO_ONE = "oneToOne"
    MANY_TO_MANY = "manyToMany"


class CrossFilterDirection(str, Enum):
    """Cross-filter direction for a relationship (TMDL crossFilteringBehavior)."""

    SINGLE = "oneDirection"
    BOTH = "bothDirections"


class ReviewFlag(BaseModel):
    """A migration item that needs human or AI review."""

    code: str
    message: str
    severity: Severity = Severity.WARNING
    source_ref: str | None = None


class QueryRole(str, Enum):
    """The role a Cognos query plays in a report's query graph.

    A query can be an *output* query (referenced by a layout object such as a list or chart), a
    *join* query (its source combines other queries with a ``joinOperation``), a *union* query (its
    source combines other queries with a set ``queryOperation``), a *reference* query (its source is
    a single ``queryRef`` to another query), a *detail* query (referenced only by other queries, not
    by any layout), or ``unknown`` when the role cannot be determined.
    """

    OUTPUT = "output"
    JOIN = "join"
    UNION = "union"
    REFERENCE = "reference"
    DETAIL = "detail"
    UNKNOWN = "unknown"


class QueryEdgeKind(str, Enum):
    """How two queries are connected in the query graph."""

    JOIN = "join"
    UNION = "union"
    REFERENCE = "reference"


class QueryNode(BaseModel):
    """A node in the report's query graph: one Cognos ``<query>`` classified by role."""

    name: str
    role: QueryRole = QueryRole.UNKNOWN
    is_output: bool = False


class QueryEdge(BaseModel):
    """A directed edge in the query graph.

    The edge points from the composite query (the one whose source combines others) to each operand
    query it consumes. ``condition`` carries the raw join expression when the edge is a join.
    """

    from_query: str
    to_query: str
    kind: QueryEdgeKind
    condition: str | None = None


class QueryGraph(BaseModel):
    """A structured representation of the report's queries and how they relate."""

    nodes: list[QueryNode] = Field(default_factory=list)
    edges: list[QueryEdge] = Field(default_factory=list)


class FilterUse(str, Enum):
    """How Cognos applies a detail filter, taken from the ``use`` attribute.

    - ``required`` (no ``use`` attribute) - the filter is always applied.
    - ``optional`` (``use="optional"``) - applied only when a prompt value is supplied.
    - ``prohibited`` (``use="prohibited"``) - the filter is defined but disabled by Cognos.
    """

    REQUIRED = "required"
    OPTIONAL = "optional"
    PROHIBITED = "prohibited"

    @classmethod
    def from_attribute(cls, use: str | None) -> FilterUse:
        """Map a Cognos ``use`` attribute value to a :class:`FilterUse`."""
        value = (use or "").strip().lower()
        if value == "optional":
            return cls.OPTIONAL
        if value == "prohibited":
            return cls.PROHIBITED
        return cls.REQUIRED


class FilterValue(BaseModel):
    """A literal operand of a translated filter condition.

    ``value`` is the literal as text: a string without its quotes (and with doubled quotes
    undone), a number as written (for example ``1000`` or ``-2.5``), or a date/datetime in ISO
    form (``2024-01-31`` or ``2024-01-31T10:00:00``).
    """

    kind: Literal["string", "number", "date"]
    value: str


class FilterCondition(BaseModel):
    """A target-neutral condition tree for one filtered item.

    Leaves compare the item with ``values``: ``eq``/``lt``/``le``/``gt``/``ge`` take one value,
    ``in`` a list, ``between`` two inclusive bounds, ``starts_with``/``contains`` one string, and
    ``is_null`` none. ``not``/``and``/``or`` combine ``operands``. ``item`` is the Cognos reference
    a leaf filters, as written in the source expression.
    """

    op: Literal[
        "eq",
        "lt",
        "le",
        "gt",
        "ge",
        "in",
        "between",
        "is_null",
        "starts_with",
        "contains",
        "not",
        "and",
        "or",
    ]
    item: str | None = None
    values: list[FilterValue] = Field(default_factory=list)
    operands: list[FilterCondition] = Field(default_factory=list)


class FilterTarget(BaseModel):
    """The model object a translated filter clause applies to.

    ``kind`` is ``column`` for a row-level filter or ``measure`` for a filter on an aggregated
    value (Cognos ``postAutoAggregation``). ``add_column`` marks a plain model reference that no
    data item selects: a generator that applies the filter must first add it as a hidden column
    reading ``source_column`` with ``data_type``. For a measure, ``group_by`` lists the query's
    grouping columns, the level at which Cognos evaluates the aggregated value.
    """

    table: str
    name: str
    kind: Literal["column", "measure"] = "column"
    data_type: DataType | None = None
    source_column: str | None = None
    add_column: bool = False
    group_by: list[str] = Field(default_factory=list)


class FilterClause(BaseModel):
    """One filtered item of a detail filter: where it applies and the condition to apply.

    ``keeps_null`` records whether Cognos keeps rows whose item is NULL. Cognos evaluates the
    filter with SQL three-valued logic, so negative and ordering predicates drop NULL rows.
    """

    item: str
    target: FilterTarget
    condition: FilterCondition
    keeps_null: bool = False


class QueryFilter(BaseModel):
    """A Cognos detail filter extracted with its application semantics.

    ``query`` is the query the filter belongs to, ``expression`` is the raw Cognos filter
    expression, and ``use`` records how Cognos applies it. ``parameters`` lists any prompt
    parameter names referenced by the expression (for example ``p_From_Date``).
    ``post_aggregation`` is the Cognos ``postAutoAggregation`` attribute: the filter applies to
    aggregated values per group instead of to detail rows.

    A mandatory filter that translates statically has one ``clauses`` entry per filtered item
    (the clauses are ANDed). Otherwise ``untranslated_code`` and ``untranslated_reason`` say why
    it could not be translated. A generator that applies the clauses lists where in
    ``applied_to`` (for PBIP, ``page/visual`` names); when it cannot apply translated clauses it
    records why in ``untranslated_code`` and ``untranslated_reason`` instead.
    """

    query: str
    expression: str
    use: FilterUse = FilterUse.REQUIRED
    parameters: list[str] = Field(default_factory=list)
    post_aggregation: bool = False
    clauses: list[FilterClause] = Field(default_factory=list)
    untranslated_code: str | None = None
    untranslated_reason: str | None = None
    applied_to: list[str] = Field(default_factory=list)


class PromptControlType(str, Enum):
    """The Cognos prompt control used to collect a parameter value."""

    DATE = "date"
    DATE_TIME = "dateTime"
    TIME = "time"
    VALUE = "value"
    SELECT_VALUE = "selectValue"
    TEXT = "text"
    INTERVAL = "interval"
    GENERATED = "generated"
    UNKNOWN = "unknown"


class Prompt(BaseModel):
    """Structured metadata for a Cognos prompt (a user-facing report parameter).

    Captured from ``<promptPages>`` so a generator can later emit an RDL ``ReportParameter`` or a
    Power BI parameter/slicer. Only values present in the source are populated.
    """

    parameter_name: str
    control_type: PromptControlType = PromptControlType.UNKNOWN
    data_type: DataType = DataType.STRING
    caption: str | None = None
    required: bool = True
    multi_select: bool = False
    source_query: str | None = None
    values_query: str | None = None
    value_column: str | None = None
    display_column: str | None = None
    default_values: list[str] = Field(default_factory=list)
    range_prompt: bool = False


class Style(BaseModel):
    """Presentation style extracted from a Cognos report, in target-neutral terms.

    Only properties that were explicitly set in the source are populated; ``None`` means "not
    specified" so a generator can fall back to its own default without overwriting an intended
    value. Colors are stored as the source token (a hex value like ``#005DAB`` or a CSS color name).
    """

    font_family: str | None = None
    font_size_pt: float | None = None
    bold: bool = False
    italic: bool = False
    underline: bool = False
    color: str | None = None
    background_color: str | None = None
    text_align: str | None = None
    vertical_align: str | None = None

    def is_empty(self) -> bool:
        """Return True when no style property is set."""
        return not any(
            (
                self.font_family,
                self.font_size_pt,
                self.bold,
                self.italic,
                self.underline,
                self.color,
                self.background_color,
                self.text_align,
                self.vertical_align,
            )
        )


class TextBlock(BaseModel):
    """A block of static layout text (letterhead, signature) with its extracted style."""

    text: str
    style: Style | None = None


class Column(BaseModel):
    """A column in a semantic-model table."""

    name: str
    data_type: DataType = DataType.STRING
    source_column: str | None = None
    cognos_expression: str | None = None
    dax_expression: str | None = None
    format_string: str | None = None
    is_calculated: bool = False
    needs_calculation: bool = False
    is_hidden: bool = False
    is_key: bool = False
    is_foreign_key: bool = False
    summarize_by: str | None = None
    data_category: str | None = None


class Measure(BaseModel):
    """A measure (aggregated calculation) in a semantic-model table."""

    name: str
    dax_expression: str | None = None
    cognos_expression: str | None = None
    format_string: str | None = None
    needs_review: bool = False


class Table(BaseModel):
    """A semantic-model table derived from a Cognos query."""

    name: str
    source_query: str | None = None
    columns: list[Column] = Field(default_factory=list)
    measures: list[Measure] = Field(default_factory=list)
    role: TableRole = TableRole.UNKNOWN
    data_category: str | None = None
    is_date_table: bool = False

    def column(self, name: str) -> Column | None:
        """Return the column with the given name, or ``None``."""
        for col in self.columns:
            if col.name == name:
                return col
        return None


class Relationship(BaseModel):
    """A relationship between two tables.

    The relationship is oriented so ``from`` is the many side and ``to`` is the one side,
    matching how Power BI / TMDL serialize a single-column relationship.
    """

    from_table: str
    from_column: str
    to_table: str
    to_column: str
    is_active: bool = True
    name: str | None = None
    cardinality: Cardinality = Cardinality.MANY_TO_ONE
    cross_filter: CrossFilterDirection = CrossFilterDirection.SINGLE


class DataSource(BaseModel):
    """Physical data source used to populate the generated semantic model.

    Cognos report specifications reference a logical model, not a physical connection, so the
    generator emits a parameterized Power Query that the user points at their own database. When
    the kind is ``NONE`` the generator emits empty placeholder tables instead.
    """

    kind: DataSourceKind = DataSourceKind.SQL_SERVER
    server: str = "localhost"
    database: str = "AdventureWorks"
    schema_name: str = "dbo"


class VisualField(BaseModel):
    """A field bound to a visual role (for example rows, values, category)."""

    table: str
    name: str
    role: str = "values"
    header_style: Style | None = None
    cell_style: Style | None = None
    # How a column is summarized where the visual needs one value (``min`` shows the first
    # value); None binds the column as is. Ignored for measures.
    aggregation: Literal["sum", "min", "count"] | None = None


class Visual(BaseModel):
    """A visual placed on a report page."""

    title: str | None = None
    visual_type: VisualType = VisualType.TABLE
    fields: list[VisualField] = Field(default_factory=list)
    values_on_rows: bool = False
    x: float = 0.0
    y: float = 0.0
    width: float = 480.0
    height: float = 320.0


class ReportPage(BaseModel):
    """A page in the target Power BI report."""

    name: str
    display_name: str
    visuals: list[Visual] = Field(default_factory=list)
    header_blocks: list[TextBlock] = Field(default_factory=list)
    footer_blocks: list[TextBlock] = Field(default_factory=list)


class MigrationProject(BaseModel):
    """The complete migration unit produced by parsers and consumed by generators."""

    name: str
    source_path: str | None = None
    data_source: DataSource = Field(default_factory=DataSource)
    tables: list[Table] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    pages: list[ReportPage] = Field(default_factory=list)
    query_graph: QueryGraph = Field(default_factory=QueryGraph)
    filters: list[QueryFilter] = Field(default_factory=list)
    prompts: list[Prompt] = Field(default_factory=list)
    review_flags: list[ReviewFlag] = Field(default_factory=list)

    def add_flag(
        self,
        code: str,
        message: str,
        severity: Severity = Severity.WARNING,
        source_ref: str | None = None,
    ) -> None:
        """Record a manual-review flag on the project."""
        self.review_flags.append(
            ReviewFlag(code=code, message=message, severity=severity, source_ref=source_ref)
        )
