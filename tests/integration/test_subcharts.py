"""
Differential and integration tests for subcharts against real Helm v4.3.0 (Phase 4).

Tests compare ValueTrace behavior directly against actual Helm semantics.
Skips gracefully if 'helm' is not available.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path

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


def _helm_template_values(chart_dir: Path, extra_args: list[str] | None = None) -> dict:
    """Extract .Values from real Helm template via ConfigMap dump."""
    cmd = ["helm", "template", "subchart-release", str(chart_dir)] + (extra_args or [])
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"helm template failed ({proc.returncode}): {proc.stderr}")
    docs = list(yaml.safe_load_all(proc.stdout))
    for doc in docs:
        if isinstance(doc, dict) and doc.get("metadata", {}).get("name") == "parent-values-dump":
            return json.loads(doc["data"]["values"])
    for doc in docs:
        if isinstance(doc, dict) and "data" in doc and "values" in doc["data"]:
            return json.loads(doc["data"]["values"])
    raise RuntimeError(f"No values dump ConfigMap found in Helm output: {proc.stdout}")


def _vt_run(argv: list[str]) -> tuple[int, str, str]:
    """Run ValueTrace CLI and capture rc, stdout, stderr."""
    buf_out = StringIO()
    buf_err = StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf_out, buf_err
    try:
        rc = main(argv)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, buf_out.getvalue(), buf_err.getvalue()


def _vt_json(chart_dir: Path, extra_args: list[str] | None = None) -> dict:
    rc, stdout, stderr = _vt_run([str(chart_dir), "-o", "json", "--no-redact"] + (extra_args or []))
    if rc != 0:
        raise RuntimeError(f"ValueTrace failed ({rc}): {stderr}")
    return json.loads(stdout)


def _vt_values_dict(vt_json_doc: dict) -> dict:
    """Reconstruct a nested values dictionary from ValueTrace JSON values rows."""
    from helm_valuetrace.core import _set_nested
    from helm_valuetrace.parsers.set_parser import _parse_key_path

    res: dict = {}
    for row in vt_json_doc.get("values", []):
        key = row["key"]
        val = row["value"]
        path = _parse_key_path(key)
        _set_nested(res, path, val)
    return res


def _make_base_chart(
    base_dir: Path,
    parent_values: str = "replicaCount: 1\n",
    child_values: str = "port: 8080\nimage: nginx\n",
    parent_chart_yaml: str | None = None,
    child_chart_yaml: str | None = None,
    child_name: str = "child",
) -> tuple[Path, Path]:
    parent = base_dir / "parent"
    parent.mkdir(parents=True, exist_ok=True)
    (parent / "Chart.yaml").write_text(
        parent_chart_yaml or "apiVersion: v2\nname: parent\nversion: 0.1.0\n"
    )
    (parent / "values.yaml").write_text(parent_values)
    parent_templates = parent / "templates"
    parent_templates.mkdir(exist_ok=True)
    (parent_templates / "dump.yaml").write_text(
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        "  name: parent-values-dump\n"
        "data:\n"
        "  values: |\n"
        "    {{ toJson .Values }}\n"
    )

    charts_dir = parent / "charts"
    charts_dir.mkdir(exist_ok=True)
    child = charts_dir / child_name
    child.mkdir(parents=True, exist_ok=True)
    (child / "Chart.yaml").write_text(
        child_chart_yaml or f"apiVersion: v2\nname: {child_name}\nversion: 0.1.0\n"
    )
    (child / "values.yaml").write_text(child_values)
    child_templates = child / "templates"
    child_templates.mkdir(exist_ok=True)
    (child_templates / "dump.yaml").write_text(
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        "  name: child-values-dump\n"
        "data:\n"
        "  values: |\n"
        "    {{ toJson .Values }}\n"
    )

    return parent, child


class TestSubchartChildDefaults(unittest.TestCase):
    @unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
    def test_child_defaults_differential(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent, _ = _make_base_chart(
                Path(tmpdir),
                parent_values="replicaCount: 2\n",
                child_values="servicePort: 8080\nenabled: true\n",
            )
            helm_vals = _helm_template_values(parent)
            vt_doc = _vt_json(parent)
            vt_vals = _vt_values_dict(vt_doc)

            self.assertEqual(helm_vals["child"]["servicePort"], vt_vals["child"]["servicePort"])
            self.assertEqual(helm_vals["child"]["enabled"], vt_vals["child"]["enabled"])
            self.assertEqual(helm_vals["replicaCount"], vt_vals["replicaCount"])

            rows = {r["key"]: r for r in vt_doc["values"]}
            self.assertIn("child.servicePort", rows)
            self.assertEqual(rows["child.servicePort"]["value"], 8080)
            self.assertIn("charts/child/values.yaml", rows["child.servicePort"]["source"])
            self.assertEqual(len(rows["child.servicePort"]["assignments"]), 1)


class TestSubchartParentOverrides(unittest.TestCase):
    @unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
    def test_parent_overrides_child_differential(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent, _ = _make_base_chart(
                Path(tmpdir),
                parent_values="replicaCount: 1\nchild:\n  servicePort: 9000\n",
                child_values="servicePort: 8080\nenabled: true\n",
            )
            helm_vals = _helm_template_values(parent)
            vt_doc = _vt_json(parent)
            vt_vals = _vt_values_dict(vt_doc)

            self.assertEqual(helm_vals["child"]["servicePort"], 9000)
            self.assertEqual(vt_vals["child"]["servicePort"], 9000)
            self.assertEqual(vt_vals["child"]["enabled"], True)

            rows = {r["key"]: r for r in vt_doc["values"]}
            port_row = rows["child.servicePort"]
            self.assertEqual(len(port_row["assignments"]), 2)
            self.assertIn("charts/child/values.yaml", port_row["assignments"][0]["source"])
            self.assertEqual(port_row["assignments"][0]["value"], 8080)
            self.assertIn("values.yaml", port_row["assignments"][1]["source"])
            self.assertEqual(port_row["assignments"][1]["value"], 9000)
            self.assertIn("parent/values.yaml", port_row["source"])


class TestSubchartGlobalPropagation(unittest.TestCase):
    @unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
    def test_global_values_differential(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent, _ = _make_base_chart(
                Path(tmpdir),
                parent_values="global:\n  environment: production\n  sharedKey: from-parent\n",
                child_values="global:\n  environment: development\n  childOnly: unique\n",
            )
            helm_vals = _helm_template_values(parent)
            vt_doc = _vt_json(parent)
            vt_vals = _vt_values_dict(vt_doc)

            self.assertEqual(helm_vals["global"]["environment"], "production")
            self.assertEqual(vt_vals["global"]["environment"], "production")
            self.assertEqual(helm_vals["child"]["global"]["environment"], "production")
            self.assertEqual(vt_vals["child"]["global"]["environment"], "production")
            self.assertEqual(helm_vals["child"]["global"]["childOnly"], "unique")
            self.assertEqual(vt_vals["child"]["global"]["childOnly"], "unique")

    @unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
    def test_global_with_set_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent, _ = _make_base_chart(
                Path(tmpdir),
                parent_values="global:\n  environment: production\n",
                child_values="global:\n  environment: development\n",
            )
            extra = ["--set", "global.environment=staging"]
            helm_vals = _helm_template_values(parent, extra)
            vt_doc = _vt_json(parent, extra)
            vt_vals = _vt_values_dict(vt_doc)

            self.assertEqual(helm_vals["global"]["environment"], "staging")
            self.assertEqual(vt_vals["global"]["environment"], "staging")
            self.assertEqual(helm_vals["child"]["global"]["environment"], "staging")
            self.assertEqual(vt_vals["child"]["global"]["environment"], "staging")

            rows = {r["key"]: r for r in vt_doc["values"]}
            row = rows["child.global.environment"]
            self.assertEqual(row["value"], "staging")
            self.assertGreaterEqual(len(row["assignments"]), 2)
            self.assertEqual(row["assignments"][-1]["value"], "staging")
            self.assertEqual(row["assignments"][-1]["source_type"], "set")


class TestSubchartValuesFiles(unittest.TestCase):
    @unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
    def test_values_file_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent, _ = _make_base_chart(
                Path(tmpdir),
                parent_values="replicaCount: 1\n",
                child_values="replicaCount: 1\nservicePort: 8080\n",
            )
            vf = Path(tmpdir) / "custom-values.yaml"
            vf.write_text("child:\n  replicaCount: 5\n")

            extra = ["-f", str(vf)]
            helm_vals = _helm_template_values(parent, extra)
            vt_doc = _vt_json(parent, extra)
            vt_vals = _vt_values_dict(vt_doc)

            self.assertEqual(helm_vals["child"]["replicaCount"], 5)
            self.assertEqual(vt_vals["child"]["replicaCount"], 5)

            rows = {r["key"]: r for r in vt_doc["values"]}
            row = rows["child.replicaCount"]
            self.assertEqual(row["value"], 5)
            self.assertIn("custom-values.yaml", row["source"])
            self.assertEqual(len(row["assignments"]), 2)


class TestSubchartCliOverrides(unittest.TestCase):
    @unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
    def test_set_scalar(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent, _ = _make_base_chart(Path(tmpdir))
            extra = ["--set", "child.port=9999"]
            helm_vals = _helm_template_values(parent, extra)
            vt_doc = _vt_json(parent, extra)
            vt_vals = _vt_values_dict(vt_doc)
            self.assertEqual(helm_vals["child"]["port"], 9999)
            self.assertEqual(vt_vals["child"]["port"], 9999)

    @unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
    def test_set_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent, _ = _make_base_chart(Path(tmpdir))
            extra = ["--set-string", "child.image=1.26"]
            helm_vals = _helm_template_values(parent, extra)
            vt_doc = _vt_json(parent, extra)
            vt_vals = _vt_values_dict(vt_doc)
            self.assertEqual(helm_vals["child"]["image"], "1.26")
            self.assertEqual(vt_vals["child"]["image"], "1.26")

    @unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
    def test_set_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent, _ = _make_base_chart(Path(tmpdir))
            extra = ["--set-json", 'child.config={"enabled":true,"limit":100}']
            helm_vals = _helm_template_values(parent, extra)
            vt_doc = _vt_json(parent, extra)
            vt_vals = _vt_values_dict(vt_doc)
            self.assertEqual(helm_vals["child"]["config"], {"enabled": True, "limit": 100})
            self.assertEqual(vt_vals["child"]["config"], {"enabled": True, "limit": 100})

    @unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
    def test_set_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent, _ = _make_base_chart(Path(tmpdir))
            content_file = Path(tmpdir) / "config.txt"
            content_file.write_text("hello-from-file")
            extra = ["--set-file", f"child.fileData={content_file}"]
            helm_vals = _helm_template_values(parent, extra)
            vt_doc = _vt_json(parent, extra)
            vt_vals = _vt_values_dict(vt_doc)
            self.assertEqual(helm_vals["child"]["fileData"], "hello-from-file")
            self.assertEqual(vt_vals["child"]["fileData"], "hello-from-file")

    @unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
    def test_set_literal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent, _ = _make_base_chart(Path(tmpdir))
            extra = ["--set-literal", "child.tag=v1,2,3"]
            helm_vals = _helm_template_values(parent, extra)
            vt_doc = _vt_json(parent, extra)
            vt_vals = _vt_values_dict(vt_doc)
            self.assertEqual(helm_vals["child"]["tag"], "v1,2,3")
            self.assertEqual(vt_vals["child"]["tag"], "v1,2,3")


class TestMultipleSubcharts(unittest.TestCase):
    @unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
    def test_multiple_subcharts_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            parent, _ = _make_base_chart(
                base,
                parent_values="replicaCount: 1\n",
                child_values="port: 8080\n",
                child_name="child-a",
            )
            child_b = parent / "charts" / "child-b"
            child_b.mkdir(parents=True)
            (child_b / "Chart.yaml").write_text("apiVersion: v2\nname: child-b\nversion: 0.1.0\n")
            (child_b / "values.yaml").write_text("port: 9090\n")
            child_b_tpl = child_b / "templates"
            child_b_tpl.mkdir()
            (child_b_tpl / "dump.yaml").write_text(
                "apiVersion: v1\n"
                "kind: ConfigMap\n"
                "metadata:\n"
                "  name: b-dump\n"
                "data:\n"
                "  values: |\n"
                "    {{ toJson .Values }}\n"
            )

            extra = ["--set", "child-a.port=8000", "--set", "child-b.port=9000"]
            helm_vals = _helm_template_values(parent, extra)
            vt_doc = _vt_json(parent, extra)
            vt_vals = _vt_values_dict(vt_doc)

            self.assertEqual(helm_vals["child-a"]["port"], 8000)
            self.assertEqual(helm_vals["child-b"]["port"], 9000)
            self.assertEqual(vt_vals["child-a"]["port"], 8000)
            self.assertEqual(vt_vals["child-b"]["port"], 9000)


class TestSubchartAliases(unittest.TestCase):
    @unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
    def test_dependency_alias_differential(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent_chart_yaml = """apiVersion: v2
