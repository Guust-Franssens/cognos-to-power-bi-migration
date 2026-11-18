# Converter source snapshot

This directory contains a locally patched source snapshot derived from
[navintkr/cognos-to-powerbi](https://github.com/navintkr/cognos-to-powerbi).
The original MIT licence is retained in `LICENSE`.

Use the repository-root [README](../../README.md) for setup and supported scope.
The root uv project installs this directory as its local converter dependency.

This is not a pristine upstream release. Its inherited version is `0.9.0`, but
the snapshot includes additional pilot changes. See the root
[third-party notices](../../THIRD_PARTY_NOTICES.md) for the upstream base commit.

No example reports, downloaded report corpora or recorded conformance outputs are
bundled. Only selected self-contained unit tests are included. Optional API/AI
modules are inherited source, not a qualified hosted service; the upstream web
frontend is absent.
