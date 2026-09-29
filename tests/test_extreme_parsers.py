"""Bounded adversarial regressions and deterministic parser differential checks."""

import json
import os
import random
import shutil
import subprocess

import pytest
import yaml

from helm_valuetrace.core import trace_values
from helm_valuetrace.exceptions import ValueTraceError
from helm_valuetrace.parsers.set_parser import (
    parse_all_set_arguments,
    parse_set,
    parse_set_json,
    parse_set_literal,
    parse_set_string,
)
from helm_valuetrace.parsers.yaml_loader import load_values_file


@pytest.fixture
def dump_chart(tmp_path):
    chart = tmp_path / "chart with spaces 雪"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: audit\nversion: 0.1.0\n")
    (chart / "values.yaml").write_text("{}\n")
    (chart / "templates").mkdir()
    (chart / "templates/dump.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: dump\ndata:\n"
        "  values: {{ .Values | toJson | quote }}\n"
    )
    return chart


def helm_values(chart, args):
    helm = os.environ.get("HELM_BIN") or shutil.which("helm")
    if not helm:
        pytest.skip("real Helm required")
    proc = subprocess.run(
        [helm, "template", "audit", str(chart), *args],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc, json.loads(
        yaml.safe_load(proc.stdout)["data"]["values"]
    ) if not proc.returncode else None


@pytest.mark.parametrize(
    "text",
    [
        "a: &a\n  b: *a\n",
        "a: &a [*a]\n",
        "a: " + "[" * 1000 + "0" + "]" * 1000,
        "a: .inf\n",
        "a: .nan\n",
        "1: one\ntrue: yes\n",
        "outer:\n  1: unsupported\n",
        "a: !!set {x: null}\n",
        "a: !unsupported FAKE_TAG_SECRET\n",
        "a: [FAKE_UNCLOSED_SECRET\n",
    ],
)
def test_unsupported_yaml_fails_without_raw_exception_or_value(tmp_path, text):
    path = tmp_path / "values.yaml"
    path.write_text(text)
    with pytest.raises(ValueTraceError) as error:
        load_values_file(path)
    assert "FAKE_" not in str(error.value)


def test_multi_doc_yaml_merges_all_documents(tmp_path):
    """Multi-document YAML files merge all documents; later keys override earlier ones.

    This matches Helm's behaviour: ``helm template -f multi-doc.yaml`` reads
    all documents and the last one wins for duplicate keys.
    """
    path = tmp_path / "values.yaml"
    path.write_text("a: 1\n---\nb: 2\n")
    values, _lines, _ = load_values_file(path)
    assert values == {"a": 1, "b": 2}

    path.write_text("a: 1\n---\na: 2\n")
    values, _, _ = load_values_file(path)
    assert values == {"a": 2}

    path.write_text("a: 1\n---\n---\nb: 3\n")
    values, _, _ = load_values_file(path)
    assert values == {"a": 1, "b": 3}


def test_yaml_alias_merge_has_real_source_location(tmp_path):
    path = tmp_path / "values.yaml"
    path.write_text("base: &base\n  child: yes\nnext:\n  <<: *base\n")
    values, lines, _ = load_values_file(path)
    assert values["next"] == {"child": True}
    assert lines[("next", "child")] == 2
    assert ("next", "<<", "child") not in lines


@pytest.mark.parametrize(
    "scalar",
    [
        "2020-01-01",
        "!!binary aGVsbG8=",
        "1e3",
        "1:20",
        "0o12",
        "08",
        "1_000",
        "-.5",
        "!!str 1e3",
        "y",
        "n",
        "Y",
        "N",
        "1e999",
        "1_0e1_0",
        "!!binary /w==",
    ],
)
def test_yaml_scalar_differential(dump_chart, tmp_path, scalar):
    path = tmp_path / "values 雪.yaml"
    path.write_text(f"a: {scalar}\n")
    proc, expected = helm_values(dump_chart, ["-f", str(path)])
    assert proc.returncode == 0, proc.stderr
    assert load_values_file(path)[0] == expected


@pytest.mark.parametrize(
    ("flag", "expression"),
    [
        ("set", "a=" + "9" * 5000),
        ("set", "a=x\\"),
        ("set", "a[+1]=x"),
        ("set", "a[-0]=x"),
        ("set", "a[²]=x"),
        ("set", "a[\u0661]=x"),
        ("set", "a[" + "9" * 5000 + "]=x"),
        ("set-literal", r"a\=b=x"),
        ("set-literal", r"a\.b=x"),
        ("set-literal", r"a\,b=x"),
        ("set-json", 'a="\\ud800"'),
        ("set-json", "a=9007199254740993"),
        ("set-json", "a=9223372036854775807"),
        ("set-json", "a=18446744073709551615"),
        ("set-json", "a=1e999"),
        ("set-json", 'a={"x":1e999}'),
        ("set-json", "a=1, b=2"),
    ],
)
def test_strvals_adversarial_differential(dump_chart, flag, expression):
    proc, expected = helm_values(dump_chart, [f"--{flag}", expression])
    kwargs = {flag.replace("-", "_") + "_args": [expression]}
    if proc.returncode:
        with pytest.raises(ValueTraceError):
            parse_all_set_arguments(**kwargs)
    else:
        entries = parse_all_set_arguments(**kwargs)
        result = trace_values({}, "defaults", {}, [], extra_set_entries=entries)
        assert result.values == expected


def test_yaml_alias_expansion_is_bounded(tmp_path):
    lines = ["a0: &a0 [0, 1]"]
    lines += [f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}]" for i in range(1, 20)]
    path = tmp_path / "alias-expansion.yaml"
    path.write_text("\n".join(lines))
    with pytest.raises(ValueTraceError, match=r"limit|large"):
        load_values_file(path)


