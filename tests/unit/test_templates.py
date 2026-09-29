"""Tests for static template analysis (Phase 8)."""

from __future__ import annotations

import sys
import unittest
from io import StringIO
from pathlib import Path

FIXTURE = Path(__file__).parent.parent / "fixtures" / "templates"


class TestFindValueReferences(unittest.TestCase):
    def test_finds_used_keys(self) -> None:
        from helm_valuetrace.analysis.templates import find_value_references

        refs = find_value_references(FIXTURE)
        self.assertIn("replicaCount", refs)
        self.assertIn("image.repository", refs)
        self.assertIn("image.tag", refs)
        self.assertIn("service.port", refs)

    def test_each_ref_lists_template_file(self) -> None:
        from helm_valuetrace.analysis.templates import find_value_references

        refs = find_value_references(FIXTURE)
        self.assertTrue(any("deployment.yaml" in f for f in refs["replicaCount"]))

    def test_no_duplicates_per_template(self) -> None:
        from helm_valuetrace.analysis.templates import find_value_references

        refs = find_value_references(FIXTURE)
        for key, files in refs.items():
            self.assertEqual(len(files), len(set(files)), f"Duplicate file for key {key!r}")

    def test_empty_templates_dir(self) -> None:
        import tempfile

        from helm_valuetrace.analysis.templates import find_value_references

        with tempfile.TemporaryDirectory() as tmpdir:
            chart_dir = Path(tmpdir)
            (chart_dir / "Chart.yaml").write_text("apiVersion: v2\nname: x\nversion: 0.1.0\n")
            refs = find_value_references(chart_dir)
        self.assertEqual(refs, {})

    def test_no_templates_dir(self) -> None:
        import tempfile

        from helm_valuetrace.analysis.templates import find_value_references

        with tempfile.TemporaryDirectory() as tmpdir:
            refs = find_value_references(Path(tmpdir))
        self.assertEqual(refs, {})

    def test_limitations_list_is_populated(self) -> None:
        from helm_valuetrace.analysis.templates import LIMITATIONS

        self.assertGreater(len(LIMITATIONS), 0)
        for item in LIMITATIONS:
            self.assertIsInstance(item, str)


class TestCLITemplateAnalysis(unittest.TestCase):
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

    def test_template_analysis_exits_zero(self) -> None:
        rc, _, _ = self._run(["--template-analysis"])
        self.assertEqual(rc, 0)

    def test_template_analysis_shows_limitations(self) -> None:
        _, _, stderr = self._run(["--template-analysis"])
        self.assertIn("TEMPLATE ANALYSIS", stderr)
        self.assertIn("Limitations", stderr)

    def test_template_analysis_reports_unused(self) -> None:
        _, _, stderr = self._run(["--template-analysis"])
        self.assertIn("unusedKey", stderr)

    def test_template_analysis_off_by_default(self) -> None:
        _, _, stderr = self._run()
        self.assertNotIn("TEMPLATE ANALYSIS", stderr)


if __name__ == "__main__":
    unittest.main()
