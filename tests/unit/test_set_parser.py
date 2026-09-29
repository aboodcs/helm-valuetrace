"""
Tests for the full --set-* parser.

Covers:
- Simple dotted paths
- Array bracket notation: servers[0].port
- Escaped dots: a\\.b → single key "a.b"
- Brace lists: names={a,b,c}
- --set-string (always string)
- --set-file (reads file contents)
- --set-json (JSON parsed)
- --set-literal (completely literal)
- Multiple comma-separated assignments
- Scalar coercion (booleans, integers, null, strings)
- Error cases
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helm_valuetrace.core import ValueTraceError
from helm_valuetrace.models import SourceType
from helm_valuetrace.parsers.set_parser import (
    _parse_key_path,
    parse_all_set_arguments,
    parse_helm_scalar,
    parse_set,
    parse_set_file,
    parse_set_json,
    parse_set_literal,
    parse_set_string,
)


class HelmScalarCoercionTests(unittest.TestCase):
    """Verify scalar coercion matches Helm's strvals rules."""

    def test_true_variants(self) -> None:
        self.assertIs(parse_helm_scalar("true"), True)
        self.assertIs(parse_helm_scalar("TRUE"), True)
        self.assertIs(parse_helm_scalar("True"), True)

    def test_false_variants(self) -> None:
        self.assertIs(parse_helm_scalar("false"), False)
        self.assertIs(parse_helm_scalar("FALSE"), False)
        self.assertIs(parse_helm_scalar("False"), False)

    def test_null(self) -> None:
        self.assertIsNone(parse_helm_scalar("null"))
        self.assertIsNone(parse_helm_scalar("NULL"))
        self.assertIsNone(parse_helm_scalar("Null"))

    def test_zero(self) -> None:
        self.assertEqual(parse_helm_scalar("0"), 0)
        self.assertIsInstance(parse_helm_scalar("0"), int)

    def test_positive_integer(self) -> None:
        self.assertEqual(parse_helm_scalar("42"), 42)
        self.assertIsInstance(parse_helm_scalar("42"), int)

    def test_negative_integer(self) -> None:
        self.assertEqual(parse_helm_scalar("-7"), -7)

    def test_leading_zero_stays_string(self) -> None:
        self.assertEqual(parse_helm_scalar("0123"), "0123")
        self.assertIsInstance(parse_helm_scalar("0123"), str)

    def test_float_stays_string(self) -> None:
        self.assertEqual(parse_helm_scalar("1.0"), "1.0")
        self.assertIsInstance(parse_helm_scalar("1.0"), str)

    def test_yes_stays_string(self) -> None:
        self.assertEqual(parse_helm_scalar("yes"), "yes")

    def test_empty_string(self) -> None:
        self.assertEqual(parse_helm_scalar(""), "")

    def test_int64_overflow_stays_string(self) -> None:
        big = str(2**63)
        result = parse_helm_scalar(big)
        self.assertIsInstance(result, str)

    def test_int64_max_is_parsed(self) -> None:
        max_val = str(2**63 - 1)
        self.assertEqual(parse_helm_scalar(max_val), 2**63 - 1)


