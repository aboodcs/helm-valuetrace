"""Independent, bounded merge/provenance regressions and Helm differential cases."""

import copy
import json
import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from helm_valuetrace.core import final_assignment, trace_values
from helm_valuetrace.exceptions import ValueTraceError
from helm_valuetrace.models import SubchartInfo


def test_empty_override_mapping_does_not_own_restored_defaults():
    result = trace_values({"a": {"x": 1}}, "defaults", {}, [({"a": {}}, "empty", {})])
    assert result.values == {"a": {"x": 1}}
    assert final_assignment(("a",), result.history).source == "defaults"


def test_deleted_assignment_is_not_container_value_source():
    result = trace_values(
        {"a": {"x": 1, "y": 2}}, "defaults", {}, [({"a": {"x": None}}, "delete", {})]
    )
    assert result.values == {"a": {"y": 2}}
    assert final_assignment(("a",), result.history).source == "defaults"
    assert result.history[("a", "x")][-1].source == "delete"


def test_container_contributors_include_all_surviving_sources():
    result = trace_values(
        {"a": {"x": 1, "y": 2}}, "defaults", {}, [({"a": {"x": 3}}, "override", {})]
    )
    assert final_assignment(("a",), result.history).value == {"x": 3, "y": 2}
    assert [a.source for a in result.history.contributors[("a",)]] == ["defaults", "override"]


def test_sparse_array_padding_is_attributed_without_fabricated_history():
    result = trace_values({}, "defaults", {}, [], set_arguments=["a[2]=x", "a[4]=y"])
    assert result.values == {"a": [None, None, "x", None, "y"]}
    for index, source in [(0, "--set[1]"), (1, "--set[1]"), (3, "--set[2]")]:
        winner = final_assignment(("a", index), result.history)
        assert winner is not None
        assert winner.source == source
        assert winner.value is None
        assert ("a", index) not in result.history
        assert result.history.generated[("a", index)].source == source


def test_replacing_array_drops_generated_padding_sources():
    result = trace_values({}, "defaults", {}, [], set_arguments=["a[2]=x", "a={first,second}"])
    assert result.values == {"a": ["first", "second"]}
    assert final_assignment(("a", 0), result.history).source == "--set[2]"
    assert ("a", 0) not in result.history.generated
    assert [a.source for a in result.history.contributors[("a",)]] == ["--set[2]"]


def test_array_element_mutation_has_multiple_contributors():
    result = trace_values(
        {},
        "defaults",
        {},
        [({"a": [{"x": 1, "y": 2}]}, "file", {})],
        set_arguments=["a[0].x=3"],
    )
    assert final_assignment(("a", 0, "x"), result.history).source == "--set[1]"
    assert final_assignment(("a", 0, "y"), result.history).source == "file"
    assert [a.source for a in result.history.contributors[("a", 0)]] == ["file", "--set[1]"]


def test_array_alias_override_does_not_mutate_sibling_or_input():
    layer = yaml.safe_load("a:\n  - &item {x: 1}\n  - *item\n")
    result = trace_values({}, "defaults", {}, [(layer, "file", {})], set_arguments=["a[0].x=2"])
    assert result.values == {"a": [{"x": 2}, {"x": 1}]}
    assert layer == {"a": [{"x": 1}, {"x": 1}]}
    assert final_assignment(("a", 1, "x"), result.history).source == "file"


def test_deletion_only_empty_container_retains_structural_origin():
    result = trace_values({"a": {"x": 1}}, "defaults", {}, [({"a": {"x": None}}, "delete", {})])
    assert result.values == {"a": {}}
    assert final_assignment(("a",), result.history).source == "delete"


def test_empty_mapping_followed_by_deletion_uses_latest_structural_source():
    result = trace_values(
        {"a": {"x": 1}},
        "defaults",
        {},
        [({"a": {}}, "empty", {}), ({"a": {"x": None}}, "delete", {})],
    )
    assert result.values == {"a": {}}
    assert final_assignment(("a",), result.history).source == "delete"


@pytest.mark.parametrize("expression", ["a[0]=1,a[0].b=2", "a[0]={x,y},a[0].b=2"])
def test_scalar_or_list_array_item_can_be_replaced_by_mapping(expression):
    result = trace_values({}, "defaults", {}, [], set_arguments=[expression])
    assert result.values == {"a": [{"b": 2}]}
    assert final_assignment(("a", 0, "b"), result.history).source == "--set[1]"


