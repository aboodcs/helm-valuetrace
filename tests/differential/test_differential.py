"""
Integration and differential tests against real Helm (Phase 10).

These tests compare ValueTrace output against actual Helm behavior.
All tests skip gracefully when 'helm' is not available.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"
BASIC_CHART = FIXTURE_DIR / "_explain_test_chart"


def _helm_available() -> bool:
    """Return True if helm binary is available."""
    try:
        result = subprocess.run(
            ["helm", "version", "--short"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


HELM_AVAILABLE = _helm_available()
SKIP_NO_HELM = "helm binary not available"


def _helm_get_values(chart_dir: Path, extra_args: list[str]) -> dict:
    """Run helm show values and parse."""
    cmd = ["helm", "show", "values", str(chart_dir)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    if result.returncode != 0:
        return {}
    import yaml

    return yaml.safe_load(result.stdout) or {}


def _helm_template_values(chart_dir: Path, extra_args: list[str]) -> dict:
    """Run real helm template and extract resolved .Values as a dict."""
    cmd = ["helm", "template", "differential-release", str(chart_dir)] + extra_args
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"helm template failed ({proc.returncode}): {proc.stderr}")
    import yaml

    manifest = yaml.safe_load(proc.stdout)
    return json.loads(manifest["data"]["values"])


def _vt_run_json(chart_dir: Path, extra_args: list[str] | None = None) -> dict:
    """Run ValueTrace CLI and return parsed JSON output."""
    buf = StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        from helm_valuetrace.cli import main

        main([str(chart_dir), "-o", "json", "--no-redact"] + (extra_args or []))
    finally:
        sys.stdout = old_stdout
    return json.loads(buf.getvalue())


def _ensure_basic_fixture() -> Path:
    chart_dir = FIXTURE_DIR / "_explain_test_chart"
    chart_dir.mkdir(parents=True, exist_ok=True)
    (chart_dir / "Chart.yaml").write_text("apiVersion: v2\nname: test-chart\nversion: 0.1.0\n")
    (chart_dir / "values.yaml").write_text(
        "image:\n  repository: nginx\n  tag: latest\nreplicaCount: 1\nservice:\n  port: 80\n"
    )
    templates = chart_dir / "templates"
    template_content = (
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        "  name: values-dump\n"
        "data:\n"
        "  values: |\n"
        "    {{ toJson .Values }}\n"
    )
    (templates / "values.yaml").write_text(template_content)
    return chart_dir


class TestSetFlagTypes(unittest.TestCase):
    """Test --set type coercion matches ValueTrace behavior."""

    def setUp(self) -> None:
        self.chart = _ensure_basic_fixture()

    def test_set_integer_coercion(self) -> None:
        doc = _vt_run_json(self.chart, ["--set", "replicaCount=3"])
        by_key = {r["key"]: r["value"] for r in doc["values"]}
        self.assertEqual(by_key["replicaCount"], 3)
        self.assertIsInstance(by_key["replicaCount"], int)

    def test_set_boolean_coercion(self) -> None:
        doc = _vt_run_json(self.chart, ["--set", "replicaCount=true"])
        by_key = {r["key"]: r["value"] for r in doc["values"]}
        self.assertIs(by_key["replicaCount"], True)

    def test_set_string_no_coercion(self) -> None:
        doc = _vt_run_json(self.chart, ["--set-string", "replicaCount=3"])
        by_key = {r["key"]: r["value"] for r in doc["values"]}
        self.assertEqual(by_key["replicaCount"], "3")
        self.assertIsInstance(by_key["replicaCount"], str)

    def test_set_null_removes_default(self) -> None:
        doc = _vt_run_json(self.chart, ["--set", "image.tag=null"])
        by_key = {r["key"]: r for r in doc["values"]}
        self.assertNotIn("image.tag", by_key)

    def test_set_json_object(self) -> None:
        doc = _vt_run_json(self.chart, ["--set-json", 'image={"repository":"alpine","tag":"3.18"}'])
        by_key = {r["key"]: r["value"] for r in doc["values"]}
        self.assertEqual(by_key["image.repository"], "alpine")
        self.assertEqual(by_key["image.tag"], "3.18")

    def test_multiple_set_flags_last_wins(self) -> None:
        doc = _vt_run_json(
            self.chart,
            [
                "--set",
                "image.repository=first",
                "--set",
                "image.repository=second",
            ],
        )
        by_key = {r["key"]: r["value"] for r in doc["values"]}
        self.assertEqual(by_key["image.repository"], "second")

    def test_set_escaped_dot_key(self) -> None:
        """Escaped dots create leaf keys, not nested paths."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            chart = Path(tmpdir)
            (chart / "Chart.yaml").write_text("apiVersion: v2\nname: x\nversion: 0.1.0\n")
            (chart / "values.yaml").write_text("{}\n")
            doc = _vt_run_json(chart, ["--set", r"a\.b=value"])
            by_key = {r["key"]: r["value"] for r in doc["values"]}
            self.assertIn("a.b", by_key)
            self.assertEqual(by_key["a.b"], "value")


@unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
class TestDifferentialHelm(unittest.TestCase):
    """Differential tests: compare ValueTrace against actual Helm."""

    def setUp(self) -> None:
        self.chart = _ensure_basic_fixture()

    def test_default_values_match_helm(self) -> None:
        """Default values from values.yaml match what Helm reads."""
        helm_vals = _helm_template_values(self.chart, [])
        doc = _vt_run_json(self.chart)
        vt_keys = {r["key"]: r["value"] for r in doc["values"]}

        self.assertEqual(
            vt_keys.get("image.repository"), helm_vals.get("image", {}).get("repository")
        )
        self.assertEqual(vt_keys.get("replicaCount"), helm_vals.get("replicaCount"))
        self.assertEqual(vt_keys.get("service.port"), helm_vals.get("service", {}).get("port"))

    def test_differential_scalars(self) -> None:
        """Differential: scalars (int, bool, string) match Helm strvals."""
        args = ["--set", "replicaCount=3", "--set", "enabled=true", "--set", "name=hello"]
        helm_vals = _helm_template_values(self.chart, args)
        doc = _vt_run_json(self.chart, args)
        vt_keys = {r["key"]: r["value"] for r in doc["values"]}

        self.assertEqual(vt_keys["replicaCount"], helm_vals["replicaCount"])
        self.assertEqual(vt_keys["replicaCount"], 3)
        self.assertIsInstance(vt_keys["replicaCount"], int)

        self.assertEqual(vt_keys["enabled"], helm_vals["enabled"])
        self.assertIs(vt_keys["enabled"], True)

        self.assertEqual(vt_keys["name"], helm_vals["name"])
        self.assertEqual(vt_keys["name"], "hello")

    def test_differential_brace_lists(self) -> None:
        """Differential: brace lists names={a,b,c} produce array matching Helm."""
        args = ["--set", "names={a,b,c}"]
        helm_vals = _helm_template_values(self.chart, args)
        doc = _vt_run_json(self.chart, args)
        vt_keys = {r["key"]: r["value"] for r in doc["values"]}

        self.assertEqual(vt_keys["names"], helm_vals["names"])
        self.assertEqual(vt_keys["names"], ["a", "b", "c"])

    def test_differential_array_indexes(self) -> None:
        """Differential: array indexing servers[0].port, etc. matches Helm."""
        args = [
            "--set",
            "servers[0].port=8080",
            "--set",
            "servers[0].name=api",
            "--set",
            "servers[1].name=worker",
        ]
        helm_vals = _helm_template_values(self.chart, args)
        doc = _vt_run_json(self.chart, args)
        vt_keys = {r["key"]: r["value"] for r in doc["values"]}

        self.assertEqual(vt_keys["servers"], helm_vals["servers"])
        self.assertEqual(
            vt_keys["servers"],
            [{"name": "api", "port": 8080}, {"name": "worker"}],
        )

    def test_differential_set_string(self) -> None:
        """Differential: --set-string preserves string type matching Helm."""
        args = ["--set-string", "servers[0].port=8080"]
        helm_vals = _helm_template_values(self.chart, args)
        doc = _vt_run_json(self.chart, args)
        vt_keys = {r["key"]: r["value"] for r in doc["values"]}

        self.assertEqual(vt_keys["servers"], helm_vals["servers"])
        self.assertEqual(vt_keys["servers"], [{"port": "8080"}])
        self.assertIsInstance(vt_keys["servers"][0]["port"], str)

    def test_differential_set_file(self) -> None:
        """Differential: --set-file reads file contents matching Helm."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "cert.txt"
            file_path.write_text("differential-file-content")
            args = ["--set-file", f"servers[0].port={file_path}"]
            helm_vals = _helm_template_values(self.chart, args)
            doc = _vt_run_json(self.chart, args)
            vt_keys = {r["key"]: r["value"] for r in doc["values"]}

            self.assertEqual(vt_keys["servers"], helm_vals["servers"])
            self.assertEqual(vt_keys["servers"], [{"port": "differential-file-content"}])

    def test_differential_set_literal(self) -> None:
        """Differential: --set-literal preserves value literally matching Helm."""
        args = ["--set-literal", "servers[0].port=8080"]
        helm_vals = _helm_template_values(self.chart, args)
        doc = _vt_run_json(self.chart, args)
        vt_keys = {r["key"]: r["value"] for r in doc["values"]}

        self.assertEqual(vt_keys["servers"], helm_vals["servers"])
        self.assertEqual(vt_keys["servers"], [{"port": "8080"}])

    def test_differential_set_json(self) -> None:
        """Differential: --set-json parses JSON values matching Helm."""
        args = ["--set-json", 'servers=[{"name":"api","port":8080}]']
        helm_vals = _helm_template_values(self.chart, args)
        doc = _vt_run_json(self.chart, args)
        vt_keys = {r["key"]: r["value"] for r in doc["values"]}

        self.assertEqual(vt_keys["servers"], helm_vals["servers"])

    def test_differential_escaped_dots(self) -> None:
        """Differential: escaped dots in keys match Helm."""
        args = ["--set", r"esc\.dot=ok", "--set", r"annotations.example\.com/name=val"]
        helm_vals = _helm_template_values(self.chart, args)
        doc = _vt_run_json(self.chart, args)
        vt_keys = {r["key"]: r["value"] for r in doc["values"]}

        self.assertEqual(vt_keys["esc.dot"], helm_vals["esc.dot"])
        self.assertEqual(vt_keys["esc.dot"], "ok")
        self.assertEqual(
            vt_keys["annotations.example.com/name"],
            helm_vals["annotations"]["example.com/name"],
        )
        self.assertEqual(vt_keys["annotations.example.com/name"], "val")

    def test_differential_escaped_commas(self) -> None:
        """Differential: escaped commas are literal in value and match Helm."""
        args = ["--set", r"message=a\,b", "--set-string", r"str_message=a\,b"]
        helm_vals = _helm_template_values(self.chart, args)
        doc = _vt_run_json(self.chart, args)
        vt_keys = {r["key"]: r["value"] for r in doc["values"]}

        self.assertEqual(vt_keys["message"], helm_vals["message"])
        self.assertEqual(vt_keys["message"], "a,b")
        self.assertEqual(vt_keys["str_message"], helm_vals["str_message"])
        self.assertEqual(vt_keys["str_message"], "a,b")

    def test_differential_values_file_override_precedence(self) -> None:
        """Differential: -f file overrides default, and --set overrides -f file matching Helm."""
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_yaml = Path(tmpdir) / "custom.yaml"
            custom_yaml.write_text("replicaCount: 50\nimage:\n  repository: custom-img\n")
            args = ["-f", str(custom_yaml), "--set", "replicaCount=99"]
            helm_vals = _helm_template_values(self.chart, args)
            doc = _vt_run_json(self.chart, args)
            vt_keys = {r["key"]: r["value"] for r in doc["values"]}

            self.assertEqual(vt_keys["replicaCount"], helm_vals["replicaCount"])
            self.assertEqual(vt_keys["replicaCount"], 99)
            self.assertEqual(vt_keys["image.repository"], helm_vals["image"]["repository"])
            self.assertEqual(vt_keys["image.repository"], "custom-img")


class TestMultipleValuesFiles(unittest.TestCase):
    """Tests for multiple -f files with correct precedence."""

    def setUp(self) -> None:
        self.chart = _ensure_basic_fixture()

    def test_two_files_last_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = Path(tmpdir) / "base.yaml"
            f2 = Path(tmpdir) / "override.yaml"
            f1.write_text("image:\n  repository: base\n  tag: base-tag\n")
            f2.write_text("image:\n  repository: override\n")
            doc = _vt_run_json(self.chart, ["-f", str(f1), "-f", str(f2)])
            by_key = {r["key"]: r["value"] for r in doc["values"]}
            self.assertEqual(by_key["image.repository"], "override")
            self.assertEqual(by_key["image.tag"], "base-tag")

    def test_provenance_history_records_all_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = Path(tmpdir) / "base.yaml"
            f2 = Path(tmpdir) / "override.yaml"
            f1.write_text("image:\n  repository: base\n")
            f2.write_text("image:\n  repository: override\n")
            doc = _vt_run_json(self.chart, ["-f", str(f1), "-f", str(f2)])
            for row in doc["values"]:
                if row["key"] == "image.repository":
                    self.assertGreaterEqual(len(row["assignments"]), 2)
                    break


if __name__ == "__main__":
    unittest.main()