class KeyPathParserTests(unittest.TestCase):
    """Verify _parse_key_path handles all Helm key syntax."""

    def test_simple_key(self) -> None:
        self.assertEqual(_parse_key_path("replicaCount"), ("replicaCount",))

    def test_dotted_path(self) -> None:
        self.assertEqual(_parse_key_path("image.repository"), ("image", "repository"))

    def test_deeply_nested(self) -> None:
        self.assertEqual(
            _parse_key_path("a.b.c.d"),
            ("a", "b", "c", "d"),
        )

    def test_array_index(self) -> None:
        path = _parse_key_path("servers[0].port")
        self.assertEqual(path, ("servers", 0, "port"))
        self.assertIsInstance(path[1], int)

    def test_array_index_distinguishable_from_string(self) -> None:
        int_path = _parse_key_path("servers[0].port")
        str_path = _parse_key_path("servers.0.port")
        self.assertNotEqual(int_path, str_path)
        self.assertEqual(int_path[1], 0)
        self.assertIsInstance(int_path[1], int)
        self.assertEqual(str_path[1], "0")
        self.assertIsInstance(str_path[1], str)

    def test_multiple_array_indexes(self) -> None:
        path = _parse_key_path("a[0][1]")
        self.assertEqual(path, ("a", 0, 1))

    def test_nested_array_with_field(self) -> None:
        path = _parse_key_path("servers[0].tls.enabled")
        self.assertEqual(path, ("servers", 0, "tls", "enabled"))

    def test_escaped_dot_becomes_single_segment(self) -> None:
        path = _parse_key_path("annotations.example\\.com/name")
        self.assertEqual(path, ("annotations", "example.com/name"))

    def test_fully_escaped_key(self) -> None:
        path = _parse_key_path("a\\.b")
        self.assertEqual(path, ("a.b",))

    def test_empty_key_raises(self) -> None:
        with self.assertRaises(ValueTraceError):
            _parse_key_path("")

    def test_unclosed_bracket_raises(self) -> None:
        with self.assertRaises(ValueTraceError):
            _parse_key_path("servers[0")

    def test_non_digit_index_raises(self) -> None:
        with self.assertRaises(ValueTraceError):
            _parse_key_path("servers[abc]")

    def test_empty_index_raises(self) -> None:
        with self.assertRaises(ValueTraceError):
            _parse_key_path("servers[]")


class ParseSetTests(unittest.TestCase):
    """Tests for parse_set() — basic --set with type coercion."""

    def test_simple_string_value(self) -> None:
        results = parse_set(["image.tag=v1.0"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].path, ("image", "tag"))
        self.assertEqual(results[0].value, "v1.0")
        self.assertEqual(results[0].source_type, SourceType.SET)

    def test_boolean_coercion(self) -> None:
        results = parse_set(["enabled=true"])
        self.assertIs(results[0].value, True)

    def test_integer_coercion(self) -> None:
        results = parse_set(["replicaCount=5"])
        self.assertEqual(results[0].value, 5)
        self.assertIsInstance(results[0].value, int)

    def test_null_coercion(self) -> None:
        results = parse_set(["key=null"])
        self.assertIsNone(results[0].value)

    def test_comma_separated_multiple_assignments(self) -> None:
        results = parse_set(["image.tag=v2,replicaCount=4,feature.enabled=true"])
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0].path, ("image", "tag"))
        self.assertEqual(results[1].value, 4)
        self.assertIs(results[2].value, True)

    def test_multiple_raw_arguments(self) -> None:
        results = parse_set(["a=1", "b=2"])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].source_label, "--set[1]")
        self.assertEqual(results[1].source_label, "--set[2]")

    def test_array_index_path(self) -> None:
        results = parse_set(["servers[0].port=8080"])
        self.assertEqual(results[0].path, ("servers", 0, "port"))
        self.assertEqual(results[0].value, 8080)

    def test_sparse_array_indexes(self) -> None:
        results = parse_set(["servers[0].port=80,servers[1].port=443"])
        self.assertEqual(results[0].path, ("servers", 0, "port"))
        self.assertEqual(results[1].path, ("servers", 1, "port"))

    def test_brace_list_value(self) -> None:
        results = parse_set(["names={a,b,c}"])
        self.assertEqual(results[0].value, ["a", "b", "c"])

    def test_escaped_dot_in_key(self) -> None:
        results = parse_set(["annotations.example\\.com/name=value"])
        self.assertEqual(results[0].path, ("annotations", "example.com/name"))
        self.assertEqual(results[0].value, "value")

    def test_missing_equals_raises(self) -> None:
        with self.assertRaises(ValueTraceError):
            parse_set(["no-equals-sign"])

    def test_empty_brace_list(self) -> None:
        results = parse_set(["names={}"])
        self.assertEqual(results[0].value, [""])

    def test_escaped_comma_in_value(self) -> None:
        results = parse_set([r"message=a\,b"])
        self.assertEqual(results[0].value, "a,b")

    def test_escaped_comma_in_brace_list(self) -> None:
        results = parse_set([r"names={a,b\,c,d}"])
        self.assertEqual(results[0].value, ["a", "b,c", "d"])

    def test_escaped_equals_in_value(self) -> None:
        results = parse_set([r"expr=a\=b"])
        self.assertEqual(results[0].value, "a=b")

    def test_multiple_assignments_with_escaped_commas(self) -> None:
        results = parse_set([r"first=a\,b,second=c\,d"])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].value, "a,b")
        self.assertEqual(results[1].value, "c,d")


