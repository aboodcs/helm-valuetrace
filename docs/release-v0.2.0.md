# v0.2.0 local release audit

Audit captured 2026-09-29 for candidate branch `audit/extreme-v0.2.0`.
No remote GitHub workflow was run or inspected for a run result.

## Starting state

- Workspace: `/home/abood/helm-valuetrace`; branch: `audit/extreme-v0.2.0`.
- Candidate commit: `04655d3e8a860532ef017062e4fb5d2a8b326a05`.
- Python 3.12.3, Helm 4.3.0+gbec5b06.
- Baseline test suite: `PYTHONPATH=src:.devdeps HELM_BIN=/usr/sbin/helm PIP_FIND_LINKS=/tmp/valuetrace-wheels python3 -m pytest -q` — **641 passed, 0 failed, 0 errors**.

## Defects confirmed before correction

- `final_assignment` could select an earlier child after a later parent scalar replacement; a focused regression failed.
- A later user map layer can expose a chart-default child again after an earlier override and parent replacement. Effective values matched Helm, while ValueTrace reported the earlier user layer as source. A real Helm 4.3.0 differential regression failed on provenance.
- Reporting a JSON/list container containing a sensitive descendant could expose nested credentials. A regression failed for table, JSON, YAML and explanation output.
- `jsonschema` diagnostics contained offending instance values in report JSON and standard error.
- PyYAML parse exceptions echoed malformed source text, including an unclosed sensitive scalar.
- Explanation used a separate trace path and bypassed source-denial enforcement.
- `--set` parsing differed from Helm for typed brace lists, empty brace lists, string brace lists, whitespace and commas inside JSON strings. Incompatible parent/list writes could be silently ignored or accepted incorrectly.
- Escaped equals signs in keys were split at the wrong separator; malformed empty path segments and malformed post-index text could be accepted.
- Installer removed the old plugin before copying or validating its replacement and also uninstalled the unrelated legacy `values-source` plugin.

Each confirmed issue now has a regression test or targeted installer test. The known parser differences were reproduced directly against Helm before correction.

## Implemented

- Winning-source state is separate from append-only assignment history. Parent replacements invalidate the active descendant sources, and default coalescing restores default attribution. Container results receive their effective value while retaining the last contributing source.
- Redaction recurses through maps and arrays, protects descendants of sensitive parents, covers assignment history, and accepts repeatable exact `--redact-path` values. `--no-redact` reveals report values explicitly.
- Schema failures and malformed YAML diagnostics omit offending input values. External schema retrieval is disabled. With a chart schema, `jsonschema` supplies authoritative validation and default-key heuristics are suppressed unless an explicit reference structure was supplied.
- `--explain KEY` is supported alongside the original `explain KEY` subcommand. It shares normal tracing, validation and source policy behavior, supports arrays and deleted/null values, and returns the effective container value.
- Supported `--set*` flags retain Helm's group order. The parser handles tested escaped equals/dot keys, comma-containing JSON strings, brace lists and key/index limits; invalid path/container syntax returns an input error.
- Local installation stages and validates a replacement, then swaps it into place with rollback on failure. It leaves legacy plugins alone. The dependency hook fails rather than silently using missing dependencies.
- A self-contained layered example and fault-injection installer coverage were added. CI actions are pinned to immutable commit SHAs; CI installs checksum-verified Helm 3 and Helm 4 binaries and tests the supported Python matrix.

## Verification

The full local automated test suite passed: **641 collected, 641 passed, 0 failed, 0 errors** (including 18 subtests passed).

### Packaging and Artifact Verification

- Wheel build: successful (`helm_valuetrace-0.2.0-py3-none-any.whl`, built with setuptools 84.0.0, zero deprecation warnings).
- sdist build: successful (`helm_valuetrace-0.2.0.tar.gz`).
- Twine check: successful (`twine check dist/*` reported PASSED for both wheel and sdist).
- Fresh wheel installation: successful into an isolated virtual environment.
- Fresh sdist installation: successful into an isolated virtual environment.
- Artifact CLI verification: `valuetrace --version` successful (`Helm ValueTrace 0.2.0`), `valuetrace --help` successful.
- Example chart execution: successful against real example chart (`examples/layered/chart`).
- Package junk checks: clean (zero matches for `__pycache__`, `.pyc`, `tt.txt`, `feedback.txt`, `megalab`, `.audit-evidence`, or `.devdeps`; legitimate test file `tests/test_feedback_fixes.py` preserved).

### Code Quality and Shell Syntax

- Ruff linting: `ruff check .` passed with 0 errors.
- Ruff formatting: `ruff format --check .` passed with all files cleanly formatted.
- Shell syntax: `bash -n install-local.sh uninstall-local.sh scripts/install.sh` passed.
- Git diff hygiene: `git diff --check` passed with no trailing whitespace or merge conflict markers.

### Differential and Security Verification

- 20/20 tested Helm 4.3.0 differential scenarios matched.
- 0 leaks observed in the tested canary scenarios.
- The example's documented effective values, winning sources, redaction, schema exits, JSON/YAML output and Helm rendering were executed locally; details are in `examples/layered/README.md`. Remote GitHub Actions status is unknown and is not inferred from local CI-equivalent runs.

## Major requirement status

- **VERIFIED:** Audit and baseline; core parent replacement/default restoration provenance; supported file and `--set*` precedence against Helm 3/4; recursive output redaction and tested error-path privacy; focused explanation and source-policy exit; local schema type/required/additional-property checks; staged installer upgrade/rollback fault cases; layered example; local test/lint/shell checks; pinned local CI definition.
- **INCOMPLETE:** Claiming broad Helm equivalence. The support boundary below is intentional. Local plugin installation cannot make Helm's own native `helm plugin update` transactional before the update hook is invoked.
- **UNVERIFIED:** Remote GitHub Actions execution; non-Linux/POSIX shells; power-loss recovery or concurrent installers; external JSON Schema refs and format assertions (explicitly rejected/omitted); all permutations of subchart coalescing.

## Compatibility and limits

- Tested effective values and relevant provenance cases are matched against Helm 3.21.4 and Helm 4.3.0. This does not establish compatibility with every Helm syntax or chart feature.
- The supported `--set-json` form is `KEY=JSON`; object-only `--set-json '{"key":...}'` is unsupported. Remote values inputs and stdin are unsupported.
- Advanced YAML implicit typing, custom tags, cyclic aliases, non-string keys and duplicate source definitions are not a certified parity surface. Quote values whose interpretation matters.
- Display keys keep the prior dotted spelling, so an escaped literal dot can collide textually with a nested path. Explain accepts escaped paths and rejects detected ambiguities; structured row paths retain the displayed spelling.
- `values.schema.json` uses the installed `jsonschema` dialect validator, rejects remote ref retrieval, and does not assert `format`. It is not asserted to match every Helm schema detail.
- Redaction is heuristic and key-name based. It cannot reliably find values under innocuous names, embedded credentials, secret material in source/file names, or secrets in a larger arbitrary string. Review reports before sharing.
- Complex/nested subchart conditions, tags/import-values/multiple aliases, packaged/OCI dependency coalescing and dynamic template analysis remain outside the verified subset.
- The initial untracked `lab-chart/` is user work and was not included in release changes.

## Branch and commit

Branch: `audit/extreme-v0.2.0`; candidate commit: `04655d3e8a860532ef017062e4fb5d2a8b326a05`. Nothing was pushed, tagged, merged, or published. The release candidate is not marked bug-free. Final release readiness depends on maintainer review of the committed diff and remote CI execution.
