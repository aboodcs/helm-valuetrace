"""Tests for the models module — SourceType, Assignment, PathKey, path_text."""

from __future__ import annotations

import json
import unittest

from helm_valuetrace.models import (
    Assignment,
    DeniedSource,
    PathKey,
    SchemaError,
    SourceType,
    TraceResult,
    path_text,
    path_text_simple,
)


class SourceTypeTests(unittest.TestCase):
    def test_source_type_serializes_to_plain_string(self) -> None:
        """SourceType must serialize to its value string, not Python enum repr."""
        self.assertEqual(SourceType.SET.value, "set")
        self.assertEqual(SourceType.VALUES_FILE.value, "values-file")
        self.assertEqual(SourceType.SET_JSON.value, "set-json")
        self.assertEqual(SourceType.DEFAULT.value, "default")
        self.assertEqual(SourceType.GLOBAL.value, "global")

    def test_source_type_is_json_serializable(self) -> None:
        """SourceType value must be directly JSON-serializable."""
        serialized = json.dumps({"source_type": SourceType.SET.value})
        self.assertEqual(serialized, '{"source_type": "set"}')

    def test_all_source_types_have_stable_values(self) -> None:
        """Verify every source type has the expected stable string value."""
        expected = {
            "VALUES_FILE": "values-file",
            "SET": "set",
            "SET_STRING": "set-string",
            "SET_FILE": "set-file",
            "SET_JSON": "set-json",
            "SET_LITERAL": "set-literal",
            "DEFAULT": "default",
            "DEPENDENCY": "dependency",
            "GLOBAL": "global",
        }
        for name, value in expected.items():
            self.assertEqual(SourceType[name].value, value, f"SourceType.{name}")


class PathTextTests(unittest.TestCase):
    def test_simple_mapping_path(self) -> None:
        self.assertEqual(path_text(("image", "repository")), "image.repository")

    def test_single_segment(self) -> None:
        self.assertEqual(path_text(("replicaCount",)), "replicaCount")

    def test_empty_path(self) -> None:
        self.assertEqual(path_text(()), "")

    def test_list_index_uses_bracket_notation(self) -> None:
        """servers[0].port must differ from servers.0.port"""
        path: PathKey = ("servers", 0, "port")
        text = path_text(path)
        self.assertEqual(text, "servers[0].port")

    def test_list_index_is_distinguishable_from_string_zero(self) -> None:
        """int index 0 produces [0], string "0" produces .0"""
        int_path: PathKey = ("servers", 0, "port")
        str_path: PathKey = ("servers", "0", "port")
        self.assertNotEqual(path_text(int_path), path_text(str_path))
        self.assertEqual(path_text(int_path), "servers[0].port")
        self.assertEqual(path_text(str_path), "servers.0.port")

    def test_nested_list_indexes(self) -> None:
        path: PathKey = ("a", 0, "b", 1, "c")
        self.assertEqual(path_text(path), "a[0].b[1].c")

    def test_escaped_dot_key(self) -> None:
        """A key that literally contains a dot should appear as-is."""
        path: PathKey = ("annotations.example.com/name",)
        self.assertEqual(path_text(path), "annotations.example.com/name")

    def test_path_text_simple_always_uses_dots(self) -> None:
        """path_text_simple is the legacy dot-only representation."""
        path: PathKey = ("servers", 0, "port")
        self.assertEqual(path_text_simple(path), "servers.0.port")


class AssignmentTests(unittest.TestCase):
    def test_location_with_line(self) -> None:
        a = Assignment(
            source="values.yaml",
            source_type=SourceType.VALUES_FILE,
            line=12,
            column=3,
            value="nginx",
        )
        self.assertEqual(a.location, "values.yaml:12")

    def test_location_without_line(self) -> None:
        a = Assignment(
            source="--set[1]",
            source_type=SourceType.SET,
            line=None,
            column=None,
            value="custom",
        )
        self.assertEqual(a.location, "--set[1]")

    def test_to_dict_serializes_source_type_as_string(self) -> None:
        a = Assignment(
            source="values.yaml",
            source_type=SourceType.VALUES_FILE,
            line=5,
            column=1,
            value=42,
        )
        d = a.to_dict()
        self.assertEqual(d["source_type"], "values-file")
        self.assertEqual(d["line"], 5)
        self.assertEqual(d["column"], 1)
        self.assertEqual(d["value"], 42)

    def test_assignment_is_frozen(self) -> None:
        a = Assignment(
            source="test",
            source_type=SourceType.DEFAULT,
            line=1,
            column=1,
            value=1,
        )
        with self.assertRaises((AttributeError, TypeError)):
            a.source = "other"  # type: ignore[misc]


class TraceResultDefaultsTests(unittest.TestCase):
    def test_optional_fields_default_to_empty(self) -> None:
        result = TraceResult(values={}, history={})
        self.assertEqual(result.unknown, [])
        self.assertEqual(result.missing, [])
        self.assertEqual(result.schema_errors, [])
        self.assertEqual(result.used_by_templates, {})

    def test_schema_errors_field_exists(self) -> None:
        err = SchemaError(
            key="replicaCount",
            message="expected integer, got string",
            schema_path="$.replicaCount",
        )
        result = TraceResult(values={}, history={}, schema_errors=[err])
        self.assertEqual(len(result.schema_errors), 1)
        self.assertEqual(result.schema_errors[0].key, "replicaCount")


class DeniedSourceTests(unittest.TestCase):
    def test_denied_source_is_frozen(self) -> None:
        ds = DeniedSource(source="debug.yaml", pattern="*debug*")
        self.assertEqual(ds.source, "debug.yaml")
        self.assertEqual(ds.pattern, "*debug*")


if __name__ == "__main__":
    unittest.main()
