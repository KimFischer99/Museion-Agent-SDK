# License Inventory

<!-- license-gate
status: pass
# Original code is MIT; the user explicitly asked for the selected Skills to ship with the
# package as passive deployment reference material.
# pass means the content boundary and the provenance inventory check out; it does NOT mean
# third-party redistribution licensing has been verified.
-->

This file is the data source for the P0 license inventory and for the release gate.
`tools/license_gate.py` reads the `<!-- license-gate -->` marker above and the audit hashes, and
checks the forbidden paths and the explicitly registered reference files. Gate pass and
third-party license verification are two different states; the source licensing of the bundled
references has not been verified overall.

## 1. Original code - MIT

`LICENSE` (MIT, Copyright (c) 2026 Kim Fischer) covers the following original content of this
repository:

- the original implementation under `src/proactive_sdk/`; it does not cover the third-party
  reference files in `_deployment_reference/skills/`
- `schemas/` - the interoperability contract schemas
- `tools/` - audit reproduction and license gate tooling
- `examples/`, `tests/` - original reference slices and tests
- `docs/` - this inventory and the project documentation
- `tests/fixtures/*.json` - machine-readable **metadata** (names, hashes, issue flags) derived
  from the user-provided attachment; it contains no Skill body text

## 2. User-provided Muse material and bundled references

| Object | Location | Status |
|---|---|---|
| The selected Skills (88 entry points, 377 source files) | Source: `src/proactive_sdk/_deployment_reference/skills/`; delivery: `release/skills/` | Separate from the runtime wheel, flattened as passive deployment reference; ships `NOTICE.md` and `manifest.json` alongside; the source license status is preserved |
| Muse attachment unpack (88 Skills, runtime scripts, platform docs) | `01/private-vendor/muse-sdk/` | Not tracked by git; local reference only |
| Verbatim copy of `hatch_hook_runtime.sh` | `01/private-vendor/muse-reuse/` | SHA-256 `c87af221...58e741`; no redistribution license found |
| Local reference documents and caches | `01/` (including `muse-refer/`) | Not tracked by git; not distributed with the package |

Current delivery convention (2026-10-07): the runtime delivery lives in `release/runtime/` and
contains the SDK wheel, the assembled `app.py` and installation instructions; the wheel contains
neither Skills nor the Skills `NOTICE.md`. The Skills are delivered as a separate directory at
`release/skills/`, kept apart from the complete private snapshot and from the internal
construction documents.
`release/skills/manifest.json` records the source, the file count, one directory checksum and the
license verification status; it does not store a per-file hash list. `release/skills/NOTICE.md`
states that the reference files are not covered by the project's MIT license.
`tools/license_gate.py` and `tools/package_gate.py` preserve the provenance boundary, the hash
checks and the sensitive-file gate; no other original file may be mixed into the runtime or into
the Skills delivery.

## 3. Third-party runtime dependencies

The first version's core depends only on the Python 3.11+ standard library (including zoneinfo).
The tests likewise use only the standard library. The `schema` extra in `pyproject.toml`
(jsonschema) is an optional conformance-tooling dependency, not a runtime dependency; any
third-party dependency must be registered in this inventory and pinned before it is introduced.

## 4. Blocking rules

- This marker's `status:` being `blocked` => the release is blocked.
- A tracked file matching an audit hash, but not at a designated deployment reference path or in
  its manifest => the release is blocked.
- Deployment references missing, changed, or a new unregistered file added => the release is
  blocked; actual private key blocks are still blocked by the artifact scan.
- Bundled references grant no new rights of use or redistribution; the original file-level
  licenses and provenance notices must be preserved.