@pytest.mark.parametrize("expression", ["a=null,a.b=2", "a=null,a[0]=2"])
def test_explicit_null_mapping_key_rejects_descendant_assignment(expression):
    with pytest.raises(ValueTraceError, match="Incompatible container"):
        trace_values({}, "defaults", {}, [], set_arguments=[expression])


def test_padding_converted_to_container_is_no_longer_generated_null():
    result = trace_values({}, "defaults", {}, [], set_arguments=["a[2]=x", "a[0].b=2"])
    assert result.values == {"a": [{"b": 2}, None, "x"]}
    assert ("a", 0) not in result.history.generated
    assert final_assignment(("a", 0), result.history).source == "--set[2]"


@pytest.mark.parametrize("major", [3, 4])
def test_nested_user_null_survives_when_default_is_also_null(major):
    result = trace_values(
        {"a": {"b": None}},
        "defaults",
        {},
        [({"a": {"b": None}}, "user", {})],
        helm_major_version=major,
    )
    assert result.values == {"a": {"b": None}}
    assert final_assignment(("a", "b"), result.history).source == "user"


def test_related_events_index_retains_exact_write_order():
    result = trace_values(
        {"a": {"b": 0}, "unrelated": 1},
        "defaults",
        {},
        [({"a": 1}, "parent", {}), ({"a": {"b": 2}}, "child", {})],
    )
    for path in [("a",), ("a", "b"), ("unrelated",), ("missing",)]:
        expected = [
            (event_path, assignment)
            for event_path, assignment in result.history.events
            if event_path[: len(path)] == path or path[: len(event_path)] == event_path
        ]
        assert result.history.related_events(path) == expected


def _subchart():
    return SubchartInfo("child", "child", ("child",), Path("child"), {"own": 0}, {}, {}, "child")


def test_restored_global_default_propagates_its_actual_source():
    result = trace_values(
        {"global": {"x": 1, "y": 2}},
        "defaults",
        {},
        [
            ({"global": {"x": 3}}, "first", {}),
            ({"global": "stop"}, "replace", {}),
            ({"global": {"y": 4}}, "last", {}),
        ],
        subcharts=[_subchart()],
    )
    path = ("child", "global", "x")
    assert result.values["child"]["global"]["x"] == 1
    assert final_assignment(path, result.history).source == "defaults"
    origin = result.history.origins[path]
    assert any(assignment is origin for _, assignment in result.history.related_events(path))


def test_global_array_propagation_keeps_each_leaf_contributor():
    result = trace_values(
        {},
        "defaults",
        {},
        [({"global": {"items": ["first", "second"]}}, "file", {})],
        set_arguments=["global.items[1]=last"],
        subcharts=[_subchart()],
    )
    assert result.values["child"]["global"]["items"] == ["first", "last"]
    assert final_assignment(("child", "global", "items", 0), result.history).source == "file"
    assert final_assignment(("child", "global", "items", 1), result.history).source == "--set[1]"
    assert {a.source for a in result.history.contributors[("child", "global", "items")]} == {
        "file",
        "--set[1]",
    }


def test_global_array_replacement_removes_prepropagation_winners():
    child = _subchart()
    child.values = {"global": {"items": ["old", "removed"]}}
    result = trace_values({"global": {"items": ["root"]}}, "defaults", {}, [], subcharts=[child])
    assert result.values["child"]["global"]["items"] == ["root"]
    assert final_assignment(("child", "global", "items", 1), result.history) is None


def _walk(value, path=()):
    if path:
        yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, path + (index,))


def _layer(rng, source, depth=0):
    kind = rng.randrange(6)
    if kind <= 1 and depth < 3:
        return {
            k: _layer(rng, source, depth + 1) for k in rng.sample(["a", "b", "c"], rng.randrange(4))
        }
    if kind == 2 and depth < 3:
        return [_layer(rng, source, depth + 1) for _ in range(rng.randrange(4))]
    if kind == 3:
        return None
    return source + "::" + str(rng.randrange(100000))


def _generated_case(seed):
    rng = random.Random(seed)
    defaults = {k: _layer(rng, "defaults") for k in ["a", "b", "c"]}
    layers = [
        {k: _layer(rng, "layer" + str(i)) for k in rng.sample(["a", "b", "c"], rng.randrange(4))}
        for i in range(3)
    ]
    return defaults, layers