def test_invalid_utf8_is_sanitized(tmp_path):
    path = tmp_path / "values.yaml"
    path.write_bytes(b"password: FAKE_UTF8_SECRET\xff")
    with pytest.raises(ValueTraceError, match="UTF-8") as error:
        load_values_file(path)
    assert "FAKE_" not in str(error.value)


def test_deterministic_malformed_strvals_never_raise_unexpected_exceptions():
    rng = random.Random(271828)
    alphabet = "a019²\u0661.,={}[]\\ +-\n"
    for _ in range(500):
        raw = "".join(rng.choice(alphabet) for _ in range(rng.randrange(50)))
        for parser in (parse_set, parse_set_json, parse_set_literal, parse_set_string):
            try:
                first = parser([raw])
                second = parser([raw])
            except ValueTraceError:
                continue
            assert [(x.path, x.value) for x in first] == [(x.path, x.value) for x in second]


def test_yaml_deterministic_json_roundtrip_and_input_locations(tmp_path):
    rng = random.Random(314159)
    for _ in range(40):
        values = {
            f"key{i}": rng.choice([None, True, 0, "雪", [1, "x"], {"nested": 2}]) for i in range(20)
        }
        path = tmp_path / "roundtrip.yaml"
        path.write_text(yaml.safe_dump(values))
        actual, lines, _ = load_values_file(path)
        assert actual == values
        assert json.loads(json.dumps(actual)) == values
        assert all(line > 0 for line in lines.values())


@pytest.mark.parametrize("text", ["", "null\n", "# comment only\n"])
def test_empty_yaml_documents_are_empty_mappings(tmp_path, text):
    path = tmp_path / "empty.yaml"
    path.write_text(text)
    assert load_values_file(path)[0] == {}


def test_duplicate_keys_follow_last_assignment_with_last_location(tmp_path):
    path = tmp_path / "duplicates.yaml"
    path.write_text("a: first\na: second\n")
    values, lines, _ = load_values_file(path)
    assert values == {"a": "second"}
    assert lines[("a",)] == 2


def test_yaml_mapping_merge_alias_precedence_matches_helm(dump_chart, tmp_path):
    path = tmp_path / "merge.yaml"
    path.write_text(
        "one: &one {a: 1, b: 2}\ntwo: &two {a: 3, c: 4}\nresult:\n  <<: [*one, *two]\n  b: 5\n"
    )
    proc, expected = helm_values(dump_chart, ["-f", str(path)])
    assert proc.returncode == 0, proc.stderr
    values, lines, _ = load_values_file(path)
    assert values == expected
    assert lines[("result", "a")] == 1
    assert lines[("result", "b")] == 5
    assert lines[("result", "c")] == 2


def test_yaml_list_element_locations_are_available(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("a:\n  - first\n  - second\n")
    _, lines, _ = load_values_file(path)
    assert lines[("a", 0)] == 2
    assert lines[("a", 1)] == 3


def test_yaml_and_set_file_enforce_bounded_reads(tmp_path):
    path = tmp_path / "oversized.yaml"
    path.write_bytes(b"a: " + b"x" * (8 * 1024 * 1024))
    with pytest.raises(ValueTraceError, match="size limit"):
        load_values_file(path)
    with pytest.raises(ValueTraceError, match="size limit"):
        parse_all_set_arguments(set_file_args=[f"a={path}"])


def test_set_json_enforces_depth_and_replaces_surrogates_in_keys():
    with pytest.raises(ValueTraceError):
        parse_set_json(["a=" + "[" * 70 + "0" + "]" * 70])
    assert parse_set_json(['a={"\\ud800":"\\udc00"}'])[0].value == {"\ufffd": "\ufffd"}


def test_yaml_large_integer_fails_explicitly_instead_of_silently_disagreeing(tmp_path):
    path = tmp_path / "large.yaml"
    path.write_text("a: 9007199254740993\n")
    with pytest.raises(ValueTraceError, match="unsupported"):
        load_values_file(path)


@pytest.mark.parametrize("flag", ["set", "set-string", "set-json", "set-literal"])
def test_seeded_supported_strvals_differential(dump_chart, flag):
    rng = random.Random(161803)
    for _ in range(12):
        key = rng.choice(["plain", "a.b", "a[2]", "a[+1].b", r"a\.b", r"a\=b"])
        if flag == "set-json":
            value = json.dumps(rng.choice([False, None, [1, {"a": "雪"}], {"a": [0, "x"]}, 1.25]))
        else:
            value = rng.choice(["true", "010", "", r"x\,y", "{a,b}", "x\\", "a=b"])
        expression = key + "=" + value
        proc, expected = helm_values(dump_chart, ["--" + flag, expression])
        kwargs = {flag.replace("-", "_") + "_args": [expression]}
        if proc.returncode:
            with pytest.raises(ValueTraceError):
                parse_all_set_arguments(**kwargs)
        else:
            entries = parse_all_set_arguments(**kwargs)
            actual = trace_values({}, "defaults", {}, [], extra_set_entries=entries).values
            assert actual == expected, (flag, expression)
