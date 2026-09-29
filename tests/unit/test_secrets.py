"""Tests for secret redaction (Phase 4B)."""

from __future__ import annotations

import json
import sys
import unittest
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from helm_valuetrace.analysis.secrets import (
    REDACTED,
    is_sensitive,
    redact_value,
)
from helm_valuetrace.cli import main

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "security"


class TestIsSensitive(unittest.TestCase):
    def test_password_key(self) -> None:
        self.assertTrue(is_sensitive("adminPassword"))

    def test_api_key(self) -> None:
        self.assertTrue(is_sensitive("apiKey"))

    def test_token(self) -> None:
        self.assertTrue(is_sensitive("apiToken"))

    def test_secret(self) -> None:
        self.assertTrue(is_sensitive("mySecret"))

    def test_credentials(self) -> None:
        self.assertTrue(is_sensitive("credentials"))

    def test_auth(self) -> None:
        self.assertTrue(is_sensitive("auth"))

    def test_non_sensitive(self) -> None:
        self.assertFalse(is_sensitive("replicaCount"))
        self.assertFalse(is_sensitive("image.repository"))
        self.assertFalse(is_sensitive("service.port"))

    def test_leaf_segment_only(self) -> None:
        self.assertFalse(is_sensitive("auth.username"))
        self.assertTrue(is_sensitive("auth.password"))

    def test_case_insensitive(self) -> None:
        self.assertTrue(is_sensitive("AdminPassword"))
        self.assertTrue(is_sensitive("API_KEY"))
        self.assertTrue(is_sensitive("SECRET"))

    def test_false_positives_excluded(self) -> None:
        self.assertFalse(is_sensitive("keyCount"))
        self.assertFalse(is_sensitive("monkey"))
        self.assertFalse(is_sensitive("donkey"))
        self.assertFalse(is_sensitive("authorizationMode"))
        self.assertFalse(is_sensitive("author"))
        self.assertFalse(is_sensitive("keyboard"))
        from helm_valuetrace.analysis.secrets import DEFAULT_SENSITIVE_PATTERNS

        self.assertTrue(is_sensitive("monkey", patterns=DEFAULT_SENSITIVE_PATTERNS + ["*monkey*"]))


class TestRedactValue(unittest.TestCase):
    def test_redacts_sensitive(self) -> None:
        result = redact_value("hunter2", "password")
        self.assertEqual(result, REDACTED)

    def test_passes_non_sensitive(self) -> None:
        result = redact_value("nginx", "image.repository")
        self.assertEqual(result, "nginx")

    def test_custom_pattern(self) -> None:
        result = redact_value("my-value", "myCustomSensitiveField", ["*customsensitivefield*"])
        self.assertEqual(result, REDACTED)


class TestCLIRedactionDefault(unittest.TestCase):
    """Sensitive values must be redacted by default."""

    def _run_json(self, extra_args: list[str] | None = None) -> dict:
        buf = StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            main([str(FIXTURE_DIR), "-o", "json"] + (extra_args or []))
        finally:
            sys.stdout = old_stdout
        return json.loads(buf.getvalue())

    def test_sensitive_values_redacted_by_default(self) -> None:
        doc = self._run_json()
        values_by_key = {row["key"]: row["value"] for row in doc["values"]}
        self.assertEqual(values_by_key["adminPassword"], REDACTED)
        self.assertEqual(values_by_key["apiToken"], REDACTED)
        self.assertEqual(values_by_key["apiKey"], REDACTED)
        self.assertEqual(values_by_key["auth.password"], REDACTED)
        self.assertEqual(values_by_key["credentials.accessKey"], REDACTED)
        self.assertEqual(values_by_key["credentials.secretKey"], REDACTED)

    def test_non_sensitive_values_not_redacted(self) -> None:
        doc = self._run_json()
        values_by_key = {row["key"]: row["value"] for row in doc["values"]}
        self.assertEqual(values_by_key["replicaCount"], 1)
        self.assertEqual(values_by_key["service.port"], 80)
        self.assertEqual(values_by_key["image.repository"], "nginx")

    def test_no_redact_shows_plaintext(self) -> None:
        doc = self._run_json(["--no-redact"])
        values_by_key = {row["key"]: row["value"] for row in doc["values"]}
        self.assertEqual(values_by_key["adminPassword"], "FAKE_SECRET_DO_NOT_USE")
        self.assertEqual(values_by_key["apiToken"], "FAKE_TOKEN_DO_NOT_USE")

    def test_sensitive_value_never_in_output_when_redacted(self) -> None:
        """Regression: raw sensitive string must not appear anywhere in JSON output."""
        buf = StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            main([str(FIXTURE_DIR), "-o", "json"])
        finally:
            sys.stdout = old_stdout
        output = buf.getvalue()
        self.assertNotIn("FAKE_SECRET_DO_NOT_USE", output)
        self.assertNotIn("FAKE_TOKEN_DO_NOT_USE", output)
        self.assertNotIn("FAKE_API_KEY_DO_NOT_USE", output)

    def test_custom_redact_pattern(self) -> None:
        doc = self._run_json(["--redact-pattern", "*repository*"])
        values_by_key = {row["key"]: row["value"] for row in doc["values"]}
        self.assertEqual(values_by_key["image.repository"], REDACTED)

    def test_assignment_values_also_redacted(self) -> None:
        """Assignment history values must also be redacted."""
        doc = self._run_json()
        for row in doc["values"]:
            if row["key"] == "adminPassword":
                for assignment in row["assignments"]:
                    self.assertEqual(assignment["value"], REDACTED)
                break

    def test_sensitive_value_never_in_table_output(self) -> None:
        """Secret values must not appear in default table output."""
        buf = StringIO()
        err_buf = StringIO()
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = buf, err_buf
        try:
            main([str(FIXTURE_DIR)])
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
        output = buf.getvalue()
        err = err_buf.getvalue()
        self.assertNotIn("FAKE_SECRET_DO_NOT_USE", output)
        self.assertNotIn("FAKE_SECRET_DO_NOT_USE", err)
        self.assertIn(REDACTED, output)

    def test_sensitive_value_never_in_yaml_output(self) -> None:
        """Secret values must not appear in YAML output."""
        buf = StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            main([str(FIXTURE_DIR), "-o", "yaml"])
        finally:
            sys.stdout = old_stdout
        output = buf.getvalue()
        self.assertNotIn("FAKE_SECRET_DO_NOT_USE", output)
        self.assertIn(REDACTED, output)

    def test_sensitive_value_never_in_explain_output(self) -> None:
        """Secret values must not appear in explain output."""
        buf = StringIO()
        err_buf = StringIO()
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = buf, err_buf
        try:
            main([str(FIXTURE_DIR), "explain", "adminPassword"])
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
        output = buf.getvalue()
        err = err_buf.getvalue()
        self.assertNotIn("FAKE_SECRET_DO_NOT_USE", output)
        self.assertNotIn("FAKE_SECRET_DO_NOT_USE", err)
        self.assertIn(REDACTED, output)


class TestCLISetStringFlag(unittest.TestCase):
    """Tests for --set-string CLI flag."""

    def test_set_string_preserves_string(self) -> None:
        buf = StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            main(
                [
                    str(FIXTURE_DIR),
                    "--set-string",
                    "replicaCount=42",
                    "-o",
                    "json",
                    "--no-redact",
                ]
            )
        finally:
            sys.stdout = old_stdout
        doc = json.loads(buf.getvalue())
        values_by_key = {row["key"]: row["value"] for row in doc["values"]}
        self.assertEqual(values_by_key["replicaCount"], "42")


if __name__ == "__main__":
    unittest.main()