class ParseSetStringTests(unittest.TestCase):
    """Tests for parse_set_string() — values always remain strings."""

    def test_true_stays_string(self) -> None:
        results = parse_set_string(["flag=true"])
        self.assertEqual(results[0].value, "true")
        self.assertIsInstance(results[0].value, str)

    def test_false_stays_string(self) -> None:
        results = parse_set_string(["flag=false"])
        self.assertEqual(results[0].value, "false")
        self.assertIsInstance(results[0].value, str)

    def test_null_stays_string(self) -> None:
        results = parse_set_string(["key=null"])
        self.assertEqual(results[0].value, "null")
        self.assertIsInstance(results[0].value, str)

    def test_integer_stays_string(self) -> None:
        results = parse_set_string(["count=42"])
        self.assertEqual(results[0].value, "42")
        self.assertIsInstance(results[0].value, str)

    def test_float_stays_string(self) -> None:
        results = parse_set_string(["ratio=1.5"])
        self.assertEqual(results[0].value, "1.5")
        self.assertIsInstance(results[0].value, str)

    def test_source_type_is_set_string(self) -> None:
        results = parse_set_string(["key=value"])
        self.assertEqual(results[0].source_type, SourceType.SET_STRING)

    def test_source_label_prefix(self) -> None:
        results = parse_set_string(["key=value"])
        self.assertEqual(results[0].source_label, "--set-string[1]")

    def test_escaped_comma_in_set_string(self) -> None:
        results = parse_set_string([r"message=a\,b"])
        self.assertEqual(results[0].value, "a,b")
        self.assertIsInstance(results[0].value, str)


