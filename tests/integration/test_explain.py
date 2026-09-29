"""Integration tests for the 'explain' subcommand (Phase 7)."""

from __future__ import annotations

import json
import sys
import unittest
from io import StringIO
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "basic"
SECURITY_FIXTURE = Path(__file__).parent.parent / "fixtures" / "security"

BASIC_CHART = Path(__file__).parent.parent / "fixtures" / "basic"


def _ensure_basic_fixture() -> Path:
    """Ensure a basic chart fixture exists for testing."""
    chart_dir = Path(__file__).parent.parent / "fixtures" / "_explain_test_chart"
    if chart_dir.exists():
        return chart_dir
    chart_dir.mkdir(parents=True, exist_ok=True)
    (chart_dir / "Chart.yaml").write_text("apiVersion: v2\nname: test-chart\nversion: 0.1.0\n")
    (chart_dir / "values.yaml").write_text(
        "image:\n  repository: nginx\n  tag: latest\nreplicaCount: 1\n"
    )
    return chart_dir


def _run_cli(args: list[str]) -> tuple[int, str, str]:
    from helm_valuetrace.cli import main

    buf_out = StringIO()
    buf_err = StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = buf_out
    sys.stderr = buf_err
    try:
        rc = main(args)
    finally:
        sys.stdout = old_out
        sys.stderr = old_err
    return rc, buf_out.getvalue(), buf_err.getvalue()


class TestExplainSubcommand(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.chart = _ensure_basic_fixture()

    def test_explain_returns_zero(self) -> None:
        rc, _, _ = _run_cli([str(self.chart), "explain", "image.repository", "--no-redact"])
        self.assertEqual(rc, 0)

    def test_explain_shows_key_name(self) -> None:
        _, stdout, _ = _run_cli([str(self.chart), "explain", "image.repository", "--no-redact"])
        self.assertIn("image.repository", stdout)

    def test_explain_shows_value(self) -> None:
        _, stdout, _ = _run_cli([str(self.chart), "explain", "image.repository", "--no-redact"])
        self.assertIn("nginx", stdout)

    def test_explain_shows_source_history(self) -> None:
        _, stdout, _ = _run_cli([str(self.chart), "explain", "image.repository", "--no-redact"])
        self.assertIn("Source history", stdout)

    def test_explain_json_format(self) -> None:
        rc, stdout, _ = _run_cli(
            [
                str(self.chart),
                "explain",
                "image.repository",
                "-o",
                "json",
                "--no-redact",
            ]
        )
        self.assertEqual(rc, 0)
        doc = json.loads(stdout)
        self.assertEqual(doc["key"], "image.repository")
        self.assertIn("history", doc)
        self.assertIn("final_value", doc)

    def test_explain_missing_key_error(self) -> None:
        rc, _, stderr = _run_cli([str(self.chart), "explain", "nonexistent.key"])
        self.assertEqual(rc, 1)
        self.assertIn("not found", stderr.lower())

    def test_explain_redacts_sensitive(self) -> None:
        rc, stdout, _ = _run_cli(
            [
                str(SECURITY_FIXTURE),
                "explain",
                "adminPassword",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("<redacted>", stdout)
        self.assertNotIn("FAKE_SECRET_DO_NOT_USE", stdout)

    def test_explain_no_redact_shows_plaintext(self) -> None:
        rc, stdout, _ = _run_cli(
            [
                str(SECURITY_FIXTURE),
                "explain",
                "adminPassword",
                "--no-redact",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("FAKE_SECRET_DO_NOT_USE", stdout)

    def test_explain_with_set_override(self) -> None:
        _, stdout, _ = _run_cli(
            [
                str(self.chart),
                "explain",
                "image.repository",
                "--set",
                "image.repository=my-image",
                "--no-redact",
            ]
        )
        self.assertIn("my-image", stdout)
        self.assertIn("Final winner", stdout)

    def test_explain_array_path(self) -> None:
        rc, stdout, _ = _run_cli(
            [
                str(self.chart),
                "explain",
                "servers[0].port",
                "--set",
                "servers[0].port=8080",
                "--no-redact",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("servers[0].port", stdout)
        self.assertIn("8080", stdout)
        self.assertIn("Final winner", stdout)

    def test_unknown_subcommand_error(self) -> None:
        rc, _, stderr = _run_cli([str(self.chart), "badcommand"])
        self.assertEqual(rc, 1)
        self.assertIn("Unknown subcommand", stderr)


if __name__ == "__main__":
    unittest.main()
