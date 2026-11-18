# Cognos to Power BI Migration

Private development snapshot of a toolkit for supervised Cognos-to-Power BI
migrations. This is a starting point for refinement, not a production release or
a claim of migration fidelity.

## Included now

- A locally patched deterministic converter that emits a first Power BI project
  (PBIP, TMDL and PBIR), with explicit review items for unsupported constructs.
- A headless Cognos oracle-capture tool for specifications, rendered outputs,
  table-level data and lineage, subject to server capabilities.
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

## Next refinements

The intended workflow is deterministic conversion, specialist semantic/report
repairs, then independent validation. The pilot's agent definitions, Desktop
refresh helpers, report-specific data generators and orchestration are not yet
part of this clean snapshot. They need portable configuration, a generic unit
contract and dependency/provenance checks before import.

Further release work includes a portable dependency lock, a distribution allowlist and automated content
checks, an optional external-corpus test harness, a support matrix, clean-machine
Desktop validation and a licensing decision for the toolkit's original code.
No publishing workflow or automatic deployment is configured.

## Provenance and licensing

The converter is derived from
[navintkr/cognos-to-powerbi](https://github.com/navintkr/cognos-to-powerbi)
under MIT, with its licence retained. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
The converter's licence does not grant redistribution rights to report inputs or
outputs. This private snapshot does not assign a blanket licence to the rest of
the toolkit.