class ParseSetFileTests(unittest.TestCase):
    """Tests for parse_set_file() — reads file contents."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reads_file_contents(self) -> None:
        f = self.tmpdir / "secret.txt"
        f.write_text("my-secret-value", encoding="utf-8")
        results = parse_set_file([f"password={f}"])
        self.assertEqual(results[0].value, "my-secret-value")
        self.assertEqual(results[0].path, ("password",))
        self.assertEqual(results[0].source_type, SourceType.SET_FILE)

    def test_empty_file(self) -> None:
        f = self.tmpdir / "empty.txt"
        f.write_text("", encoding="utf-8")
        results = parse_set_file([f"key={f}"])
        self.assertEqual(results[0].value, "")

    def test_multiline_file(self) -> None:
        f = self.tmpdir / "cert.pem"
        content = "-----BEGIN CERT-----\nABCD\n-----END CERT-----\n"
        f.write_text(content, encoding="utf-8")
        results = parse_set_file([f"cert={f}"])
        self.assertEqual(results[0].value, content)

    def test_missing_file_raises_clear_error(self) -> None:
        with self.assertRaises(ValueTraceError) as ctx:
            parse_set_file(["key=/nonexistent/path/to/file.txt"])
        self.assertIn("--set-file", str(ctx.exception))
        self.assertIn("key", str(ctx.exception))

    def test_missing_equals_raises(self) -> None:
        with self.assertRaises(ValueTraceError):
            parse_set_file(["no-equals"])

    def test_file_path_never_in_value_error(self) -> None:
        """File contents must never appear in error messages."""
        f = self.tmpdir / "secret.txt"
        f.write_text("REAL_SECRET_VALUE_DO_NOT_EXPOSE", encoding="utf-8")
        with self.assertRaises(ValueTraceError) as ctx:
            parse_set_file(["key=/completely/missing/file"])
        self.assertNotIn("REAL_SECRET_VALUE_DO_NOT_EXPOSE", str(ctx.exception))

    def test_multiple_files(self) -> None:
        f1 = self.tmpdir / "f1.txt"
        f2 = self.tmpdir / "f2.txt"
        f1.write_text("content-1", encoding="utf-8")
        f2.write_text("content-2", encoding="utf-8")
        results = parse_set_file([f"k1={f1},k2={f2}"])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].value, "content-1")
        self.assertEqual(results[1].value, "content-2")

    def test_unusual_binary_file_content(self) -> None:
        f = self.tmpdir / "binary.bin"
        f.write_bytes(bytes([0xFF, 0xFE, 0x00, 0x80]))
        results = parse_set_file([f"bin={f}"])
        self.assertEqual(len(results), 1)
        self.assertIn("\ufffd", results[0].value)


class ParseSetJsonTests(unittest.TestCase):
    """Tests for parse_set_json() — JSON parsed values."""

    def test_json_object(self) -> None:
        results = parse_set_json(['config={"host":"db","port":5432}'])
        self.assertEqual(results[0].value, {"host": "db", "port": 5432})
        self.assertEqual(results[0].source_type, SourceType.SET_JSON)

    def test_json_array(self) -> None:
        results = parse_set_json(['servers=["a","b","c"]'])
        self.assertEqual(results[0].value, ["a", "b", "c"])

    def test_json_string(self) -> None:
        results = parse_set_json(['key="hello"'])
        self.assertEqual(results[0].value, "hello")

    def test_json_number(self) -> None:
        results = parse_set_json(["count=42"])
        self.assertEqual(results[0].value, 42)

    def test_json_float(self) -> None:
        results = parse_set_json(["ratio=1.5"])
        self.assertAlmostEqual(results[0].value, 1.5)

    def test_json_boolean_true(self) -> None:
        results = parse_set_json(["enabled=true"])
        self.assertIs(results[0].value, True)

    def test_json_boolean_false(self) -> None:
        results = parse_set_json(["enabled=false"])
        self.assertIs(results[0].value, False)

    def test_json_null(self) -> None:
        results = parse_set_json(["key=null"])
        self.assertIsNone(results[0].value)

    def test_nested_json_object(self) -> None:
        results = parse_set_json(['db={"host":"localhost","creds":{"user":"admin"}}'])
        self.assertEqual(results[0].value["creds"]["user"], "admin")

    def test_invalid_json_raises_clear_error(self) -> None:
        with self.assertRaises(ValueTraceError) as ctx:
            parse_set_json(["key={not-valid-json}"])
        self.assertIn("--set-json", str(ctx.exception))
        self.assertIn("key", str(ctx.exception))

    def test_missing_equals_raises(self) -> None:
        with self.assertRaises(ValueTraceError):
            parse_set_json(["no-equals"])


class ParseSetLiteralTests(unittest.TestCase):
    """Tests for parse_set_literal() — completely literal string."""

    def test_true_preserved_literally(self) -> None:
        results = parse_set_literal(["flag=true"])
        self.assertEqual(results[0].value, "true")
        self.assertIsInstance(results[0].value, str)

    def test_null_preserved_literally(self) -> None:
        results = parse_set_literal(["key=null"])
        self.assertEqual(results[0].value, "null")

    def test_integer_preserved_literally(self) -> None:
        results = parse_set_literal(["count=42"])
        self.assertEqual(results[0].value, "42")
        self.assertIsInstance(results[0].value, str)

    def test_comma_not_split(self) -> None:
        """--set-literal does NOT split on commas."""
        results = parse_set_literal(["key=a,b,c"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].value, "a,b,c")

    def test_backslash_preserved_literally(self) -> None:
        results = parse_set_literal(["key=a\\b"])
        self.assertEqual(results[0].value, "a\\b")

    def test_source_type_is_set_literal(self) -> None:
        results = parse_set_literal(["key=value"])
        self.assertEqual(results[0].source_type, SourceType.SET_LITERAL)

    def test_missing_equals_raises(self) -> None:
        with self.assertRaises(ValueTraceError):
            parse_set_literal(["no-equals"])


class ParseAllSetArgumentsTests(unittest.TestCase):
    """Tests for parse_all_set_arguments() — unified entry point."""

    def test_empty_returns_empty(self) -> None:
        self.assertEqual(parse_all_set_arguments(), [])

    def test_ordering_set_before_set_string(self) -> None:
        results = parse_all_set_arguments(
            set_args=["a=1"],
            set_string_args=["b=2"],
        )
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].source_type, SourceType.SET)
        self.assertEqual(results[1].source_type, SourceType.SET_STRING)

    def test_mixed_types(self) -> None:
        results = parse_all_set_arguments(
            set_args=["count=5"],
            set_json_args=['config={"a":1}'],
            set_literal_args=["key=true,false"],
        )
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0].value, {"a": 1})
        self.assertEqual(results[1].value, 5)
        self.assertEqual(results[2].value, "true,false")


if __name__ == "__main__":
    unittest.main()
