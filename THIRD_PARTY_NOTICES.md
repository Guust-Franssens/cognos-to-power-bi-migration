# Third-party provenance

## Cognos converter

- Upstream: https://github.com/navintkr/cognos-to-powerbi
- Upstream base commit: `1994a986c73f37538cde8235f613d1c29031b505`
- Licence: MIT; retained verbatim at `tools\cognos-to-powerbi\LICENSE`.
- Included: Python source, package configuration and selected self-contained tests.
- Local modifications: the source snapshot includes uncommitted pilot patches
  beyond that upstream commit. It is not a pristine upstream release, even though
  the inherited package version remains `0.9.0`.
- Excluded: upstream/pilot Git history, example reports, external report corpora,
  recorded conformance outputs, web frontend and upstream publishing workflows.
- The nested README is a snapshot-specific replacement; package metadata URLs
  continue to identify the upstream project.

The MIT notice applies to this converter subtree. Dependency licences are separate.
No customer or third-party report redistribution rights are implied.

## Toolkit code

The capture utility and its synthetic offline tests were carried from the local
pilot. This repository does not yet grant a blanket open-source licence for toolkit
code outside the converter subtree. Resolve that before any public release.
