from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from helm_valuetrace.core import flatten_values, path_text  # noqa: E402


HELM_BIN = os.environ.get("HELM_BIN") or shutil.which("helm")


@unittest.skipUnless(HELM_BIN, "Helm compatibility tests require HELM_BIN or helm on PATH")
class HelmCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name)
        self.chart = self.workspace / "chart"
        (self.chart / "templates").mkdir(parents=True)

        self._write(
            self.chart / "Chart.yaml",
            "apiVersion: v2\nname: compatibility\nversion: 0.1.0\n",
        )
        self._write(
            self.chart / "values.yaml",
            """\
word: original
leadingZero: original
floatValue: original
enabled: false
count: 0
nullable: original
removedViaSet: original
defaultNull: null
service:
  port: 80
  type: ClusterIP
""",
        )
        self._write(
            self.chart / "templates" / "values.yaml",
            """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: compatibility
data:
  values: {{ .Values | toJson | quote }}
""",
        )

        self.first_override = self.workspace / "first.yaml"
        self.second_override = self.workspace / "second.yaml"
        self._write(
            self.first_override,
            "nullable: null\nservice: disabled\n",
        )
        self._write(
            self.second_override,
            "service:\n  port: 8080\nuserOnlyNull: null\n",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def _write(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")

    def _common_arguments(self) -> list[str]:
        return [
            str(self.chart),
            "-f",
            str(self.first_override),
            "-f",
            str(self.second_override),
            "--set",
            "word=yes",
            "--set",
            "leadingZero=0123",
            "--set",
            "floatValue=1.0",
            "--set",
            "enabled=true",
            "--set",
            "count=42",
            "--set",
            "removedViaSet=null",
        ]

    def test_final_values_match_helm(self) -> None:
        helm = subprocess.run(
            [str(HELM_BIN), "template", "compatibility", *self._common_arguments()],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(helm.returncode, 0, helm.stderr)
        manifest = yaml.safe_load(helm.stdout)
        helm_values = json.loads(manifest["data"]["values"])

        environment = os.environ.copy()
        environment["HELM_BIN"] = str(HELM_BIN)
        environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        valuetrace = subprocess.run(
            [
                sys.executable,
                "-m",
                "helm_valuetrace.cli",
                *self._common_arguments(),
                "--output",
                "json",
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(valuetrace.returncode, 0, valuetrace.stderr)
        report = json.loads(valuetrace.stdout)

        expected = {
            path_text(path): value
            for path, value in flatten_values(helm_values).items()
        }
        actual = {row["key"]: row["value"] for row in report["values"]}

        self.assertEqual(actual, expected)
        self.assertEqual(actual["word"], "yes")
        self.assertEqual(actual["leadingZero"], "0123")
        self.assertEqual(actual["floatValue"], "1.0")
        self.assertEqual(actual["service.type"], "ClusterIP")
        self.assertNotIn("nullable", actual)
        self.assertNotIn("removedViaSet", actual)


if __name__ == "__main__":
    unittest.main()
