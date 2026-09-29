"""Permanent regressions for the reproduced black-box feedback."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.integration.test_subcharts import (
    _helm_template_values,
    _make_base_chart,
    _vt_run,
    _vt_values_dict,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "parent,child,condition,alias,flags",
    [
        ("foo: {enabled: true}", "port: 80", "foo.enabled", "foo", []),
        ("foo: {enabled: false}", "port: 80", "foo.enabled", "foo", []),
        ("{}", "port: 80", "foo.enabled", "foo", []),
        ("{}", "enabled: false\nport: 80", "foo.enabled", "foo", []),
        ("{}", "enabled: true\nport: 80", "foo.enabled", "foo", []),
        ("features: {api: {enabled: false}}", "port: 80", "features.api.enabled", "foo", []),
        ("backend: {enabled: false}", "port: 80", "backend.enabled", "backend", []),
        (
            "foo: {enabled: false, port: 90}",
            "port: 80",
            "foo.enabled",
            "foo",
            ["--set", "foo.enabled=true"],
        ),
        (
            "foo: {enabled: true}",
            "port: 80",
            "foo.enabled",
            "foo",
            ["--set-json", "foo.enabled=false"],
        ),
        ("{}", "port: 80", "foo.enabled", "foo", ["--set", "foo.enabled=false"]),
    ],
)
def test_conditions_match_helm(tmp_path, monkeypatch, parent, child, condition, alias, flags):
    monkeypatch.setenv("HELM_BIN", "helm")
    chart, _ = _make_base_chart(
        tmp_path,
        parent_values=parent,
        child_values=child,
        child_name="foo",
        parent_chart_yaml="apiVersion: v2\nname: parent\nversion: 0.1.0\n"
        "dependencies:\n- name: foo\n  version: 0.1.0\n"
        f"  alias: {alias}\n  condition: {condition}\n",
    )
    expected = _helm_template_values(chart, flags)
    code, out, err = _vt_run([str(chart), "-o", "json", "--no-redact", *flags])
    assert code == 0, err
    doc = json.loads(out)
    assert _vt_values_dict(doc) == expected
    for row in doc["values"]:
        if row["key"].endswith("port"):
            assert "values.yaml:" in row["source"]


def test_multiple_conditions_and_file_override_match_helm(tmp_path, monkeypatch):
    monkeypatch.setenv("HELM_BIN", "helm")
    chart, child = _make_base_chart(tmp_path, parent_values="child: {enabled: true}\n")
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: parent\nversion: 0.1.0\ndependencies:\n"
        "- name: child\n  condition: child.enabled\n"
        "- name: other\n  version: 0.1.0\n  alias: backend\n  condition: backend.enabled\n"
    )
    other = chart / "charts/other"
    other.mkdir()
    (other / "Chart.yaml").write_text("apiVersion: v2\nname: other\nversion: 0.1.0\n")
    (other / "values.yaml").write_text("port: 90\n")
    (child / "values.schema.json").write_text('{"required": ["missing"]}')
    layer = tmp_path / "override.yaml"
    layer.write_text("child: {enabled: false, custom: retained}\nbackend: {enabled: true}\n")
    flags = ["-f", str(layer), "--set", "backend.port=99"]
    expected = _helm_template_values(chart, flags)
    code, out, err = _vt_run([str(chart), "-o", "json", "--no-redact", *flags])
    assert code == 0, err
    assert _vt_values_dict(json.loads(out)) == expected
    assert "SCHEMA WARNINGS" not in err
    assert "charts/child/values.yaml" not in out
    assert "--set[1]" in out


@pytest.mark.parametrize(
    "condition,value",
    [
        ("child.enabled,other.enabled", "true"),
        ("child.enabled", '"false"'),
        ("child.enabled", "null"),
        ("child[0].enabled", "true"),
    ],
)
def test_unsupported_conditions_fail_clearly(tmp_path, monkeypatch, condition, value):
    from helm_valuetrace.cli import _detect_helm_major_version

    chart, _ = _make_base_chart(tmp_path, parent_values=f"child: {{enabled: {value}}}")
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: parent\nversion: 0.1.0\n"
        f"dependencies:\n- name: child\n  condition: {condition}\n"
    )
    is_helm4 = _detect_helm_major_version() == 4
    if is_helm4 and value == "null" and condition == "child.enabled":
        code, out, err = _vt_run([str(chart)])
        assert code == 0, err
        assert not err
        monkeypatch.delenv("HELM_BIN", raising=False)
        code_h3, out_h3, err_h3 = _vt_run([str(chart)])
        assert code_h3 == 3, err_h3
        assert "unsupported" in err_h3.lower()
        assert not out_h3
    else:
        code, out, err = _vt_run([str(chart)])
        assert code == 3, err
        assert "unsupported" in err.lower()
        assert not out


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--set", "port=91"),
        ("--set-string", "port=91"),
        ("--set-json", "port=91"),
        ("--set-literal", "port=91"),
        ("--set-file", None),
    ],
)
@pytest.mark.parametrize("fmt", ["table", "json", "yaml"])
@pytest.mark.parametrize("explain", [False, True])
def test_cli_policy_all_flags_formats_and_explain(tmp_path, flag, value, fmt, explain):
    chart, _ = _make_base_chart(tmp_path, parent_values="port: 80\n")
    payload = tmp_path / "payload"
    payload.write_text("POLICY_SECRET_CANARY")
    override = value or f"port={payload}"
    args = [str(chart), "-o", fmt, "--deny-cli-overrides", flag, override]
    if explain:
        args += ["--explain", "port"]
    code, out, err = _vt_run(args)
    assert code == 2, err
    assert f"{flag}[1]" in err
    assert "POLICY_SECRET_CANARY" not in err
    if fmt != "table":
        assert isinstance(yaml.safe_load(out), dict)
    assert _vt_run([str(chart), "--deny-cli-overrides"])[0] == 0
    assert _vt_run([str(chart), "--deny-source", "*--set*", flag, override])[0] == 0


@pytest.mark.parametrize(
    "template",
    [
        "{{ .Values | toJson }}",
        "{{ toYaml .Values }}",
        '{{ include "helper" .Values }}',
        "{{- $v := .Values -}}",
        '{{ index .Values "port" }}',
        "{{ toJson $.Values }}",
    ],
)
@pytest.mark.parametrize("filename", ["dump.yaml", "_helpers.tpl"])
def test_root_values_prevents_unused_false_positives(tmp_path, template, filename):
    chart, _ = _make_base_chart(tmp_path)
    (chart / "templates/dump.yaml").unlink()
    (chart / "templates" / filename).write_text(template)
    code, _, err = _vt_run([str(chart), "--template-analysis"])
    assert code == 0
    assert "Root .Values is consumed wholesale" in err
    assert "Values not referenced" not in err


def test_static_refs_still_report_unused(tmp_path):
    chart, _ = _make_base_chart(tmp_path, parent_values="port: 80\nimage: {tag: stable}\n")
    (chart / "templates/dump.yaml").write_text("{{ .Values.image | toYaml }}")
    code, _, err = _vt_run([str(chart), "--template-analysis"])
    assert code == 0
    assert "WARNING: port" in err
    assert "WARNING: image.tag" not in err


@pytest.mark.parametrize("fmt", ["table", "json", "yaml"])
def test_primary_and_legacy_explain(tmp_path, fmt):
    chart, _ = _make_base_chart(tmp_path)
    primary = _vt_run([str(chart), "--explain", "replicaCount", "-o", fmt])
    legacy = _vt_run([str(chart), "explain", "replicaCount", "-o", fmt])
    assert primary[0] == 0
    assert primary == legacy


@pytest.mark.parametrize("flag", ["-V", "--version"])
def test_version_without_chart(flag):
    proc = subprocess.run([str(ROOT / "bin/valuetrace"), flag], text=True, capture_output=True)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "Helm ValueTrace 0.2.0"
    assert not proc.stderr


def installer(script, plugins, **extra):
    env = dict(os.environ, HELM_PLUGINS=str(plugins), **extra)
    return subprocess.run(
        ["sh", str(ROOT / script)], env=env, text=True, capture_output=True, timeout=90
    )


@pytest.mark.parametrize(
    "layout", ["empty", "src", "corrupt", "missing-manifest", "invalid-encoding"]
)
def test_incomplete_uninstall_preserves_files(tmp_path, layout):
    target = tmp_path / "helm-valuetrace"
    target.mkdir()
    if layout == "src":
        (target / "src").mkdir()
    if layout in ("corrupt", "missing-manifest", "invalid-encoding"):
        (target / "src/helm_valuetrace").mkdir(parents=True)
        (target / "src/helm_valuetrace/cli.py").write_text("partial")
    if layout == "corrupt":
        (target / "plugin.yaml").write_text("name: [broken yaml")
        (target / ".valuetrace-install").touch()
    if layout == "invalid-encoding":
        (target / "plugin.yaml").write_bytes(b"\xff")
    before = sorted(str(p.relative_to(target)) for p in target.rglob("*"))
    proc = installer("uninstall-local.sh", tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "incomplete" in proc.stdout
    assert not target.exists()
    saved = next(tmp_path.glob(".valuetrace-recovery.*/previous"))
    assert sorted(str(p.relative_to(saved)) for p in saved.rglob("*")) == before
    assert "not installed" in installer("uninstall-local.sh", tmp_path).stdout


@pytest.mark.parametrize("layout", ["unrelated", "other-plugin", "symlink"])
def test_unrelated_directories_are_untouched(tmp_path, layout):
    target = tmp_path / "helm-valuetrace"
    if layout == "symlink":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        target.symlink_to(elsewhere)
    else:
        target.mkdir()
    (target / "valuable").write_text("keep")
    if layout == "other-plugin":
        (target / "plugin.yaml").write_text("name: other-plugin\n")
    for script in ("install-local.sh", "uninstall-local.sh"):
        proc = installer(script, tmp_path)
        assert proc.returncode == 1
        assert "unrelated" in proc.stderr
        assert (target / "valuable").read_text() == "keep"


def test_empty_wheelhouse_failure_preserves_incomplete_install(tmp_path):
    plugins = tmp_path / "plugins"
    target = plugins / "helm-valuetrace"
    (target / "src").mkdir(parents=True)
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    proc = installer(
        "install-local.sh",
        plugins,
        PIP_NO_INDEX="1",
        PIP_FIND_LINKS=str(wheels),
        PIP_CONFIG_FILE=os.devnull,
    )
    assert proc.returncode != 0
    assert "Dependency download/install failed" in proc.stderr
    assert (target / "src").is_dir()
    assert not list(plugins.glob(".valuetrace-work.*"))


@pytest.mark.parametrize("version", ["", "^0.1.0", "9.0.0"])
def test_ambiguous_alias_condition_version_is_rejected(tmp_path, version):
    chart, _ = _make_base_chart(tmp_path)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: parent\nversion: 0.1.0\ndependencies:\n"
        f'- name: child\n  alias: backend\n  version: "{version}"\n'
        "  condition: backend.enabled\n"
    )
    code, out, err = _vt_run([str(chart)])
    assert code == 3
    assert "exact matching version" in err
    assert not out


def test_nested_dependency_conditions_rejected(tmp_path):
    chart, child = _make_base_chart(tmp_path)
    (child / "Chart.yaml").write_text(
        "apiVersion: v2\nname: child\nversion: 0.1.0\n"
        "dependencies:\n- name: grandchild\n  condition: grandchild.enabled\n"
    )
    code, out, err = _vt_run([str(chart)])
    assert code == 3
    assert "Conditions on nested dependencies are unsupported" in err
    assert not out


@pytest.mark.parametrize("layout", ["src", "healthy"])
def test_interrupted_install_preserves_previous_directory(tmp_path, layout):
    import signal
    import time

    plugins = tmp_path / "plugins"
    target = plugins / "helm-valuetrace"
    (target / "src").mkdir(parents=True)
    if layout == "healthy":
        (target / "plugin.yaml").write_text((ROOT / "plugin.yaml").read_text())
        (target / "src/helm_valuetrace").mkdir()
        (target / "src/helm_valuetrace/cli.py").write_text("healthy marker")
        (target / "bin").mkdir()
        (target / "bin/valuetrace").write_text("healthy launcher")
        (target / ".venv/bin").mkdir(parents=True)
        (target / ".venv/bin/python").write_text("healthy python")
    before = {str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    marker = tmp_path / "venv-started"
    python = tmp_path / "python"
    python.write_text(
        '#!/bin/sh\nif [ "$1" = -m ]; then\n'
        '  touch "$START_MARKER"\n  sleep 60\nelse\n  exec python3 "$@"\nfi\n'
    )
    python.chmod(0o755)
    env = dict(
        os.environ, HELM_PLUGINS=str(plugins), PYTHON_BIN=str(python), START_MARKER=str(marker)
    )
    process = subprocess.Popen(
        ["sh", str(ROOT / "install-local.sh")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists()
        os.killpg(process.pid, signal.SIGTERM)
        process.communicate(timeout=10)
        assert process.returncode != 0
        assert (target / "src").is_dir()
        assert {
            str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file()
        } == before
        assert not list(plugins.glob(".valuetrace-work.*"))
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