@pytest.mark.parametrize("seed", range(20260927, 20260991))
def test_deterministic_layering_provenance_against_real_helm(tmp_path, seed):
    helm = os.environ.get("HELM_BIN") or shutil.which("helm")
    if not helm:
        pytest.skip("real Helm executable unavailable")
    version = subprocess.run(
        [helm, "version", "--short"], capture_output=True, text=True, check=True, timeout=5
    ).stdout
    major = int(version.split(".")[0].lstrip("v"))
    defaults, layers = _generated_case(seed)
    original = copy.deepcopy((defaults, layers))
    (tmp_path / "Chart.yaml").write_text("apiVersion: v2\nname: audit\nversion: 0.1.0\n")
    (tmp_path / "values.yaml").write_text(yaml.safe_dump(defaults))
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates/dump.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: dump\ndata:\n"
        "  values: {{ .Values | toJson | quote }}\n"
    )
    args = []
    for index, layer in enumerate(layers):
        path = tmp_path / f"layer {index}.yaml"
        path.write_text(yaml.safe_dump(layer))
        args.extend(["-f", str(path)])
    proc = subprocess.run(
        [helm, "template", "audit", str(tmp_path), *args],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    expected = json.loads(yaml.safe_load(proc.stdout)["data"]["values"])
    inputs = [(layer, f"layer{index}", {}) for index, layer in enumerate(layers)]
    result = trace_values(defaults, "defaults", {}, inputs, helm_major_version=major)
    repeat = trace_values(defaults, "defaults", {}, inputs, helm_major_version=major)
    assert result.values == expected
    assert repeat.values == result.values
    assert repeat.history == result.history
    assert (defaults, layers) == original
    for path, value in _walk(result.values):
        if isinstance(value, str) and "::" in value:
            winner = final_assignment(path, result.history)
            assert winner is not None
            assert winner.source == value.split("::")[0]
            assert winner.value == value


def test_thousand_bounded_generated_merges_preserve_inputs_and_leaf_origins():
    for seed in range(20261000, 20262000):
        defaults, layers = _generated_case(seed)
        original = copy.deepcopy((defaults, layers))
        inputs = [(layer, f"layer{index}", {}) for index, layer in enumerate(layers)]
        result = trace_values(defaults, "defaults", {}, inputs, helm_major_version=4)
        repeat = trace_values(defaults, "defaults", {}, inputs, helm_major_version=4)
        assert result.values == repeat.values, seed
        assert result.history == repeat.history, seed
        assert (defaults, layers) == original, seed
        for path, value in _walk(result.values):
            winner = final_assignment(path, result.history)
            assert winner is not None, (seed, path)
            assert winner.value == value, (seed, path)
            if isinstance(value, str) and "::" in value:
                assert winner.source == value.split("::")[0], (seed, path)


@pytest.mark.parametrize(
    "expression",
    [
        "a[0]=1,a[0].b=2",
        "a[0]={x,y},a[0].b=2",
        "a=null,a.b=2",
        "a=null,a[0]=2",
        "a[0]=null,a[0].b=2",
        "a[0]=null,a[0][1]=2",
        "a[2]=x,a[0].b=2",
        "a[0].b=2,a[0][1]=3",
        "a[0]=1,a[0][1]=3",
        "a[2]=x,a={a,b},a[3]=c",
    ],
)
def test_indexed_container_transitions_against_real_helm(tmp_path, expression):
    helm = os.environ.get("HELM_BIN") or shutil.which("helm")
    if not helm:
        pytest.skip("real Helm executable unavailable")
    (tmp_path / "Chart.yaml").write_text("apiVersion: v2\nname: audit\nversion: 0.1.0\n")
    (tmp_path / "values.yaml").write_text("{}\n")
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates/dump.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: dump\ndata:\n"
        "  values: {{ .Values | toJson | quote }}\n"
    )
    proc = subprocess.run(
        [helm, "template", "audit", str(tmp_path), "--set", expression],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode:
        with pytest.raises(ValueTraceError):
            trace_values({}, "defaults", {}, [], set_arguments=[expression])
    else:
        expected = json.loads(yaml.safe_load(proc.stdout)["data"]["values"])
        assert trace_values({}, "defaults", {}, [], set_arguments=[expression]).values == expected
