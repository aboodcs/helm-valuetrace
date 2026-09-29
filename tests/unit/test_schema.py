"""Tests for JSON Schema validation (Phase 6)."""

from __future__ import annotations

import json
import sys
import unittest
from io import StringIO
from pathlib import Path

FIXTURES = Path(__file__).parent.parent / "fixtures" / "schema"
VALID_CHART = FIXTURES / "valid"
INVALID_CHART = FIXTURES / "invalid"

try:
    import jsonschema  # noqa: F401

    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

SKIP_MSG = "jsonschema not installed"


class TestLoadSchema(unittest.TestCase):
    def test_loads_schema_when_present(self) -> None:
        from helm_valuetrace.analysis.schema import load_schema

        schema = load_schema(VALID_CHART)
        self.assertIsNotNone(schema)
        self.assertIn("properties", schema)

    def test_returns_none_when_absent(self) -> None:
        from helm_valuetrace.analysis.schema import load_schema

        security_chart = Path(__file__).parent.parent / "fixtures" / "security"
        result = load_schema(security_chart)
        self.assertIsNone(result)

    def test_malformed_schema_raises_valuetraceerror(self) -> None:
        import tempfile

        from helm_valuetrace.analysis.schema import load_schema
        from helm_valuetrace.core import ValueTraceError

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            (tmp_dir / "values.schema.json").write_text("{ invalid json")
            with self.assertRaises(ValueTraceError) as ctx:
                load_schema(tmp_dir)
            self.assertIn("Malformed values.schema.json", str(ctx.exception))


@unittest.skipUnless(HAS_JSONSCHEMA, SKIP_MSG)
class TestValidateValues(unittest.TestCase):
    def test_valid_values_no_errors(self) -> None:
        from helm_valuetrace.analysis.schema import load_schema, validate_values

        schema = load_schema(VALID_CHART)
        values = {"replicaCount": 2, "image": {"repository": "nginx", "tag": "1.21"}}
        errors = validate_values(values, schema)
        self.assertEqual(errors, [])

    def test_invalid_type_produces_error(self) -> None:
        from helm_valuetrace.analysis.schema import load_schema, validate_values

        schema = load_schema(INVALID_CHART)
        values = {
            "replicaCount": "not-an-integer",
            "image": {"repository": "nginx"},
            "service": {"port": 99999},
        }
        errors = validate_values(values, schema)
        self.assertGreater(len(errors), 0)
        keys = [e.key for e in errors]
        self.assertIn("replicaCount", keys)

    def test_schema_error_has_message(self) -> None:
        from helm_valuetrace.analysis.schema import load_schema, validate_values

        schema = load_schema(INVALID_CHART)
        values = {
            "replicaCount": "bad",
            "image": {"repository": "nginx"},
            "service": {"port": 99999},
        }
        errors = validate_values(values, schema)
        for err in errors:
            self.assertIsNotNone(err.message)
            self.assertIsNotNone(err.schema_path)

    def test_missing_required_field(self) -> None:
        from helm_valuetrace.analysis.schema import load_schema, validate_values

        schema = load_schema(VALID_CHART)
        values = {"replicaCount": 1}
        errors = validate_values(values, schema)
        self.assertGreater(len(errors), 0)

    def test_nested_property_error(self) -> None:
        from helm_valuetrace.analysis.schema import load_schema, validate_values

        schema = load_schema(VALID_CHART)
        values = {"replicaCount": 1, "image": {"repository": 12345}}
        errors = validate_values(values, schema)
        self.assertGreater(len(errors), 0)
        self.assertIn("image.repository", [e.key for e in errors])

    def test_array_validation(self) -> None:
        from helm_valuetrace.analysis.schema import validate_values

        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {
                "hosts": {
                    "type": "array",
                    "items": {"type": "string"},
                }
            },
        }
        errors = validate_values({"hosts": ["valid", 123]}, schema)
        self.assertEqual(len(errors), 1)
        self.assertIn("hosts.1", errors[0].key)

    def test_additional_properties_error(self) -> None:
        from helm_valuetrace.analysis.schema import validate_values

        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "replicaCount": {"type": "integer"},
            },
        }
        errors = validate_values({"replicaCount": 1, "extraField": "bad"}, schema)
        self.assertGreater(len(errors), 0)

    def test_invalid_value_constraint(self) -> None:
        from helm_valuetrace.analysis.schema import load_schema, validate_values

        schema = load_schema(VALID_CHART)
        values = {"replicaCount": 0, "image": {"repository": "nginx"}}
        errors = validate_values(values, schema)
        self.assertGreater(len(errors), 0)
        self.assertIn("replicaCount", [e.key for e in errors])


@unittest.skipUnless(HAS_JSONSCHEMA, SKIP_MSG)
class TestCLISchemaIntegration(unittest.TestCase):
    def _run(self, chart_dir: Path, extra_args: list[str] | None = None) -> tuple[int, str, str]:
        buf_out = StringIO()
        buf_err = StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = buf_out
        sys.stderr = buf_err
        try:
            from helm_valuetrace.cli import main

            rc = main([str(chart_dir), "-o", "json", "--no-redact"] + (extra_args or []))
        finally:
            sys.stdout = old_out
            sys.stderr = old_err
        return rc, buf_out.getvalue(), buf_err.getvalue()

    def test_valid_chart_exits_zero(self) -> None:
        rc, _, _ = self._run(VALID_CHART, ["--strict-schema"])
        self.assertEqual(rc, 0)

    def test_invalid_chart_warnings_without_strict(self) -> None:
        """Without --strict-schema, schema errors produce warnings but exit 0."""
        rc, _stdout, stderr = self._run(INVALID_CHART)
        self.assertEqual(rc, 0)
        self.assertIn("SCHEMA WARNINGS", stderr)

    def test_invalid_chart_exits_2_with_strict(self) -> None:
        """With --strict-schema, schema errors cause exit 2."""
        rc, _, _ = self._run(INVALID_CHART, ["--strict-schema"])
        self.assertEqual(rc, 2)

    def test_schema_errors_in_json_output(self) -> None:
        """Schema errors appear in structured JSON output."""
        _, stdout, _ = self._run(INVALID_CHART)
        doc = json.loads(stdout)
        self.assertIn("schema_errors", doc)
        self.assertGreater(len(doc["schema_errors"]), 0)

    def test_valid_chart_no_schema_errors_in_output(self) -> None:
        _, stdout, _ = self._run(VALID_CHART)
        doc = json.loads(stdout)
        self.assertEqual(doc["schema_errors"], [])


if __name__ == "__main__":
    unittest.main()
