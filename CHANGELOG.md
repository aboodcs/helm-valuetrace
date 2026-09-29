# Changelog

All notable changes to Helm ValueTrace are documented in this file.

## v0.2.0

### Independent audit corrections (local candidate)

- Report all contributing container sources; attribute list padding and restored global values correctly, and index provenance lookup to avoid repeated whole-history scans.
- Correct nested null coalescing and indexed scalar/container transitions against Helm 3.21.4 and 4.3.0.
- Bound YAML composition, alias expansion and file reads; correct tested scalar resolution, alias/list source locations and CLI literal/JSON/overflow/escape semantics.
- Preserve literal dotted and bracketed sensitive names in redaction; use typed exact paths, escape terminal controls and add unambiguous paths to structured reports.
- Handle interleaved explanation arguments, absent paths and empty explanation options explicitly; retain policy exits across reporting modes.
- Isolate schema evaluation with resource limits; reject malformed/unknown-dialect schemas, validate local child schemas and require a schema for strict schema mode.
- Reject unsupported dependency metadata and ambiguous/multiple-root archives instead of silently returning partial or incorrect results.
- Remove implicit working-directory imports from plugin launch and installation; improve archive cleanup, streaming limits, signal rollback and uninstall error propagation.
- Verify installed wheel/sdist imports outside the checkout, ship the declared typing marker, declare referencing directly, and update vulnerable build-tool baselines.
- Add reproducible adversarial regressions, bounded deterministic generated cases and local audit evidence. See tt.txt for executed tests and remaining limitations.

### Release-candidate corrections

- Track surviving sources independently from history, fixing attribution after parent replacement and chart-default restoration.
- Recursively redact mappings, arrays, sensitive ancestors and historical containers; add repeatable `--redact-path`.
- Remove instance values from schema errors and YAML snippets from parse errors.
- Add `--explain KEY`; use effective container values and preserve validation/source-policy exits in explain mode.
- Correct typed/empty brace lists, string brace lists, JSON comma parsing, empty JSON values and whitespace preservation; reject incompatible container writes and indexes beyond Helm's limit.
- Use schema validation instead of default-key guesses when a schema exists; reject remote schema resolution. Require jsonschema >=4.18 for the offline registry API.
- Stage local upgrades with rollback; fail dependency installation explicitly and preserve legacy plugins.
- Add a reproducible layered chart, security/provenance/installer regressions and a Python 3.10–3.12 × Helm 3/4 CI matrix with commit-pinned actions.

### New Features

- **`--set-string KEY=VALUE`** — Set a value as a string without type coercion
- **`--set-file KEY=PATH`** — Set a value to the contents of a file
- **`--set-json KEY=JSON`** — Set a value to a JSON-parsed object, array, or scalar
- **`--set-literal KEY=VALUE`** — Set a literal string value (backslashes/commas not special)
- **`.tgz` / `.tar.gz` chart support** — Pass a local Helm archive instead of a directory; secure extraction with path-traversal, symlink, and size protection
- **`explain KEY` subcommand** — Show full provenance history for a single key
- **Secret redaction (ON by default)** — Sensitive key values (passwords, tokens, keys, etc.) are automatically redacted in all output; disable with `--no-redact`; extend with `--redact-pattern GLOB`
- **JSON Schema validation** — Validates `values.schema.json` when present; `--strict-schema` exits 2 on failure; schema errors appear in structured output
- **`--template-analysis`** — Static scan of chart templates for `.Values.*` references; reports unused values (informational only)
- **`--experimental-subcharts`** — Experimental subchart/dependency support; processes `charts/` directory and propagates `.global` values per Helm semantics; prints warning and known limitations
- **Exit code scheme (0-4)** — Standardized exit codes: `0` (success), `1` (usage/input error), `2` (validation/policy failure), `3` (chart/dependency error such as missing Chart.yaml or corrupted archive), `4` (internal error)

### Breaking Changes

- Existing options are retained. Corrected semantics may change reports: `{}` via `--set` means `[""]` as in Helm; schema-aware unknown detection, recursive redaction, accurate restored sources and explain policy exits supersede earlier behavior.

### Security

- Redaction is **ON by default**. Detected sensitive values are masked; heuristic detection cannot identify every secret. Review reports before sharing.
- `.tgz` extraction rejects absolute paths, path traversal (`../`), symlinks, and archives exceeding 50 MiB.
- No network access — OCI registries and remote charts are unsupported.

### Known Limitations

- `--experimental-subcharts` does not run `helm dependency update`; pre-fetched charts must be present in `charts/`
- `--template-analysis` detects only static `.Values.*` references; dynamic patterns are not detected
- Schema array constraints are validated; external schema references and `format` assertions are unsupported.
- OCI chart references (`oci://`) are not supported

## v0.1.0

- Trace final Helm values to their source file and line number
- Preserve assignment history across chart defaults, values files, and `--set`
- Detect unknown keys and suggest likely corrections
- Compare values files against a non-merged reference structure
- Report additional and missing reference keys
- Support table, JSON, and YAML output
- Provide strict exit codes for CI/CD validation
- Support permanent local installation with Helm 3 and Helm 4
- Match Helm's scalar typing for supported `--set` values
- Coalesce chart defaults after merging user values, including map type changes
- Match Helm 3/4 null handling based on the invoking Helm major version
- Treat known mapping parents as valid override paths in strict validation
- Keep usage errors on exit code `1` and strict validation failures on exit code `2`
- Differentially test final values against Helm 3.21.4 and Helm 4.2.4
- Block forbidden values files with repeatable `--deny-source` filename or glob policies
- Ship as a focused tool-only repository with self-contained temporary test fixtures
- Provide grouped CLI help with examples, precedence rules, and exit-code documentation