name: parent
version: 0.1.0
dependencies:
  - name: child
    version: 0.1.0
    alias: backend
"""
            parent, _ = _make_base_chart(
                Path(tmpdir),
                parent_values="backend:\n  port: 9000\n",
                child_values="port: 8080\nimage: alpine\n",
                parent_chart_yaml=parent_chart_yaml,
            )
            extra = ["--set", "backend.image=nginx"]
            helm_vals = _helm_template_values(parent, extra)
            vt_doc = _vt_json(parent, extra)
            vt_vals = _vt_values_dict(vt_doc)

            self.assertEqual(helm_vals["backend"]["port"], 9000)
            self.assertEqual(helm_vals["backend"]["image"], "nginx")
            self.assertEqual(vt_vals["backend"]["port"], 9000)
            self.assertEqual(vt_vals["backend"]["image"], "nginx")

            rows = {r["key"]: r for r in vt_doc["values"]}
            self.assertIn("backend.port", rows)
            self.assertNotIn("child.port", rows)


class TestNestedSubcharts(unittest.TestCase):
    @unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
    def test_recursive_nested_subcharts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent, child = _make_base_chart(
                Path(tmpdir),
                parent_values="child:\n  grandchild:\n    port: 9000\n",
                child_values="childParam: true\ngrandchild:\n  port: 8000\n",
            )
            grandchild = child / "charts" / "grandchild"
            grandchild.mkdir(parents=True)
            (grandchild / "Chart.yaml").write_text(
                "apiVersion: v2\nname: grandchild\nversion: 0.1.0\n"
            )
            (grandchild / "values.yaml").write_text("port: 5000\ntype: ClusterIP\n")
            gc_tpl = grandchild / "templates"
            gc_tpl.mkdir()
            (gc_tpl / "dump.yaml").write_text(
                "apiVersion: v1\n"
                "kind: ConfigMap\n"
                "metadata:\n"
                "  name: gc-dump\n"
                "data:\n"
                "  values: |\n"
                "    {{ toJson .Values }}\n"
            )

            helm_vals = _helm_template_values(parent)
            vt_doc = _vt_json(parent)
            vt_vals = _vt_values_dict(vt_doc)

            self.assertEqual(helm_vals["child"]["grandchild"]["port"], 9000)
            self.assertEqual(helm_vals["child"]["grandchild"]["type"], "ClusterIP")
            self.assertEqual(vt_vals["child"]["grandchild"]["port"], 9000)
            self.assertEqual(vt_vals["child"]["grandchild"]["type"], "ClusterIP")

            rows = {r["key"]: r for r in vt_doc["values"]}
            port_row = rows["child.grandchild.port"]
            self.assertEqual(len(port_row["assignments"]), 3)
            self.assertEqual(port_row["assignments"][0]["value"], 5000)
            self.assertEqual(port_row["assignments"][1]["value"], 8000)
            self.assertEqual(port_row["assignments"][2]["value"], 9000)


class TestSubchartExplain(unittest.TestCase):
    def test_explain_subchart_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent, _ = _make_base_chart(
                Path(tmpdir),
                parent_values="child:\n  port: 9000\n",
                child_values="port: 8080\n",
            )
            rc, stdout, _ = _vt_run([str(parent), "explain", "child.port", "--no-redact"])
            self.assertEqual(rc, 0)
            self.assertIn("child.port", stdout)
            self.assertIn("9000", stdout)
            self.assertIn("8080", stdout)
            self.assertIn("charts/child/values.yaml", stdout)

    def test_explain_global_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent, _ = _make_base_chart(
                Path(tmpdir),
                parent_values="global:\n  env: prod\n",
                child_values="global:\n  env: dev\n",
            )
            rc, stdout, _ = _vt_run([str(parent), "explain", "global.env", "--no-redact"])
            self.assertEqual(rc, 0)
            self.assertIn("global.env", stdout)
            self.assertIn("prod", stdout)

    def test_explain_child_global_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent, _ = _make_base_chart(
                Path(tmpdir),
                parent_values="global:\n  env: prod\n",
                child_values="global:\n  env: dev\n",
            )
            rc, stdout, _ = _vt_run([str(parent), "explain", "child.global.env", "--no-redact"])
            self.assertEqual(rc, 0)
            self.assertIn("child.global.env", stdout)
            self.assertIn("prod", stdout)
            self.assertIn("dev", stdout)


class TestSubchartOutputFormats(unittest.TestCase):
    def test_table_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent, _ = _make_base_chart(
                Path(tmpdir),
                parent_values="child:\n  port: 9000\n",
                child_values="port: 8080\ntag: latest\n",
            )
            rc, stdout, _ = _vt_run([str(parent), "-o", "table", "--no-redact"])
            self.assertEqual(rc, 0)
            self.assertIn("child.port", stdout)
            self.assertIn("child.tag", stdout)
            self.assertIn("9000", stdout)
            self.assertIn("latest", stdout)

    def test_yaml_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent, _ = _make_base_chart(
                Path(tmpdir),
                parent_values="child:\n  port: 9000\n",
                child_values="port: 8080\n",
            )
            rc, stdout, _ = _vt_run([str(parent), "-o", "yaml", "--no-redact"])
            self.assertEqual(rc, 0)
            doc = yaml.safe_load(stdout)
            keys = [row["key"] for row in doc["values"]]
            self.assertIn("child.port", keys)

    def test_only_overridden_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent, _ = _make_base_chart(
                Path(tmpdir),
                parent_values="child:\n  port: 9000\n",
                child_values="port: 8080\ntag: latest\n",
            )
            rc, stdout, _ = _vt_run([str(parent), "--only-overridden", "-o", "json", "--no-redact"])
            self.assertEqual(rc, 0)
            doc = json.loads(stdout)
            keys = [row["key"] for row in doc["values"]]
            self.assertIn("child.port", keys)
            self.assertNotIn("child.tag", keys)


class TestSubchartErrorHandling(unittest.TestCase):
    def test_missing_dependency_raises_chart_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "parent"
            parent.mkdir()
            (parent / "Chart.yaml").write_text("""apiVersion: v2
