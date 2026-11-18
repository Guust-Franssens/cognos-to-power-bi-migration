# Cognos to Power BI Migration

Private development snapshot of a toolkit for supervised Cognos-to-Power BI
migrations. This is a starting point for refinement, not a production release or
a claim of migration fidelity.

## Included now

- A locally patched deterministic converter that emits a first Power BI project
  (PBIP, TMDL and PBIR), with explicit review items for unsupported constructs.
- A headless Cognos oracle-capture tool for specifications, rendered outputs,
  table-level data and lineage, subject to server capabilities.
- Four role-separated project agents and a scaffold for external, isolated
  baseline/working migration units.
- Read-only Power BI Desktop gates for numeric parity, ad-hoc DAX and page
  captures of one unit.
- Offline capture tests and a selected, self-contained subset of converter tests.
- A Python dependency manifest and Windows CI for this subset.

No customer reports, downloaded sample reports, screenshots, captured data,
generated Power BI projects, credentials or pilot Git history are included.
Tests contain small synthetic inputs and mocked responses, not imported report
files. Excluding a report file does not make its copied contents safe to publish.

## Setup

Requires Git, [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer.
The instructions below use PowerShell on Windows.

```powershell
git clone https://github.com/Guust-Franssens/cognos-to-power-bi-migration.git
Set-Location cognos-to-power-bi-migration
uv sync
uv run cognos2pbi --help
uv run python scripts\capture_cognos_oracle.py --help
```

The converter is installed from the source snapshot under
`tools\cognos-to-powerbi`, not from a similarly named package on PyPI.
There is no committed dependency lock yet. `uv sync` resolves dependencies using
your configured package source and creates a local, ignored `uv.lock`. Dependency
versions can therefore differ between machines; a portable lock is release work.

## Convert your own specification

Use only inputs you are authorised to process. Keep inputs and outputs outside
the checkout where possible.

```powershell
uv run cognos2pbi migrate "C:\private\report.xml" `
    --out "C:\private\migration\baseline" --ai none --source-type none
```

This explicitly disables optional AI providers and avoids guessing a physical
data source. The result is a first conversion, not a populated or verified model.
Inspect the generated migration-review files before further authoring. Connecting
real data, processing the model and verifying calculations and rendered pages are
separate steps.

The converter also contains inherited optional AI and API modules. They are not
configured or deployed by setup. Do not select an AI provider for confidential
inputs without separate approval of the provider and data boundary. The upstream
web frontend is not included; hosting the API is outside this snapshot's scope.

## Capture an oracle from a server you control

Supply a Cognos API key through `COGNOS_API_KEY` or `--api-key-file`, never through
a tracked file. An example with a private key file:

```powershell
uv run python scripts\capture_cognos_oracle.py `
    --server "https://your-cognos-host" --namespace "your-namespace" `
    --api-key-file "C:\private\cognos-api-key.txt" `
    --report-id "<Cognos storeID>" --out "C:\private\oracle"
```

A storeID is `i` followed by 32 hexadecimal characters. Read the script's module
docstring and `--help` for the capture, preservation and timeout contract.
Default capture requests PDF, CSV, XLSX and addressable report parts.
It can be slow on an unavailable server; this is not a metadata-only probe.

The manifest distinguishes captured, unsupported and failed outputs. Exit 0 can
include explicitly unsupported legs; it does not mean every visual has an oracle.
Usage is not supplied by these REST APIs, package lineage is incomplete, and
chart data or some visual renders may be unavailable.

## Create an agent run

Run this after converting an authorized Cognos specification. The source project
and numbered run root must be local, and the run root must be outside this
repository:

```powershell
uv run python scripts\create_migration_workspace.py `
    --source-project "C:\private\converted-pbip" `
    --run-parent "C:\private\migration-runs" `
    --run-slug "quarterly-sales" `
    --unit-key "report-name"
```

The tool allocates the next external `<NNN>-<slug>` run directory. Each report
unit lives under `<run>\units\<unit-key>\` and contains `baseline\`, `working\`
and a human-owned `brief.md`. The root `run.json` records run identity, canonical
path, creation time, each project filename and each baseline SHA-256 inventory.
The hash inventory is outside the unit and is not duplicated in a unit manifest.
To add another report to a run, pass its source with `--run-root "<run>"` and a
new `--unit-key`. The command requires exactly one top-level `.pbip`, rejects
source/report/data/export files outside a safe PBIP file set before creating
output, excludes Desktop-local cache/settings and copies no original Cognos
source or oracle. Beyond the project's JSON, TMDL and PBIR files, that set admits
only documented PBIP item files: each item's `.platform`, the semantic model's
`diagramLayout.json`, and the report's `StaticResources\RegisteredResources`
images and JSON and `StaticResources\SharedResources\BaseThemes` JSON. Each item
file is checked by content: it must parse as its type, and a registered resource
must match its suffix and be listed in the report's `definition\report.json`. Other
PBIP extras (for example `CustomVisuals\` or `DAXQueries\`) are still refused.
The content check proves the file type, not its purpose: a BaseTheme may be any
JSON object and a registered `.json` resource any JSON that `report.json` lists,
so JSON data placed there is admitted. A root `.gitignore`, which Desktop writes
next to a saved `.pbip`, is refused like any other non-PBIP file; copy the
project without it.
Baseline files and `run.json` are marked read-only.

Verify a unit against its parent run manifest before and after each agent
handoff:

```powershell
uv run python scripts\create_migration_workspace.py `
    --verify-unit "C:\private\migration-runs\001-quarterly-sales\units\report-name"
```

For a read-only check of every report unit, use:

```powershell
uv run python scripts\create_migration_workspace.py `
    --verify-run "C:\private\migration-runs\001-quarterly-sales"
```

Changed, added or missing baseline files block verification. Do not edit baseline
or regenerate its hashes to hide a change. Agents may edit only their owned files
under `working\`; the run root itself is outside unit scope. This is not a
cryptographic trust boundary against an agent with same-user write access to the
run root, since that user can also rewrite `run.json`. Do not commit the run.

## Gate a unit in Power BI Desktop

Three read-only gates judge one unit's working project while it is open in Power
BI Desktop. They need Windows, Power BI Desktop and the ADOMD.NET client (by
default DAX Studio's copy under `%LOCALAPPDATA%\Programs\DAX Studio\bin`, or pass
`--adomd`). Captures also need the `powerbi-desktop` CLI from
`@microsoft/powerbi-desktop-bridge-cli` on PATH. `pythonnet` is supplied per run
with `--with`; it is not a project dependency.

Open the working project with `Start-Process`, so its path is on the process
command line, and read the pid from there:

```powershell
$unit = "C:\private\migration-runs\001-quarterly-sales\units\report-name"
Start-Process "$unit\working\Report.pbip"
Get-CimInstance Win32_Process -Filter "Name='PBIDesktop.exe'" | Select-Object ProcessId, CommandLine

uv run --with pythonnet python scripts\parity_check.py --unit $unit --pid <pid> `
    --oracle "C:\private\parity\report-name.json" `
    --data-expression DataFolder --data-path "C:\private\data"
uv run --with pythonnet python scripts\dax_query.py --unit $unit --pid <pid> --dax 'EVALUATE ROW("n", 1)'
uv run python scripts\capture_pages.py --unit $unit --pid <pid>
```

Every gate first verifies the unit against `run.json` and refuses with exit 2
unless the pid has that unit's working `.pbip` open: its command line must name
exactly one absolute `.pbip`, the working one, so a pid on the baseline, another
unit or a closed instance is refused. The working `.pbip` must name one report,
by a relative path inside `working\`, and that report's `definition.pbir` must
name its model `byPath`, also inside `working\`; a project pointed at the
baseline or another unit is refused. The command line only proves the file
opened at launch; a file opened later with File > Open is invisible to it, so
never open another file in a gated instance. Exit 2 means nothing was checked,
and exit 3 means the gate crashed (`CRASHED: <type>: <message>` on stderr). The
gates write stdout and stderr as UTF-8 whatever the console code page, so read a
piped gate as UTF-8.

Parity exits 0 when every check passes and 1 on any mismatch, missing row, row
key repeated in the query result, unreadable result value or DAX error, and on
an unexpected row only when the check sets `exact_rows: true`. An oracle that
repeats a JSON key in any object, or a check `id`, is refused with exit 2. A
whole-number float key (a Decimal year column read as `2024.0`) matches the
oracle key `2024`. The DAX gate accepts one query that starts with `EVALUATE`
or `DEFINE`, holds an `EVALUATE`, and has no `;` outside strings and comments;
it keeps every column of a result whose column names repeat, suffixing the
repeats `#2`, `#3`, and so on. `--data-expression` and `--data-path` go
together, and the data guard accepts only a shared expression that is a text
parameter (one M string literal, optionally with `meta [...]`) naming exactly
the `--data-path` folder. The guard reads only that parameter's current value:
a partition that does not use the parameter, or data loaded before the value
was set, still passes, so refresh after changing it.

Captures go to `<unit>\captures\working` unless `--out` names a folder below
`<unit>\captures\` or outside the run root, this checkout and any other run. No
folder from the run root down to the output, `captures` included, may be a
link, junction or other reparse point, and a folder that resolves inside any
unit's `baseline` or `working` is refused. A rerun deletes only the PNGs its own
record names and refuses a folder holding anything else, including a stray file
or a record it cannot read. A capture stopped with Ctrl+C records status
`interrupted` and the PNGs written so far, so the next run replaces it. A killed
process leaves its record `running`, naming no PNGs: on its own that record is
cleared and the next run proceeds, but with PNGs beside it the next run refuses
(exit 2) and deletes nothing. Inspect that folder, then move it away or pass
another `--out`. Cleanup deletes the record last, so a Ctrl+C during cleanup
leaves only PNGs the record still names. Exit 1 flags a failed or suspiciously
small page, or a PNG count that differs from the pages in the report's
`pages.json`; it still needs a look.

The oracle is a JSON object whose `checks` list holds DAX queries with expected
values (finite numbers, text or null); its format is in the module docstring of
`scripts\parity_check.py`. Duplicate JSON keys, duplicate check ids, NaN,
booleans and a hard-linked oracle are refused. Keep it outside the run root and
this checkout: the gate refuses one inside either, and prints the SHA-256 of an
oracle it accepts.
Writing an oracle is a human decision; no generator from Cognos captures is
included yet.

The gates never refresh, save or edit a model. Model refresh, persistence and the
Desktop bridge are environment prerequisites, not packaged here: process the
model after the last TMDL edit, then cold-reopen and rerun parity. If a
prerequisite is unavailable, agents report a blocked or unverified gate, never a
sign-off. The gates were validated live once, on Power BI Desktop 2.160.579.0
(Microsoft Store) with a synthetic model; other versions are unverified.

## Local checks

```powershell
uv run pytest scripts\tests tools\cognos-to-powerbi\tests -q -p no:cacheprovider
uv run ruff check scripts
uv run ruff format --check scripts
uv run ruff check tools\cognos-to-powerbi
uv run ruff format --check tools\cognos-to-powerbi
uv run mypy --config-file tools\cognos-to-powerbi\pyproject.toml tools\cognos-to-powerbi\src
```

These are offline code checks, not a live Cognos or Power BI fidelity sign-off.
Corpus and conformance suites that need external reports or recorded outputs are
not bundled. The selected converter tests do not represent the pilot's full
coverage.

## Build a code-only source archive

The source allowlist is `release-files.txt`. It names every file admitted to the
source archive. Validate it from a clean checkout:

```powershell
uv run python scripts\package_source.py
```

To create a ZIP from the clean committed `HEAD`, choose a new destination outside
the checkout:

```powershell
uv run python scripts\package_source.py `
    --output "C:\private\dist\cognos-migration-source.zip"
```

The archive command refuses a dirty checkout, unlisted or missing tracked files,
report/data/model file extensions, common local-user paths and high-confidence
credential patterns. It verifies the archive entries against the allowlist. This
is a guardrail, not proof that arbitrary text or code contains no copied report
material: review every proposed allowlist change before committing it. No report
corpus, private evidence or customer content is permitted in a release.

## Next refinements

The intended workflow is deterministic conversion, specialist semantic/report
repairs, then independent validation. Report-specific data generators are not
bundled. The generic unit scaffold and agent instructions do not replace a real
oracle, Power BI Desktop validation, or a customer-approved fidelity bar.

Further release work includes a portable dependency lock, an optional
external-corpus test harness, a support matrix, clean-machine Desktop validation,
a packaged model-refresh step, an oracle generator from Cognos captures and a
licensing decision for the toolkit's original code. No publishing workflow
or automatic deployment is configured.

## Provenance and licensing

The converter is derived from
[navintkr/cognos-to-powerbi](https://github.com/navintkr/cognos-to-powerbi)
under MIT, with its licence retained. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
The converter's licence does not grant redistribution rights to report inputs or
outputs. This private snapshot does not assign a blanket licence to the rest of
the toolkit.
