from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name)

        self.chart = self.workspace / "chart"
        self.chart.mkdir()
        self._write(
            self.chart / "Chart.yaml",
            "apiVersion: v2\nname: test-chart\nversion: 0.1.0\n",
        )
        self._write(
            self.chart / "values.yaml",
            "replicaCount: 1\nimage:\n  repository: nginx\n  tag: stable\n",
        )

        self.development = self.workspace / "values-development.yaml"
        self.production = self.workspace / "values-production.yaml"
        self.typo = self.workspace / "values-typo.yaml"
        self.reference = self.workspace / "values-reference.yaml"
        self.actual = self.workspace / "values-actual.yaml"

        self._write(self.development, "replicaCount: 2\nimage:\n  tag: dev\n")
        self._write(self.production, "replicaCount: 4\nimage:\n  tag: production\n")
        self._write(self.typo, "image:\n  repostory: custom/nginx\n")
        self._write(
            self.reference,
            "image:\n  repository: nginx\n  tag: stable\nservice:\n  port: 80\n",
        )
        self._write(
            self.actual,
            "image:\n  repository: nginx\n  tag: staging\nservice:\n  targetPort: 8080\n",
        )

        self.reference_chart = self.workspace / "reference-chart"
        self.reference_chart.mkdir()
        self._write(
            self.reference_chart / "Chart.yaml",
            "apiVersion: v2\nname: reference-chart\nversion: 0.1.0\n",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def _write(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        return subprocess.run(
            ["python3", "-m", "helm_valuetrace.cli", *arguments],
            cwd=PROJECT_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_help_is_complete_and_uses_valuetrace_branding(self) -> None:
        process = self.run_cli("--help")

        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("usage: helm valuetrace CHART [options]", process.stdout)
        self.assertIn("Required argument", process.stdout)
        self.assertIn("Value sources", process.stdout)
        self.assertIn("Validation", process.stdout)
        self.assertIn("Output", process.stdout)
        self.assertIn("VALUE PRECEDENCE", process.stdout)
        self.assertIn("EXIT CODES", process.stdout)
        self.assertNotIn("helm values-source", process.stdout)

    def test_version_uses_product_name(self) -> None:
        process = self.run_cli("--version")

        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.strip(), "Helm ValueTrace 0.1.0")

    def test_json_output_contains_final_source(self) -> None:
        process = self.run_cli(
            str(self.chart),
            "-f",
            str(self.development),
            "-f",
            str(self.production),
            "--set",
            "image.tag=manual",
            "-o",
            "json",
        )

        self.assertEqual(process.returncode, 0, process.stderr)
        document = json.loads(process.stdout)
        image_tag = next(item for item in document["values"] if item["key"] == "image.tag")
        self.assertEqual(image_tag["value"], "manual")
        self.assertEqual(image_tag["source"], "--set[1]")
        self.assertEqual(len(image_tag["assignments"]), 4)

    def test_strict_unknown_returns_status_two(self) -> None:
        process = self.run_cli(
            str(self.chart),
            "-f",
            str(self.typo),
            "--strict-unknown",
        )

        self.assertEqual(process.returncode, 2)
        self.assertIn("image.repostory", process.stderr)
        self.assertIn("image.repository", process.stderr)

    def test_reference_comparison_reports_unknown_and_missing_keys(self) -> None:
        process = self.run_cli(
            str(self.reference_chart),
            "--reference-values",
            str(self.reference),
            "-f",
            str(self.actual),
            "--strict-reference",
            "-o",
            "json",
        )

        self.assertEqual(process.returncode, 2)
        document = json.loads(process.stdout)
        unknown = {item["key"] for item in document["unknown"]}
        missing = {item["key"] for item in document["missing"]}
        self.assertEqual(unknown, {"service.targetPort"})
        self.assertEqual(missing, {"service.port"})

    def test_strict_reference_requires_a_reference_file(self) -> None:
        process = self.run_cli(str(self.chart), "--strict-reference")

        self.assertEqual(process.returncode, 1)
        self.assertIn(
            "--strict-reference requires --reference-values FILE",
            process.stderr,
        )

    def test_missing_chart_returns_status_one_without_traceback(self) -> None:
        process = self.run_cli(str(self.workspace / "missing-chart"))

        self.assertEqual(process.returncode, 1)
        self.assertIn("Chart directory not found", process.stderr)
        self.assertNotIn("Traceback", process.stderr)


if __name__ == "__main__":
    unittest.main()
