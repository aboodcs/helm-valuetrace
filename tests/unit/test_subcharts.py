"""Tests for experimental subchart support (Phase 9)."""

from __future__ import annotations

import sys
import unittest
from io import StringIO
from pathlib import Path

FIXTURE = Path(__file__).parent.parent / "fixtures" / "subcharts"


class TestLoadSubchartValues(unittest.TestCase):
    def test_finds_subchart(self) -> None:
        from helm_valuetrace.subcharts import load_subchart_values

        parent_values = {"mysubchart": {"servicePort": 9090}, "global": {"storageClass": "fast"}}
        result = load_subchart_values(FIXTURE, parent_values)
        self.assertIn("mysubchart", result)

    def test_parent_override_applied(self) -> None:
        from helm_valuetrace.subcharts import load_subchart_values

        parent_values = {"mysubchart": {"servicePort": 9090}}
        result = load_subchart_values(FIXTURE, parent_values)
        sc_values, _ = result["mysubchart"]
        self.assertEqual(sc_values["servicePort"], 9090)

    def test_global_values_propagated(self) -> None:
        from helm_valuetrace.subcharts import load_subchart_values

        parent_values = {"global": {"storageClass": "fast"}}
        result = load_subchart_values(FIXTURE, parent_values)
        sc_values, _ = result["mysubchart"]
        self.assertIn("global", sc_values)
        self.assertEqual(sc_values["global"]["storageClass"], "fast")

    def test_subchart_defaults_preserved(self) -> None:
        from helm_valuetrace.subcharts import load_subchart_values

        parent_values: dict = {}
        result = load_subchart_values(FIXTURE, parent_values)
        sc_values, _ = result["mysubchart"]
        self.assertEqual(sc_values["servicePort"], 8080)
        self.assertFalse(sc_values["enabled"])

    def test_no_charts_dir(self) -> None:
        import tempfile

        from helm_valuetrace.subcharts import load_subchart_values

        with tempfile.TemporaryDirectory() as tmpdir:
            result = load_subchart_values(Path(tmpdir), {})
        self.assertEqual(result, {})

    def test_source_label(self) -> None:
        from helm_valuetrace.subcharts import load_subchart_values

        result = load_subchart_values(FIXTURE, {})
        _, source = result["mysubchart"]
        self.assertIn("mysubchart", source)
        self.assertIn("values.yaml", source)


class TestCLISubcharts(unittest.TestCase):
    def _run(self, extra_args: list[str] | None = None) -> tuple[int, str, str]:
        from helm_valuetrace.cli import main

        buf_out = StringIO()
        buf_err = StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = buf_out
        sys.stderr = buf_err
        try:
            rc = main([str(FIXTURE), "--no-redact"] + (extra_args or []))
        finally:
            sys.stdout = old_out
            sys.stderr = old_err
        return rc, buf_out.getvalue(), buf_err.getvalue()

    def test_experimental_subcharts_exits_zero(self) -> None:
        rc, _, _ = self._run(["--experimental-subcharts"])
        self.assertEqual(rc, 0)

    def test_experimental_subcharts_prints_warning(self) -> None:
        _, _, stderr = self._run(["--experimental-subcharts"])
        self.assertIn("experimental", stderr.lower())

    def test_experimental_subcharts_reports_found_subchart(self) -> None:
        _, _, stderr = self._run(["--experimental-subcharts"])
        self.assertIn("mysubchart", stderr)

    def test_subcharts_off_by_default(self) -> None:
        _, _, stderr = self._run()
        self.assertNotIn("experimental", stderr.lower())


if __name__ == "__main__":
    unittest.main()
