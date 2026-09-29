"""Independent CLI, redaction, validation and metadata boundary regressions."""

import contextlib
import io
import json
import os
import subprocess
import sys

import pytest
import yaml

from helm_valuetrace.analysis.schema import load_schema, validate_values
from helm_valuetrace.cli import main
from helm_valuetrace.exceptions import ValueTraceError


@pytest.fixture
def chart(tmp_path):
    (tmp_path / "Chart.yaml").write_text("apiVersion: v2\nname: audit\nversion: 0.1.0\n")
    (tmp_path / "values.yaml").write_text("a: 1\n")
    return tmp_path


def cli(chart, *args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main([str(chart), *args])
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("fmt", ["table", "json", "yaml"])
@pytest.mark.parametrize("explain", [False, True])
def test_literal_bracket_sensitive_key_is_redacted(chart, fmt, explain):
    (chart / "values.yaml").write_text("'label[password': FAKE_BRACKET_SECRET\n")
    args = ["--explain", r"label\[password"] if explain else []
    code, out, err = cli(chart, *args, "-o", fmt)
    assert code == 0, err
    assert "FAKE_BRACKET_SECRET" not in out + err
    assert "<redacted>" in out


@pytest.mark.parametrize("fmt", ["table", "json", "yaml"])
@pytest.mark.parametrize("explain", [False, True])
def test_exact_escaped_redaction_paths(chart, fmt, explain):
    (chart / "values.yaml").write_text("'a.b': FAKE_LITERAL_SECRET\na:\n  b: ordinary\n")
    args = ["--explain", r"a\.b"] if explain else []
    code, out, err = cli(chart, *args, "-o", fmt, "--redact-path", r"a\.b")
    assert code == 0, err
    assert "FAKE_LITERAL_SECRET" not in out + err
    assert "<redacted>" in out


@pytest.mark.parametrize("fmt", ["table", "json", "yaml"])
@pytest.mark.parametrize("explain", [False, True])
def test_literal_dotted_sensitive_names_and_history(chart, fmt, explain):
    (chart / "values.yaml").write_text("'password.value': FAKE_LITERAL_PASSWORD\n")
    args = ["--explain", r"password\.value"] if explain else []
    code, out, err = cli(chart, *args, "-o", fmt, "--set", r"password\.value=replaced")
    assert code == 0, err
    assert "FAKE_LITERAL_PASSWORD" not in out + err
    assert "replaced" not in out


@pytest.mark.parametrize("extra", [[], ["--explain", "a"], ["--template-analysis"]])
def test_terminal_controls_are_escaped(chart, extra):
    (chart / "values.yaml").write_text(yaml.safe_dump({"a": "hello\x1b[2J\rFORGED\u202e"}))
    code, out, err = cli(chart, *extra)
    assert code == 0, err
    assert "\x1b" not in out + err
    assert "\r" not in out + err
    assert "\u202e" not in out + err


@pytest.mark.parametrize("fmt", ["json", "yaml"])
def test_structured_controls_roundtrip_safely(chart, fmt):
    value = "\x1b[2J\u202e"
    (chart / "values.yaml").write_text(yaml.safe_dump({"a": value}))
    code, out, err = cli(chart, "-o", fmt)
    assert code == 0, err
    assert "\x1b" not in out and "\u202e" not in out
    doc = json.loads(out) if fmt == "json" else yaml.safe_load(out)
    assert doc["values"][0]["value"] == value


def test_explain_never_assigned_child_is_missing(chart):
    code, _, err = cli(chart, "--explain", "a.never_existed")
    assert code == 1
    assert "not found" in err.lower()


def test_explain_empty_option_is_usage_error(chart):
    assert cli(chart, "--explain", "")[0] == 1


def test_flags_between_explain_and_key(chart):
    code, out, err = cli(chart, "explain", "--set", "a=2", "a", "-o", "json")
    assert code == 0, err
    assert json.loads(out)["final_value"] == 2


def test_missing_schema_cannot_satisfy_strict_schema(chart):
    code, _, err = cli(chart, "--strict-schema")
    assert code == 1
    assert "schema" in err


@pytest.mark.parametrize(
    "data", [b"\xff", b'{"type": NaN}', b'{"type": "object", "type": "integer"}']
)
def test_schema_bad_bytes_fail_safely(chart, data):
    (chart / "values.schema.json").write_bytes(data)
    with pytest.raises(ValueTraceError):
        load_schema(chart)


def test_unknown_schema_dialect_is_not_silently_accepted():
    with pytest.raises(ValueTraceError, match="dialect"):
        validate_values({}, {"$schema": "https://example.invalid/unknown-schema"})


def test_schema_overflow_is_rejected_at_load_boundary(chart):
    (chart / "values.schema.json").write_text('{"minimum":1e999}')
    with pytest.raises(ValueTraceError):
        load_schema(chart)


def test_schema_regex_evaluation_is_bounded(chart):
    code = """
from helm_valuetrace.analysis.schema import validate_values
from helm_valuetrace.exceptions import ValueTraceError
try:
    validate_values({'a': 'x' * 32 + '!'}, {'properties': {'a': {'pattern': '^(x+)+$'}}})
except ValueTraceError as exc:
    assert 'limit' in str(exc) or 'timed out' in str(exc)
else:
    raise AssertionError('expected resource limit error')
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=12,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("fmt", ["table", "json", "yaml"])
@pytest.mark.parametrize("explain", [False, True])
def test_schema_and_denial_policies_apply_in_all_modes(chart, fmt, explain):
    (chart / "values.schema.json").write_text(json.dumps({"properties": {"a": {"type": "string"}}}))
    layer = chart / "denied.yaml"
    layer.write_text("a: 2\n")
    args = ["--explain", "a"] if explain else []
    assert cli(chart, *args, "-o", fmt, "--strict-schema")[0] == 2
    assert cli(chart, *args, "-o", fmt, "-f", str(layer), "--deny-source", "*denied*")[0] == 2


@pytest.mark.parametrize("filename", ["Chart.yaml", "values.schema.json"])
def test_invalid_metadata_encoding_is_input_error(chart, filename):
    (chart / filename).write_bytes(b"\xff")
    code, _, err = cli(chart)
    assert code == (3 if filename == "Chart.yaml" else 1), err


def test_disabled_subchart_defaults_are_omitted(chart):
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: audit\nversion: 0.1.0\n"
        "dependencies:\n- name: child\n  condition: child.enabled\n"
    )
    child = chart / "charts/child"
    child.mkdir(parents=True)
    (child / "Chart.yaml").write_text("apiVersion: v2\nname: child\nversion: 0.1.0\n")
    (child / "values.yaml").write_text("unexpected: 42\n")
    (chart / "values.yaml").write_text("child:\n  enabled: false\n")
    code, out, err = cli(chart)
    assert code == 0, err
    assert "unexpected" not in out


def test_experimental_flag_does_not_remerge_invalid_subchart_values(chart):
    child = chart / "charts/child"
    child.mkdir(parents=True)
    (child / "Chart.yaml").write_text("apiVersion: v2\nname: child\nversion: 0.1.0\n")
    (child / "values.yaml").write_text("a: 2\n")
    (chart / "values.yaml").write_text("child: disabled\n")
    first = cli(chart)[0]
    assert cli(chart, "--experimental-subcharts")[0] == first


def test_restored_global_source_propagates_to_child(chart):
    child = chart / "charts/child"
    child.mkdir(parents=True)
    (child / "Chart.yaml").write_text("apiVersion: v2\nname: child\nversion: 0.1.0\n")
    (child / "values.yaml").write_text("global:\n  region: child-default\n")
    (chart / "values.yaml").write_text("global:\n  region: root-default\n")
    args = []
    for index, text in enumerate(
        ["global:\n  region: earlier\n", "global: disabled\n", "global: {}\n"]
    ):
        layer = chart / f"layer{index}.yaml"
        layer.write_text(text)
        args.extend(["-f", str(layer)])
    code, out, err = cli(chart, *args, "--explain", "child.global.region", "-o", "json")
    assert code == 0, err
    doc = json.loads(out)
    assert doc["final_value"] == "root-default"
    assert doc["final_source"] == str(chart / "values.yaml") + ":2"


def test_child_schema_is_not_silently_ignored(chart):
    child = chart / "charts/child"
    child.mkdir(parents=True)
    (child / "Chart.yaml").write_text("apiVersion: v2\nname: child\nversion: 0.1.0\n")
    (child / "values.yaml").write_text("a: FAKE_VALUE\n")
    (child / "values.schema.json").write_text('{"properties":{"a":{"type":"integer"}}}')
    (chart / "values.schema.json").write_text("{}")
    code, out, err = cli(chart, "--strict-schema", "-o", "json")
    assert code == 2, err
    assert json.loads(out)["schema_errors"][0]["key"] == "child.a"


def test_packaged_dependency_is_not_silently_ignored(chart):
    import tarfile

    child = chart / "child"
    child.mkdir()
    (child / "Chart.yaml").write_text("apiVersion: v2\nname: child\nversion: 0.1.0\n")
    (child / "values.yaml").write_text("present: 1\n")
    (chart / "charts").mkdir()
    with tarfile.open(chart / "charts/child-0.1.0.tgz", "w:gz") as archive:
        archive.add(child, arcname="child")
    code, _, err = cli(chart)
    assert code == 3
    assert "unsupported" in err.lower()


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://example.invalid/never-fetch"},
        {"$ref": "file:///synthetic-never-read"},
        {"$ref": "#/definitions/missing"},
        {"$ref": "#"},
    ],
)
def test_invalid_recursive_external_refs_fail_without_value_disclosure(schema):
    with pytest.raises(ValueTraceError) as exc:
        validate_values({"password": "FAKE_REFERENCE_SECRET"}, schema)
    assert "FAKE_REFERENCE_SECRET" not in str(exc.value)


def test_local_refs_and_2020_dialect_use_library_semantics():
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {"entry": {"type": "integer"}},
        "properties": {"list": {"type": "array", "items": {"$ref": "#/$defs/entry"}}},
        "unevaluatedProperties": False,
    }
    assert validate_values({"list": [1, 2]}, schema) == []
    assert validate_values({"list": ["FAKE_WRONG_TYPE"]}, schema)[0].key == "list.0"
    assert validate_values({"extra": 1}, schema)


def test_schema_file_depth_size_and_error_budgets(chart, monkeypatch):
    import helm_valuetrace.analysis.schema as schema_module

    path = chart / "values.schema.json"
    monkeypatch.setattr(schema_module, "MAX_SCHEMA_BYTES", 128)
    path.write_text('{"description":"' + "x" * 129 + '"}')
    with pytest.raises(ValueTraceError):
        load_schema(chart)
    monkeypatch.setattr(schema_module, "MAX_SCHEMA_BYTES", 10000)
    path.write_text('{"a":' * 70 + "{}" + "}" * 70)
    with pytest.raises(ValueTraceError):
        load_schema(chart)
    with pytest.raises(ValueTraceError, match="limit"):
        validate_values(
            {str(i): "bad" for i in range(1002)}, {"additionalProperties": {"type": "integer"}}
        )


def test_deterministic_redaction_does_not_mutate_or_disclose_history(chart):
    import copy
    import random

    from helm_valuetrace.analysis.secrets import redact_value

    rng = random.Random(20260928)
    for index in range(200):
        fake = "FAKE_GENERATED_" + str(index)
        key = rng.choice(["password", "apiKey.url", "clientSecret", "credentials[raw", "token"])
        tree = {"items": [[{key: fake, "ordinary": index}]], "nested": {key: fake}}
        protected = copy.deepcopy(tree)
        safe = redact_value(tree, ())
        assert fake not in json.dumps(safe)
        assert safe["items"][0][0]["ordinary"] == index
        assert tree == protected


def test_configured_helm_failure_does_not_silently_change_null_semantics(chart, monkeypatch):
    fake = chart / "fake-helm"
    fake.write_text("#!/bin/sh\nexit 19\n")
    fake.chmod(0o755)
    monkeypatch.setenv("HELM_BIN", str(fake))
    code, out, err = cli(chart, "-o", "json")
    assert code == 1
    assert not out
    assert "Helm version" in err
