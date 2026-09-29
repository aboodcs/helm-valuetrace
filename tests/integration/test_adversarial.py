"""
Adversarial differential compatibility test suite (Phase 2).

Compares ValueTrace against real Helm v4.3.0 across complex edge cases:
1. Array edge cases (sparse indexes, multiple indexes, nested lists)
2. Precedence matrix (values.yaml < -f < --set-json < --set < --set-string < ...)
3. Scalar edge cases (null, booleans, numeric variants, octal/hex strings, quotes)
4. Escaping edge cases (commas, equals, backslashes, dots, brackets)
5. Empty and unusual assignments (empty strings, null clearing default)
6. Multiple assignments in single flag
7. Array/list replacement semantics
8. Explain & provenance regressions
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from typing import ClassVar

import yaml

from helm_valuetrace.cli import main


def _helm_available() -> bool:
    try:
        res = subprocess.run(
            ["helm", "version", "--short"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return res.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


HELM_AVAILABLE = _helm_available()
SKIP_NO_HELM = "helm binary not available"


def _helm_template_values(chart_dir: Path, extra_args: list[str]) -> dict:
    cmd = ["helm", "template", "adversarial-release", str(chart_dir)] + extra_args
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"helm template failed ({proc.returncode}): {proc.stderr}")
    manifest = yaml.safe_load(proc.stdout)
    return json.loads(manifest["data"]["values"])


def _vt_run_json(chart_dir: Path, extra_args: list[str] | None = None) -> dict:
    buf = StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        main([str(chart_dir), "-o", "json", "--no-redact"] + (extra_args or []))
    finally:
        sys.stdout = old_stdout
    return json.loads(buf.getvalue())


def _create_adversarial_chart(parent: Path, values_yaml: str) -> Path:
    chart_dir = parent / "adv_chart"
    chart_dir.mkdir(parents=True, exist_ok=True)
    (chart_dir / "Chart.yaml").write_text("apiVersion: v2\nname: adv-chart\nversion: 0.1.0\n")
    (chart_dir / "values.yaml").write_text(values_yaml)
    templates = chart_dir / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    template_content = (
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        "  name: values-dump\n"
        "data:\n"
        "  values: |\n"
        "    {{ toJson .Values }}\n"
    )
    (templates / "dump.yaml").write_text(template_content)
    return chart_dir


@unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
class TestAdversarialArrayEdgeCases(unittest.TestCase):
    """1. Array edge cases compared against real Helm."""

    def test_existing_list_element_override(self) -> None:
        values_yaml = "servers:\n  - name: api\n    port: 80\n  - name: worker\n    port: 9000\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            chart = _create_adversarial_chart(Path(tmpdir), values_yaml)
            args = ["--set", "servers[0].port=8080"]
            h_vals = _helm_template_values(chart, args)
            vt_doc = _vt_run_json(chart, args)
            vt_vals = {r["key"]: r["value"] for r in vt_doc["values"]}

            self.assertEqual(vt_vals["servers"], h_vals["servers"])
            self.assertEqual(vt_vals["servers"], [{"port": 8080}])

    def test_sparse_index_assignment(self) -> None:
        values_yaml = "servers:\n  - name: api\n    port: 80\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            chart = _create_adversarial_chart(Path(tmpdir), values_yaml)
            args = ["--set", "servers[2].name=third"]
            h_vals = _helm_template_values(chart, args)
            vt_doc = _vt_run_json(chart, args)
            vt_vals = {r["key"]: r["value"] for r in vt_doc["values"]}

            self.assertEqual(vt_vals["servers"], h_vals["servers"])
            self.assertEqual(vt_vals["servers"], [None, None, {"name": "third"}])

    def test_multiple_indexed_assignments_in_single_flag(self) -> None:
        values_yaml = "servers: []\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            chart = _create_adversarial_chart(Path(tmpdir), values_yaml)
            args = ["--set", "servers[0].name=api,servers[0].port=8080"]
            h_vals = _helm_template_values(chart, args)
            vt_doc = _vt_run_json(chart, args)
            vt_vals = {r["key"]: r["value"] for r in vt_doc["values"]}

            self.assertEqual(vt_vals["servers"], h_vals["servers"])
            self.assertEqual(vt_vals["servers"], [{"name": "api", "port": 8080}])

    def test_list_of_scalars(self) -> None:
        values_yaml = "{}\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            chart = _create_adversarial_chart(Path(tmpdir), values_yaml)
            args = ["--set", "ports[0]=80,ports[1]=443"]
            h_vals = _helm_template_values(chart, args)
            vt_doc = _vt_run_json(chart, args)
            vt_vals = {r["key"]: r["value"] for r in vt_doc["values"]}

            self.assertEqual(vt_vals["ports"], h_vals["ports"])
            self.assertEqual(vt_vals["ports"], [80, 443])

    def test_nested_list_and_objects(self) -> None:
        values_yaml = "{}\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            chart = _create_adversarial_chart(Path(tmpdir), values_yaml)
            args = ["--set", "matrix[0].items[0].name=a"]
            h_vals = _helm_template_values(chart, args)
            vt_doc = _vt_run_json(chart, args)
            vt_vals = {r["key"]: r["value"] for r in vt_doc["values"]}

            self.assertEqual(vt_vals["matrix"], h_vals["matrix"])
            self.assertEqual(vt_vals["matrix"], [{"items": [{"name": "a"}]}])


@unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
class TestAdversarialPrecedenceMatrix(unittest.TestCase):
    """2. Precedence matrix compared against real Helm."""

    def test_full_precedence_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            chart = _create_adversarial_chart(tmp, "target: default\n")
            f1 = tmp / "f1.yaml"
            f1.write_text("target: f1\n")
            f2 = tmp / "f2.yaml"
            f2.write_text("target: f2\n")
            file_val = tmp / "val.txt"
            file_val.write_text("file_content")

            args = [
                "-f",
                str(f1),
                "-f",
                str(f2),
                "--set-json",
                'target="json"',
                "--set",
                "target=set",
                "--set-string",
                "target=string",
                "--set-file",
                f"target={file_val}",
                "--set-literal",
                "target=literal",
            ]
            h_vals = _helm_template_values(chart, args)
            vt_doc = _vt_run_json(chart, args)
            vt_vals = {r["key"]: r["value"] for r in vt_doc["values"]}

            self.assertEqual(vt_vals["target"], h_vals["target"])
            self.assertEqual(vt_vals["target"], "literal")

    def test_precedence_pairwise_set_json_vs_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            chart = _create_adversarial_chart(Path(tmpdir), "target: default\n")
            args = ["--set", "target=set", "--set-json", 'target="json"']
            h_vals = _helm_template_values(chart, args)
            vt_doc = _vt_run_json(chart, args)
            vt_vals = {r["key"]: r["value"] for r in vt_doc["values"]}

            self.assertEqual(vt_vals["target"], h_vals["target"])
            self.assertEqual(vt_vals["target"], "set")

    def test_precedence_pairwise_set_vs_set_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            chart = _create_adversarial_chart(Path(tmpdir), "target: default\n")
            args = ["--set-string", "target=string", "--set", "target=set"]
            h_vals = _helm_template_values(chart, args)
            vt_doc = _vt_run_json(chart, args)
            vt_vals = {r["key"]: r["value"] for r in vt_doc["values"]}

            self.assertEqual(vt_vals["target"], h_vals["target"])
            self.assertEqual(vt_vals["target"], "string")

    def test_precedence_pairwise_set_string_vs_set_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            chart = _create_adversarial_chart(tmp, "target: default\n")
            f = tmp / "val.txt"
            f.write_text("file_val")
            args = ["--set-file", f"target={f}", "--set-string", "target=string"]
            h_vals = _helm_template_values(chart, args)
            vt_doc = _vt_run_json(chart, args)
            vt_vals = {r["key"]: r["value"] for r in vt_doc["values"]}

            self.assertEqual(vt_vals["target"], h_vals["target"])
            self.assertEqual(vt_vals["target"], "file_val")

    def test_precedence_pairwise_set_file_vs_set_literal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            chart = _create_adversarial_chart(tmp, "target: default\n")
            f = tmp / "val.txt"
            f.write_text("file_val")
            args = ["--set-literal", "target=literal", "--set-file", f"target={f}"]
            h_vals = _helm_template_values(chart, args)
            vt_doc = _vt_run_json(chart, args)
            vt_vals = {r["key"]: r["value"] for r in vt_doc["values"]}

            self.assertEqual(vt_vals["target"], h_vals["target"])
            self.assertEqual(vt_vals["target"], "literal")


@unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
class TestAdversarialScalarEdgeCases(unittest.TestCase):
    """3. Scalar edge cases compared against real Helm."""

    SCALARS: ClassVar[list[tuple[str, object, type]]] = [
        ("null", None, type(None)),
        ("true", True, bool),
        ("false", False, bool),
        ("0", 0, int),
        ("1", 1, int),
        ("-1", -1, int),
        ("1.5", "1.5", str),
        ("001", "001", str),
        ("0123", "0123", str),
        ("0x10", "0x10", str),
        ("1e3", "1e3", str),
        ('"true"', '"true"', str),
        ('"123"', '"123"', str),
        ("", "", str),
    ]

    def test_all_scalar_coercions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            chart = _create_adversarial_chart(Path(tmpdir), "{}\n")
            for raw, _expected_val, expected_type in self.SCALARS:
                args = ["--set", f"v={raw}"]
                h_vals = _helm_template_values(chart, args)
                vt_doc = _vt_run_json(chart, args)
                vt_vals = {r["key"]: r["value"] for r in vt_doc["values"]}

                h_val = h_vals.get("v")
                v_val = vt_vals.get("v")
                self.assertEqual(v_val, h_val, f"Value mismatch for scalar {raw!r}")
                self.assertIsInstance(v_val, expected_type, f"Type mismatch for scalar {raw!r}")


@unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
class TestAdversarialEscapingEdgeCases(unittest.TestCase):
    """4. Escaping edge cases compared against real Helm."""

    ESCAPES: ClassVar[list[tuple[str, str, object]]] = [
        (r"key=a\,b", "key", "a,b"),
        (r"key=a\=b", "key", "a=b"),
        (r"key=a\\b", "key", "a\\b"),
        (r"key=a\;b", "key", "a;b"),
        (r"foo\.bar=val", "foo.bar", "val"),
        (r"foo\[bar=val", "foo[bar", "val"),
        (r"foo\]bar=val", "foo]bar", "val"),
        (r"names={a,b\,c,d}", "names", ["a", "b,c", "d"]),
    ]

    def test_all_escaping_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            chart = _create_adversarial_chart(Path(tmpdir), "{}\n")
            for raw, key, _expected in self.ESCAPES:
                args = ["--set", raw]
                h_vals = _helm_template_values(chart, args)
                vt_doc = _vt_run_json(chart, args)
                vt_vals = {r["key"]: r["value"] for r in vt_doc["values"]}

                self.assertEqual(vt_vals.get(key), h_vals.get(key), f"Mismatch for escape {raw}")


@unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
class TestAdversarialEmptyAndUnusualAssignments(unittest.TestCase):
    """5. Empty and unusual assignments compared against real Helm."""

    def test_empty_value_no_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            chart = _create_adversarial_chart(Path(tmpdir), "{}\n")
            for flag in ["--set", "--set-string", "--set-literal"]:
                args = [flag, "key="]
                h_vals = _helm_template_values(chart, args)
                vt_doc = _vt_run_json(chart, args)
                vt_vals = {r["key"]: r["value"] for r in vt_doc["values"]}
                self.assertEqual(vt_vals.get("key"), h_vals.get("key"))
                self.assertEqual(vt_vals.get("key"), "")

    def test_empty_value_with_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            chart = _create_adversarial_chart(Path(tmpdir), "key: original\n")
            for flag in ["--set", "--set-string", "--set-literal"]:
                args = [flag, "key="]
                h_vals = _helm_template_values(chart, args)
                vt_doc = _vt_run_json(chart, args)
                vt_vals = {r["key"]: r["value"] for r in vt_doc["values"]}
                self.assertEqual(vt_vals.get("key"), h_vals.get("key"))
                self.assertEqual(vt_vals.get("key"), "")

    def test_null_value_without_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            chart = _create_adversarial_chart(Path(tmpdir), "{}\n")
            for flag in ["--set", "--set-json"]:
                args = [flag, "key=null"]
                h_vals = _helm_template_values(chart, args)
                vt_doc = _vt_run_json(chart, args)
                vt_vals = {r["key"]: r["value"] for r in vt_doc["values"]}
                self.assertIn("key", vt_vals)
                self.assertIn("key", h_vals)
                self.assertIsNone(vt_vals["key"])

    def test_null_value_clears_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            chart = _create_adversarial_chart(Path(tmpdir), "key: original\n")
            for flag in ["--set", "--set-json"]:
                args = [flag, "key=null"]
                h_vals = _helm_template_values(chart, args)
                vt_doc = _vt_run_json(chart, args)
                vt_vals = {r["key"]: r["value"] for r in vt_doc["values"]}
                self.assertNotIn("key", vt_vals)
                self.assertNotIn("key", h_vals)


@unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
class TestAdversarialMultipleAssignments(unittest.TestCase):
    """6. Multiple assignments in one argument compared against real Helm."""

    def test_multiple_assignments_comma_separated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            chart = _create_adversarial_chart(Path(tmpdir), "{}\n")
            args_single = ["--set", "a=1,b=2,c=3"]
            args_separate = ["--set", "a=1", "--set", "b=2", "--set", "c=3"]

            h_single = _helm_template_values(chart, args_single)
            h_separate = _helm_template_values(chart, args_separate)
            vt_single = {r["key"]: r["value"] for r in _vt_run_json(chart, args_single)["values"]}
            vt_separate = {
                r["key"]: r["value"] for r in _vt_run_json(chart, args_separate)["values"]
            }

            self.assertEqual(h_single, h_separate)
            self.assertEqual(vt_single, vt_separate)
            self.assertEqual(vt_single, h_single)

    def test_multiple_assignments_with_escaped_comma(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            chart = _create_adversarial_chart(Path(tmpdir), "{}\n")
            args = ["--set", r"a=hello\,world,b=2"]
            h_vals = _helm_template_values(chart, args)
            vt_vals = {r["key"]: r["value"] for r in _vt_run_json(chart, args)["values"]}

            self.assertEqual(vt_vals, h_vals)
            self.assertEqual(vt_vals["a"], "hello,world")
            self.assertEqual(vt_vals["b"], 2)


@unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
class TestAdversarialArrayReplacement(unittest.TestCase):
    """7. Array/list replacement semantics compared against real Helm."""

    def test_indexed_set_replaces_entire_array(self) -> None:
        values_yaml = "servers:\n  - name: api\n    port: 80\n  - name: worker\n    port: 9000\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            chart = _create_adversarial_chart(Path(tmpdir), values_yaml)
            args = ["--set", "servers[0].port=8080"]
            h_vals = _helm_template_values(chart, args)
            vt_vals = {r["key"]: r["value"] for r in _vt_run_json(chart, args)["values"]}

            self.assertEqual(vt_vals["servers"], h_vals["servers"])
            self.assertEqual(vt_vals["servers"], [{"port": 8080}])

    def test_set_json_replaces_entire_array(self) -> None:
        values_yaml = "servers:\n  - name: api\n    port: 80\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            chart = _create_adversarial_chart(Path(tmpdir), values_yaml)
            args = ["--set-json", 'servers=[{"name":"worker","port":9000}]']
            h_vals = _helm_template_values(chart, args)
            vt_vals = {r["key"]: r["value"] for r in _vt_run_json(chart, args)["values"]}

            self.assertEqual(vt_vals["servers"], h_vals["servers"])
            self.assertEqual(vt_vals["servers"], [{"name": "worker", "port": 9000}])


class TestAdversarialExplainAndProvenance(unittest.TestCase):
    """8. Explain & provenance regressions with new parser features."""

    def test_explain_indexed_key(self) -> None:
        values_yaml = "servers:\n  - name: api\n    port: 80\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            chart = _create_adversarial_chart(Path(tmpdir), values_yaml)
            buf = StringIO()
            old_stdout = sys.stdout
            sys.stdout = buf
            try:
                rc = main(
                    [
                        str(chart),
                        "explain",
                        "servers[0].port",
                        "--no-redact",
                        "--set",
                        "servers[0].port=8080",
                    ]
                )
            finally:
                sys.stdout = old_stdout

            output = buf.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn("servers[0].port", output)
            self.assertIn("8080", output)
            self.assertIn("--set[1]", output)

    def test_only_overridden_with_array_update(self) -> None:
        values_yaml = "servers:\n  - name: api\n    port: 80\nreplicaCount: 1\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            chart = _create_adversarial_chart(Path(tmpdir), values_yaml)
            doc = _vt_run_json(chart, ["--only-overridden", "--set", "servers[0].port=8080"])
            keys = [r["key"] for r in doc["values"]]
            self.assertIn("servers", keys)
            self.assertNotIn("replicaCount", keys)

    def test_json_and_yaml_serialization(self) -> None:
        values_yaml = "replicaCount: 1\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            chart = _create_adversarial_chart(Path(tmpdir), values_yaml)
            doc_json = _vt_run_json(chart, ["--set", "replicaCount=5"])
            self.assertEqual(doc_json["values"][0]["value"], 5)

            buf = StringIO()
            old_stdout = sys.stdout
            sys.stdout = buf
            try:
                rc = main([str(chart), "-o", "yaml", "--no-redact", "--set", "replicaCount=5"])
            finally:
                sys.stdout = old_stdout

            self.assertEqual(rc, 0)
            doc_yaml = yaml.safe_load(buf.getvalue())
            self.assertEqual(doc_yaml["values"][0]["value"], 5)


if __name__ == "__main__":
    unittest.main()
