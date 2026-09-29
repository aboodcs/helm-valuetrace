"""Release regressions: real Helm is the oracle for effective values."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from helm_valuetrace.cli import main
from helm_valuetrace.core import final_assignment, trace_values


class ReleaseRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.chart = Path(self.tmp.name)
        (self.chart / "Chart.yaml").write_text("apiVersion: v2\nname: regression\nversion: 0.1.0\n")
        (self.chart / "templates").mkdir()
        (self.chart / "templates/dump.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: dump\ndata:\n"
            "  values: {{ .Values | toJson | quote }}\n"
        )

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([str(self.chart), *args])
        return code, out.getvalue(), err.getvalue()

    def test_restored_default_source_after_parent_replacement(self):
        defaults = {"service": {"port": 80, "type": "ClusterIP"}}
        layers = [
            {"service": {"type": "LoadBalancer"}},
            {"service": "disabled"},
            {"service": {"port": 8080}},
        ]
        (self.chart / "values.yaml").write_text(yaml.safe_dump(defaults))
        args = []
        for i, layer in enumerate(layers):
            path = self.chart / f"{i}.yaml"
            path.write_text(yaml.safe_dump(layer))
            args.extend(["-f", str(path)])
        helm = os.environ.get("HELM_BIN") or shutil.which("helm")
        if not helm:
            self.skipTest("real Helm required")
        proc = subprocess.run(
            [helm, "template", "test", str(self.chart), *args], capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        expected = json.loads(yaml.safe_load(proc.stdout)["data"]["values"])
        result = trace_values(
            defaults, "defaults", {}, [(v, str(i), {}) for i, v in enumerate(layers)]
        )
        self.assertEqual(result.values, expected)
        self.assertEqual(expected["service"]["type"], "ClusterIP")
        self.assertEqual(final_assignment(("service", "type"), result.history).source, "defaults")

    def test_parent_scalar_wins_over_historical_children(self):
        result = trace_values(
            {"a": {"b": 1}},
            "defaults",
            {},
            [({"a": {"b": 2}}, "first", {}), ({"a": 3}, "last", {})],
        )
        self.assertEqual(final_assignment(("a",), result.history).source, "last")

    def test_recursive_redaction_in_reports_and_explain(self):
        (self.chart / "values.yaml").write_text(
            "users:\n- password: FAKE_ARRAY_SECRET\ncredentials:\n  nested: FAKE_PARENT_SECRET\n"
        )
        for fmt in ("table", "json", "yaml"):
            for extra in ([], ["explain", "users"]):
                with self.subTest(fmt=fmt, extra=extra):
                    code, out, err = self.cli(*extra, "-o", fmt)
                    self.assertEqual(code, 0, err)
                    self.assertNotIn("FAKE_ARRAY_SECRET", out + err)
                    self.assertNotIn("FAKE_PARENT_SECRET", out + err)

    def test_schema_error_does_not_echo_value(self):
        (self.chart / "values.yaml").write_text("password: FAKE_SCHEMA_SECRET\n")
        (self.chart / "values.schema.json").write_text(
            json.dumps({"type": "object", "properties": {"password": {"type": "integer"}}})
        )
        code, out, err = self.cli("-o", "json", "--strict-schema")
        self.assertEqual(code, 2)
        self.assertNotIn("FAKE_SCHEMA_SECRET", out + err)

    def test_yaml_error_does_not_echo_input(self):
        (self.chart / "values.yaml").write_text("password: [FAKE_YAML_SECRET\n")
        code, out, err = self.cli()
        self.assertEqual(code, 1)
        self.assertNotIn("FAKE_YAML_SECRET", out + err)

    def test_parser_edges_against_helm(self):
        from helm_valuetrace.parsers.set_parser import parse_all_set_arguments

        helm = os.environ.get("HELM_BIN") or shutil.which("helm")
        if not helm:
            self.skipTest("real Helm required")
        (self.chart / "values.yaml").write_text("{}\n")
        cases = [
            ("--set", "a={1,true,null}"),
            ("--set", "a={}"),
            ("--set-string", "a={1,true}"),
            ("--set", "a= hello "),
            ("--set-json", 'a="hello,world",b={"x":"[a,}b"}'),
            ("--set-json", "a="),
            ("--set", "a=1,a.b=2"),
            ("--set", "a[65537]=x"),
            ("--set", "a={missing"),
        ]
        names = {
            "--set": "set_args",
            "--set-string": "set_string_args",
            "--set-json": "set_json_args",
        }
        for flag, raw in cases:
            with self.subTest(flag=flag, raw=raw):
                proc = subprocess.run(
                    [helm, "template", "test", str(self.chart), flag, raw],
                    capture_output=True,
                    text=True,
                )
                if proc.returncode:
                    from helm_valuetrace.exceptions import ValueTraceError

                    with self.assertRaises(ValueTraceError):
                        entries = parse_all_set_arguments(**{names[flag]: [raw]})
                        trace_values({}, "defaults", {}, [], extra_set_entries=entries)
                else:
                    expected = json.loads(yaml.safe_load(proc.stdout)["data"]["values"])
                    entries = parse_all_set_arguments(**{names[flag]: [raw]})
                    actual = trace_values({}, "defaults", {}, [], extra_set_entries=entries).values
                    self.assertEqual(actual, expected)

    def test_explain_actual_container_and_deleted_key(self):
        (self.chart / "values.yaml").write_text("image:\n  tag: stable\n  repository: nginx\n")
        code, out, err = self.cli("--set", "image.tag=new", "--explain", "image", "-o", "json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["final_value"], {"tag": "new", "repository": "nginx"})
        code, out, err = self.cli("--set", "image.tag=null", "--explain", "image.tag", "-o", "json")
        self.assertEqual(code, 0, err)
        self.assertFalse(json.loads(out)["present"])

    def test_explain_does_not_bypass_policies(self):
        (self.chart / "values.yaml").write_text("a: 1\n")
        override = self.chart / "denied.yaml"
        override.write_text("a: 2\n")
        code, _, _ = self.cli("explain", "a", "-f", str(override), "--deny-source", "*denied*")
        self.assertEqual(code, 2)

    def test_redaction_path_and_opt_in(self):
        (self.chart / "values.yaml").write_text("config:\n  payload: FAKE_CUSTOM_SECRET\n")
        code, out, err = self.cli("--redact-path", "config.payload", "-o", "json")
        self.assertEqual(code, 0, err)
        self.assertNotIn("FAKE_CUSTOM_SECRET", out)
        code, out, err = self.cli("--redact-path", "config.payload", "--no-redact", "-o", "json")
        self.assertEqual(code, 0, err)
        self.assertIn("FAKE_CUSTOM_SECRET", out)

    def test_schema_custom_properties_are_not_unknown(self):
        (self.chart / "values.yaml").write_text("{}\n")
        (self.chart / "values.schema.json").write_text(
            '{"type":"object","additionalProperties":true}'
        )
        code, out, err = self.cli("--set", "custom=1", "--strict-unknown", "-o", "json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["unknown"], [])

    def test_escaped_equals_and_invalid_paths(self):
        from helm_valuetrace.exceptions import ValueTraceError
        from helm_valuetrace.parsers.set_parser import parse_set

        result = trace_values({}, "defaults", {}, [], extra_set_entries=parse_set([r"a\=b=x"]))
        self.assertEqual(result.values, {"a=b": "x"})
        for expression in ("a[0]b=x", "a..b=x", "a.=x"):
            with self.subTest(expression=expression), self.assertRaises(ValueTraceError):
                parse_set([expression])

    def test_schema_definition_and_external_ref_fail_safely(self):
        (self.chart / "values.yaml").write_text("a: FAKE_SCHEMA_INPUT\n")
        for schema in ({"type": "not-a-type"}, {"$ref": "https://invalid.example/schema.json"}):
            (self.chart / "values.schema.json").write_text(json.dumps(schema))
            code, out, err = self.cli("--strict-schema")
            self.assertEqual(code, 1, err)
            self.assertNotIn("FAKE_SCHEMA_INPUT", out + err)

    def test_history_of_secret_child_is_redacted_after_parent_replacement(self):
        (self.chart / "values.yaml").write_text("config:\n  password: FAKE_HISTORY_SECRET\n")
        for fmt in ("table", "json", "yaml"):
            code, out, err = self.cli("--set", "config=disabled", "-o", fmt)
            self.assertEqual(code, 0, err)
            self.assertNotIn("FAKE_HISTORY_SECRET", out + err)


def test_internal_error_returns_exit_4(tmp_path):
    """Exit code 4 is the documented code for unexpected internal errors.
    Previously the audit report incorrectly claimed exit 4 was 'verified'
    without actually triggering it. This regression test proves the path exists.
    """
    import importlib
    import io
    import sys
    from contextlib import redirect_stderr, redirect_stdout
    from unittest.mock import patch

    import helm_valuetrace.cli as cli_mod

    chart = tmp_path / "chart"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: t\nversion: 0.0.1\n")
    (chart / "values.yaml").write_text("a: 1\n")

    def boom(args):
        raise RuntimeError("SYNTHETIC_INTERNAL_FAULT")

    importlib.reload(cli_mod)
    sys.argv = ["valuetrace", str(chart)]
    buf = io.StringIO()
    with patch.object(cli_mod, "run", boom), redirect_stderr(buf), redirect_stdout(buf):
        code = cli_mod.main()

    assert code == 4
    assert "Internal error" in buf.getvalue()
    assert "SYNTHETIC_INTERNAL_FAULT" not in buf.getvalue()


def test_strict_unknown_with_unrelated_schema_error_does_not_falsely_report_allowed_keys(tmp_path):
    """MEGA-F-001: schema-structure-based unknown suppression.

    If schema has additionalProperties:true but fails on an unrelated constraint
    (e.g. replicaCount violates minimum), heuristic unknown detection must NOT
    flag keys that the schema explicitly allows as unknown.

    The previous fix used error-count based suppression: if schema had ANY errors,
    unknowns were kept. This was wrong: an unrelated schema failure should not
    un-suppress allowed extra keys.
    """
    import importlib
    import io
    import json
    import sys
    from contextlib import redirect_stderr, redirect_stdout

    import helm_valuetrace.cli as cli_mod

    chart = tmp_path / "chart"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: t\nversion: 0.0.1\n")
    (chart / "values.yaml").write_text("replicaCount: 1\n")
    (chart / "values.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": True,
                "properties": {"replicaCount": {"type": "integer", "minimum": 0}},
            }
        )
    )

    def run(extra_args):
        importlib.reload(cli_mod)
        sys.argv = ["valuetrace", str(chart)] + extra_args
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            code = cli_mod.main()
        return code, buf.getvalue()

    code, out = run(["--set", "extra=1", "--strict-unknown"])
    assert code == 0, f"Case A expected exit 0, got {code}: {out}"
    assert "0 unknown keys" in out

    code, out = run(["--set", "replicaCount=-1", "--set", "extra=1", "--strict-unknown"])
    assert code == 0, f"Case B expected exit 0 (extra is allowed by schema), got {code}: {out}"
    assert "0 unknown keys" in out
    assert "minimum" in out.lower() or "schema" in out.lower()

    import json as _json

    (chart / "values.schema.json").write_text(
        _json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"replicaCount": {"type": "integer", "minimum": 0}},
            }
        )
    )
    code, out = run(["--set", "replicaCount=-1", "--set", "extra=1", "--strict-unknown"])
    assert code == 2, f"Case C expected exit 2 (extra not allowed), got {code}: {out}"


def test_redaction_camelcase_key_patterns_no_false_positives_or_negatives():
    """MEGA-F-002: Specific camelCase/snake_case key pattern coverage.

    Previous fix: removed *key* catch-all, added specific patterns.
    This session: added gpgKey, rsaKey, jwtKey, hmacKey camelCase variants;
    added 'coauthor' to safe words (false positive from *auth*); added 'keys'.
    """
    from helm_valuetrace.analysis.secrets import DEFAULT_SENSITIVE_PATTERNS, is_sensitive

    patterns = DEFAULT_SENSITIVE_PATTERNS

    safe = [
        "key",
        "keys",
        "keyboard",
        "keymap",
        "keyspace",
        "keyName",
        "keyFormat",
        "keyType",
        "monkey",
        "turnkey",
        "hockey",
        "donkey",
        "turkey",
        "keyring",
        "keynote",
        "keyword",
        "keypad",
        "coauthor",
        "author",
        "authority",
        "oauth",
    ]
    for word in safe:
        assert not is_sensitive(word, patterns), f"{word!r} should NOT be sensitive"

    sensitive = [
        "apiKey",
        "apikey",
        "api_key",
        "API_KEY",
        "accessKey",
        "access_key",
        "secretKey",
        "secret_key",
        "privateKey",
        "private_key",
        "signingKey",
        "signing_key",
        "encryptionKey",
        "encryption_key",
        "sshKey",
        "ssh_key",
        "gpgKey",
        "gpg_key",
        "gpgkey",
        "pgpKey",
        "pgp_key",
        "pgpkey",
        "rsaKey",
        "rsa_key",
        "rsakey",
        "tlsKey",
        "tls_key",
        "tlskey",
        "jwtKey",
        "jwt_key",
        "jwtkey",
        "hmacKey",
        "hmac_key",
        "hmackey",
        "token",
        "password",
        "passwd",
        "secret",
        "credential",
        "credentials",
        "auth",
        "authToken",
        "authorization",
        "clientSecret",
        "databasePassword",
    ]
    for word in sensitive:
        assert is_sensitive(word, patterns), f"{word!r} SHOULD be sensitive"


def test_schema_nested_ap_false_keeps_unknown_despite_root_ap_true(tmp_path):
    """MEGA-F-001 v2: root additionalProperties:true does NOT suppress unknowns
    inside a nested object that has additionalProperties:false.

    service.bad must remain flagged even though root AP:true, because the
    'service' sub-schema explicitly has AP:false.  topExtra at the root IS
    suppressed (root allows it).
    """
    import importlib
    import io
    import json
    import sys
    from contextlib import redirect_stderr, redirect_stdout

    import helm_valuetrace.cli as cli_mod

    chart = tmp_path / "chart"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: t\nversion: 0.0.1\n")
    (chart / "values.yaml").write_text("service:\n  port: 80\n")
    (chart / "values.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "service": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"port": {"type": "integer"}},
                    }
                },
            }
        )
    )

    def run(extra_args):
        importlib.reload(cli_mod)
        sys.argv = ["vt", str(chart)] + extra_args
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            code = cli_mod.main()
        return code, buf.getvalue()

    code, out = run(["--set", "topExtra=1", "--strict-unknown"])
    assert code == 0, f"topExtra should be allowed by root AP:true, got exit={code}"
    assert "0 unknown keys" in out

    code, out = run(["--set", "service.bad=x", "--strict-unknown"])
    assert code == 2, f"service.bad should be unknown (service AP:false), got exit={code}"
    assert "service.bad" in out


def test_schema_pattern_properties_suppresses_matching_unknowns(tmp_path):
    """MEGA-F-001 v2: patternProperties patterns suppress heuristic unknowns
    for keys that match the pattern, while non-matching keys remain flagged.
    """
    import importlib
    import io
    import json
    import sys
    from contextlib import redirect_stderr, redirect_stdout

    import helm_valuetrace.cli as cli_mod

    chart = tmp_path / "chart"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: t\nversion: 0.0.1\n")
    (chart / "values.yaml").write_text("replicaCount: 1\n")
    (chart / "values.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "patternProperties": {"^svc-[0-9]+$": {"type": "object"}},
                "properties": {"replicaCount": {"type": "integer"}},
            }
        )
    )

    def run(extra_args):
        importlib.reload(cli_mod)
        sys.argv = ["vt", str(chart)] + extra_args
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            code = cli_mod.main()
        return code, buf.getvalue()

    code, out = run(["--set", "svc-123.port=80", "--strict-unknown"])
    assert code == 0, (
        f"svc-123 matches patternProperties, should not be unknown, got exit={code}: {out}"
    )

    code, out = run(["--set", "badKey=x", "--strict-unknown"])
    assert code == 2, f"badKey should be unknown (no pattern match), got exit={code}"
    assert "badKey" in out


def test_schema_root_ap_true_nested_ap_false_nested_key_flagged(tmp_path):
    """MEGA-F-001 v2: ingress sub-schema with AP:false keeps ingress.secret
    unknown even though root AP:true would (incorrectly in v1) suppress all.
    """
    import importlib
    import io
    import json
    import sys
    from contextlib import redirect_stderr, redirect_stdout

    import helm_valuetrace.cli as cli_mod

    chart = tmp_path / "chart"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: t\nversion: 0.0.1\n")
    (chart / "values.yaml").write_text("ingress:\n  enabled: false\n")
    (chart / "values.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "ingress": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"enabled": {"type": "boolean"}},
                    }
                },
            }
        )
    )

    def run(extra_args):
        importlib.reload(cli_mod)
        sys.argv = ["vt", str(chart)] + extra_args
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            code = cli_mod.main()
        return code, buf.getvalue()

    code, out = run(["--set", "ingress.secret=x", "--strict-unknown"])
    assert code == 2, f"ingress.secret should be unknown (ingress AP:false), got exit={code}"
    assert "ingress.secret" in out
