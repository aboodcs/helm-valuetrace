# Changelog

All notable changes to Helm ValueTrace are documented in this file.

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
- Keep usage errors on exit code `1` and strict validation failures on exit code `2`
- Differentially test final values against Helm 3.21.4 and Helm 4.2.4
- Ship as a focused tool-only repository with self-contained temporary test fixtures
- Provide grouped CLI help with examples, precedence rules, and exit-code documentation