name: parent
version: 0.1.0
dependencies:
  - name: missing-dep
    version: 0.1.0
""")
            (parent / "values.yaml").write_text("replicaCount: 1\n")
            rc, _, stderr = _vt_run([str(parent)])
            self.assertEqual(rc, 3)
            self.assertIn("Error:", stderr)
            self.assertIn("missing in charts/ directory", stderr)
            self.assertNotIn("Traceback", stderr)

    def test_malformed_child_chart_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent, child = _make_base_chart(Path(tmpdir))
            (child / "Chart.yaml").write_text("bad: [yaml: not: valid")
            rc, _, stderr = _vt_run([str(parent)])
            self.assertEqual(rc, 3)
            self.assertIn("Error:", stderr)
            self.assertIn("Malformed Chart.yaml", stderr)
            self.assertNotIn("Traceback", stderr)

    def test_malformed_child_values_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent, child = _make_base_chart(Path(tmpdir))
            (child / "values.yaml").write_text("bad: [values: not: valid")
            rc, _, stderr = _vt_run([str(parent)])
            self.assertEqual(rc, 3)
            self.assertIn("Error:", stderr)
            self.assertIn("Malformed values.yaml", stderr)
            self.assertNotIn("Traceback", stderr)

    def test_duplicate_dependency_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "parent"
            parent.mkdir()
            (parent / "Chart.yaml").write_text("""apiVersion: v2
name: parent
version: 0.1.0
dependencies:
  - name: child1
    alias: dupalias
  - name: child2
    alias: dupalias
""")
            (parent / "values.yaml").write_text("replicaCount: 1\n")
            charts = parent / "charts"
            charts.mkdir()
            (charts / "child1").mkdir()
            (charts / "child1" / "Chart.yaml").write_text(
                "apiVersion: v2\nname: child1\nversion: 0.1.0\n"
            )
            (charts / "child2").mkdir()
            (charts / "child2" / "Chart.yaml").write_text(
                "apiVersion: v2\nname: child2\nversion: 0.1.0\n"
            )

            rc, _, stderr = _vt_run([str(parent)])
            self.assertEqual(rc, 3)
            self.assertIn("Error:", stderr)
            self.assertIn("More than one dependency with name or alias", stderr)
            self.assertNotIn("Traceback", stderr)

    def test_declared_dependency_missing_chart_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "parent"
            parent.mkdir()
            (parent / "Chart.yaml").write_text("""apiVersion: v2
name: parent
version: 0.1.0
dependencies:
  - name: child
    version: 0.1.0
""")
            charts = parent / "charts"
            charts.mkdir()
            (charts / "child").mkdir()

            rc, _, stderr = _vt_run([str(parent)])
            self.assertEqual(rc, 3)
            self.assertIn("Error:", stderr)
            self.assertIn("missing Chart.yaml", stderr)
            self.assertNotIn("Traceback", stderr)

    @unittest.skipUnless(HELM_AVAILABLE, SKIP_NO_HELM)
    def test_tgz_archive_with_subcharts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            parent, _child = _make_base_chart(
                tmp_path,
                parent_values="replicaCount: 1\nchild:\n  port: 9000\n",
                child_values="port: 8080\n",
            )
            subprocess.run(
                ["helm", "package", str(parent), "--destination", str(tmp_path)],
                check=True,
                capture_output=True,
            )
            pkg = tmp_path / "parent-0.1.0.tgz"
            self.assertTrue(pkg.is_file())

            doc = _vt_json(pkg)
            values_map = {row["key"]: row["value"] for row in doc.get("values", [])}
            self.assertEqual(values_map.get("child.port"), 9000)
            self.assertEqual(values_map.get("replicaCount"), 1)

            port_row = next(r for r in doc["values"] if r["key"] == "child.port")
            sources = [a["source"] for a in port_row["assignments"]]
            self.assertIn("charts/child/values.yaml:1", sources)


if __name__ == "__main__":
    unittest.main()
