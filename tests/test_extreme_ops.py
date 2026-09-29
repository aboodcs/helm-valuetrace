"""Disposable archive, launcher and installation regression cases from the external audit."""

from __future__ import annotations

import gzip
import io
import os
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from helm_valuetrace.exceptions import ChartError
from helm_valuetrace.parsers import chart_loader

ROOT = Path(__file__).resolve().parents[1]
METADATA = b"apiVersion: v2\nname: audit\nversion: 0.1.0\n"


def archive(tmp_path, entries, *, pax_headers=None):
    target = tmp_path / "synthetic chart \N{GREEK SMALL LETTER LAMDA}.tgz"
    with tarfile.open(target, "w:gz", pax_headers=pax_headers) as tar:
        for name, value in entries:
            entry = tarfile.TarInfo(name)
            if isinstance(value, bytes):
                entry.size = len(value)
                tar.addfile(entry, io.BytesIO(value))
            else:
                entry.type = value[1]
                entry.linkname = "../synthetic-target"
                tar.addfile(entry)
    return target


@pytest.mark.parametrize(
    "entries",
    [
        [("chart/Chart.yaml", b"\xff")],
        [("chart/Chart.yaml", METADATA), ("chart/file", b"x"), ("chart/file/child", b"x")],
    ],
    ids=["invalid-utf8", "file-directory-collision"],
)
def test_archive_input_errors_clean_up_immediately(tmp_path, entries):
    loader = chart_loader.TgzChartLoader(archive(tmp_path, entries))
    try:
        with pytest.raises(ChartError), loader:
            pass
        assert loader._tmpdir is None
    finally:
        loader.__exit__()


def test_archive_io_error_cleans_up(tmp_path, monkeypatch):
    target = archive(tmp_path, [("chart/Chart.yaml", METADATA)])

    def denied(*args, **kwargs):
        raise PermissionError("synthetic filesystem fault")

    monkeypatch.setattr(tarfile.TarFile, "extract", denied)
    monkeypatch.setattr(tarfile.TarFile, "extractall", denied)
    loader = chart_loader.TgzChartLoader(target)
    try:
        with pytest.raises(ChartError), loader:
            pass
        assert loader._tmpdir is None
    finally:
        loader.__exit__()


def test_archive_entry_budget_includes_empty_files(tmp_path, monkeypatch):
    monkeypatch.setattr(chart_loader, "MAX_ARCHIVE_MEMBERS", 4, raising=False)
    target = archive(
        tmp_path, [("chart/Chart.yaml", METADATA)] + [(f"chart/empty{i}", b"") for i in range(5)]
    )
    with pytest.raises(ChartError, match="too many entries"), chart_loader.TgzChartLoader(target):
        pass


def test_archive_stream_budget_includes_pax_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(chart_loader, "MAX_ARCHIVE_STREAM_SIZE", 4096, raising=False)
    target = archive(
        tmp_path, [("chart/Chart.yaml", METADATA)], pax_headers={"comment": "x" * 12000}
    )
    with (
        pytest.raises(ChartError, match="decompressed stream"),
        chart_loader.TgzChartLoader(target),
    ):
        pass


def test_archive_input_budget_includes_gzip_headers(tmp_path, monkeypatch):
    monkeypatch.setattr(chart_loader, "MAX_ARCHIVE_INPUT_SIZE", 4096, raising=False)
    target = archive(tmp_path, [("chart/Chart.yaml", METADATA)])
    raw_tar = gzip.decompress(target.read_bytes())
    with (
        target.open("wb") as output,
        gzip.GzipFile(filename="x" * 5000, fileobj=output, mode="wb") as compressor,
    ):
        compressor.write(raw_tar)
    with pytest.raises(ChartError, match="compressed input"), chart_loader.TgzChartLoader(target):
        pass


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE]
)
def test_archive_special_entries_rejected_without_external_write(tmp_path, kind):
    sentinel = tmp_path / "synthetic-target"
    sentinel.write_text("protected")
    target = archive(tmp_path, [("chart/Chart.yaml", METADATA), ("chart/entry", ("type", kind))])
    with pytest.raises(ChartError), chart_loader.TgzChartLoader(target):
        pass
    assert sentinel.read_text() == "protected"


@pytest.mark.parametrize("name", ["../../synthetic-target", "/synthetic-absolute-target"])
def test_archive_escaping_paths_rejected(tmp_path, name):
    target = archive(tmp_path, [("chart/Chart.yaml", METADATA), (name, b"fake")])
    with pytest.raises(ChartError), chart_loader.TgzChartLoader(target):
        pass


def test_archive_size_limit_enforced_before_large_extraction(tmp_path, monkeypatch):
    monkeypatch.setattr(chart_loader, "MAX_EXTRACT_SIZE", 100)
    target = archive(tmp_path, [("chart/Chart.yaml", METADATA), ("chart/data", b"x" * 101)])
    with pytest.raises(ChartError, match="too large"), chart_loader.TgzChartLoader(target):
        pass


def test_archive_normal_unicode_layout_and_cleanup(tmp_path):
    target = archive(
        tmp_path, [("chart/Chart.yaml", METADATA), ("chart/values.yaml", b"value: 3\n")]
    )
    with chart_loader.TgzChartLoader(target) as chart:
        extracted = chart.parent
        assert (chart / "values.yaml").read_text() == "value: 3\n"
    assert not extracted.exists()


def test_truncated_gzip_has_controlled_error_and_cleanup(tmp_path):
    target = archive(tmp_path, [("chart/Chart.yaml", METADATA)])
    target.write_bytes(target.read_bytes()[:-12])
    loader = chart_loader.TgzChartLoader(target)
    with pytest.raises(ChartError), loader:
        pass
    assert loader._tmpdir is None


def test_archive_regular_symlink_path_and_broken_symlink(tmp_path):
    target = archive(tmp_path, [("chart/Chart.yaml", METADATA)])
    linked = tmp_path / "chart-link.tgz"
    linked.symlink_to(target)
    with chart_loader.TgzChartLoader(linked) as chart:
        assert (chart / "Chart.yaml").read_bytes() == METADATA
    broken = tmp_path / "broken.tgz"
    broken.symlink_to(tmp_path / "absent")
    from helm_valuetrace.exceptions import ValueTraceError

    with pytest.raises(ValueTraceError, match="not found"), chart_loader.TgzChartLoader(broken):
        pass


@pytest.mark.parametrize("second_metadata", [True, False])
def test_ambiguous_archive_roots_rejected_after_real_helm_comparison(tmp_path, second_metadata):
    target = archive(
        tmp_path,
        [
            ("one/Chart.yaml", METADATA.replace(b"audit", b"one")),
            ("one/values.yaml", b"value: 1\n"),
            *([("two/Chart.yaml", METADATA.replace(b"audit", b"two"))] if second_metadata else []),
            ("two/values.yaml", b"value: 2\n"),
            (
                "two/templates/result.yaml",
                b"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: audit\n"
                b"data:\n  result: {{ .Values.value | quote }}\n",
            ),
        ],
    )
    proc = subprocess.run(
        [os.environ.get("HELM_BIN", "helm"), "template", "audit", str(target)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    assert 'result: "2"' in proc.stdout
    with (
        pytest.raises(ChartError, match="multiple chart roots"),
        chart_loader.TgzChartLoader(target),
    ):
        pass


def test_nested_archive_wrapper_rejected_like_real_helm(tmp_path):
    target = archive(tmp_path, [("wrapper/chart/Chart.yaml", METADATA)])
    proc = subprocess.run(
        [os.environ.get("HELM_BIN", "helm"), "template", "audit", str(target)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode != 0
    assert "Chart.yaml" in proc.stderr
    with pytest.raises(ChartError), chart_loader.TgzChartLoader(target):
        pass


def test_plugin_launcher_cannot_import_package_from_chart_cwd(tmp_path):
    package = tmp_path / "helm_valuetrace"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text('print("LOCAL_CWD_CODE_EXECUTED")\n')
    env = dict(os.environ, HELM_PLUGIN_DIR=str(ROOT))
    env["PYTHONPATH"] = str(ROOT / ".devdeps")
    proc = subprocess.run(
        [str(ROOT / "bin/valuetrace"), "--version"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert "LOCAL_CWD_CODE_EXECUTED" not in proc.stdout
    assert "0.2.0" in proc.stdout


def executable(path, content):
    path.write_text(content)
    path.chmod(0o755)
    return path


def installer_env(tmp_path):
    bins = tmp_path / "tools"
    bins.mkdir()
    plugins = tmp_path / "plugins with spaces"
    target = plugins / "helm-valuetrace"
    target.mkdir(parents=True)
    (target / "plugin.yaml").write_text("name: valuetrace\n")
    (target / "old-installation").write_text("protected")
    unrelated = plugins / "unrelated"
    unrelated.mkdir()
    (unrelated / "marker").write_text("protected")
    executable(
        bins / "helm",
        '#!/bin/sh\nif [ "$1" = env ]; then printf "%s\\n" "$TEST_PLUGINS"; exit; fi\n'
        'if [ "$1" = plugin ] && [ "$2" = list ]; then\n'
        ' [ "${FAIL_LIST:-0}" = 0 ] || exit 42\n'
        ' printf "NAME VERSION\\nvaluetrace 0.2.0\\n"; exit; fi\n'
        '[ "${FAIL_LAUNCH:-0}" = 0 ] || exit 75\n'
        'shift\nexec "$TEST_PLUGINS/helm-valuetrace/bin/valuetrace" "$@"\n',
    )
    python = executable(
        bins / "test-python",
        f"#!{sys.executable}\n"
        "import os, pathlib, sys\n"
        'if sys.argv[1:3] == ["-m", "venv"]:\n'
        ' if os.environ.get("FAIL_VENV"): raise SystemExit(73)\n'
        ' p = pathlib.Path(sys.argv[3]) / "bin" / "python"\n'
        " p.parent.mkdir(parents=True)\n"
        ' p.write_text("#!/bin/sh\\nif [ \\"$1\\" = -m ] && [ \\"$2\\" = pip ]; '
        'then exit ${FAIL_PIP:-0}; fi\\nexec " + '
        f'{shlex.quote(sys.executable)!r} + " \\"$@\\"\\n")\n'
        " p.chmod(0o755)\n"
        "else: os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n",
    )
    env = dict(
        os.environ,
        PATH=f"{bins}:{os.environ['PATH']}",
        TEST_PLUGINS=str(plugins),
        PYTHON_BIN=str(python),
    )
    env["PYTHONPATH"] = str(ROOT / ".devdeps")
    env.pop("HELM_PLUGINS", None)
    return env, bins, target


def run_script(name, env):
    return subprocess.run(
        ["/bin/sh", str(ROOT / name)], env=env, capture_output=True, text=True, timeout=20
    )


def test_uninstaller_preserves_files_when_plugin_home_is_unknown(tmp_path):
    env, bins, target = installer_env(tmp_path)
    executable(bins / "helm", "#!/bin/sh\nexit 42\n")
    env.pop("HELM_PLUGINS", None)
    proc = run_script("uninstall-local.sh", env)
    assert proc.returncode != 0
    assert "not installed" not in proc.stdout
    assert (target / "old-installation").read_text() == "protected"


@pytest.mark.parametrize(
    ("fault", "exit_code"),
    [({"FAIL_PIP": "74"}, 74), ({"FAIL_VENV": "1"}, 73), ({"FAIL_LAUNCH": "1"}, 75)],
)
def test_installer_failure_preserves_old_and_unrelated_plugins(tmp_path, fault, exit_code):
    env, _, target = installer_env(tmp_path)
    proc = run_script("install-local.sh", dict(env, **fault))
    assert proc.returncode == exit_code, proc.stderr
    assert (target / "old-installation").read_text() == "protected"
    assert (target.parent / "unrelated/marker").read_text() == "protected"
    assert sorted(p.name for p in target.parent.iterdir()) == ["helm-valuetrace", "unrelated"]


def test_signal_immediately_after_promotion_restores_old_installation(tmp_path):
    env, bins, target = installer_env(tmp_path)
    executable(
        bins / "mv",
        '#!/bin/sh\n/bin/mv "$@" || exit $?\n'
        'if [ "$2" = "$TEST_PLUGINS/helm-valuetrace" ] && [ "${1##*/}" = new ]; then\n'
        ' : > "$TEST_PLUGINS/signal-reached"\n kill -TERM "$PPID"\nfi\n',
    )
    proc = run_script("install-local.sh", env)
    assert proc.returncode != 0
    assert (target.parent / "signal-reached").exists()
    assert (target / "old-installation").read_text() == "protected"
    assert not (target / "previous").exists()


def test_dependency_hook_rolls_back_after_final_preparation_failure(tmp_path):
    env, bins, _ = installer_env(tmp_path)
    plugin = tmp_path / "dependency plugin"
    (plugin / ".venv").mkdir(parents=True)
    (plugin / ".venv/old-environment").write_text("protected")
    (plugin / "bin").mkdir()
    (plugin / "bin/valuetrace").write_text("synthetic")
    (plugin / "requirements.txt").write_text("")
    executable(bins / "chmod", "#!/bin/sh\nexit 79\n")
    proc = run_script("scripts/install.sh", dict(env, HELM_PLUGIN_DIR=str(plugin)))
    assert proc.returncode == 79
    assert (plugin / ".venv/old-environment").read_text() == "protected"
    assert not list(plugin.glob(".venv-install.*"))


def test_installer_missing_helm_preserves_old_installation(tmp_path):
    env, _, target = installer_env(tmp_path)
    isolated = tmp_path / "without helm"
    isolated.mkdir()
    (isolated / "dirname").symlink_to("/usr/bin/dirname")
    proc = run_script("install-local.sh", dict(env, PATH=str(isolated)))
    assert proc.returncode == 1
    assert "Helm is required" in proc.stderr
    assert (target / "old-installation").read_text() == "protected"


def test_installer_missing_python_preserves_old_installation(tmp_path):
    env, _, target = installer_env(tmp_path)
    proc = run_script("install-local.sh", dict(env, PYTHON_BIN=str(tmp_path / "missing-python")))
    assert proc.returncode == 1
    assert "Python 3.10 or newer is required" in proc.stderr
    assert (target / "old-installation").read_text() == "protected"


def test_installer_refuses_unrelated_target(tmp_path):
    env, _, target = installer_env(tmp_path)
    (target / "plugin.yaml").write_text("name: other-plugin\n")
    proc = run_script("install-local.sh", env)
    assert proc.returncode == 1
    assert "unrelated plugin" in proc.stderr
    assert (target / "old-installation").read_text() == "protected"


def test_installer_refuses_symlink_without_touching_its_target(tmp_path):
    env, _, target = installer_env(tmp_path)
    original = target.with_name("synthetic-original")
    target.rename(original)
    target.symlink_to(original, target_is_directory=True)
    proc = run_script("install-local.sh", env)
    assert proc.returncode == 1
    assert "symlink installation" in proc.stderr
    assert target.is_symlink()
    assert (original / "old-installation").read_text() == "protected"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_unwritable_plugin_directory_preserves_installation(tmp_path):
    env, _, target = installer_env(tmp_path)
    target.parent.chmod(0o500)
    try:
        proc = run_script("install-local.sh", env)
        assert proc.returncode != 0
        assert (target / "old-installation").read_text() == "protected"
    finally:
        target.parent.chmod(0o700)


def test_interrupted_dependency_preparation_preserves_installation(tmp_path):
    env, bins, target = installer_env(tmp_path)
    python = executable(
        bins / "interrupt-python",
        '#!/bin/sh\nif [ "$1" = -m ] && [ "$2" = venv ]; then\n'
        ' : > "$TEST_PLUGINS/interruption-reached"\n kill -TERM "$PPID"\n exit 77\nfi\n'
        f'exec {shlex.quote(sys.executable)} "$@"\n',
    )
    proc = run_script("install-local.sh", dict(env, PYTHON_BIN=str(python)))
    assert proc.returncode != 0
    assert (target.parent / "interruption-reached").exists()
    assert (target / "old-installation").read_text() == "protected"
    assert not list(target.parent.glob(".valuetrace-work.*"))


def test_uninstaller_targets_only_valuetrace(tmp_path):
    env, _, target = installer_env(tmp_path)
    proc = run_script("uninstall-local.sh", env)
    assert proc.returncode == 0, proc.stderr
    assert "incomplete" in proc.stdout
    assert not target.exists()
    saved = next(target.parent.glob(".valuetrace-recovery.*/previous"))
    assert (saved / "old-installation").read_text() == "protected"
    assert (target.parent / "unrelated/marker").read_text() == "protected"


def test_uninstaller_absent_plugin_does_not_call_uninstall(tmp_path):
    env, bins, target = installer_env(tmp_path)
    target.rename(target.with_name("saved-original"))
    executable(
        bins / "helm",
        '#!/bin/sh\n[ "$1" = env ] || exit 78\nprintf "%s\\n" "$TEST_PLUGINS"\n',
    )
    proc = run_script("uninstall-local.sh", env)
    assert proc.returncode == 0, proc.stderr
    assert "not installed" in proc.stdout
    assert (target.parent / "unrelated/marker").read_text() == "protected"


def test_dependency_hook_cannot_execute_cwd_venv_module(tmp_path):
    chart_cwd = tmp_path / "untrusted chart"
    chart_cwd.mkdir()
    (chart_cwd / "venv.py").write_text(
        'from pathlib import Path\nPath("CWD_VENV_EXECUTED").write_text("fake marker")\n'
    )
    plugin = tmp_path / "trusted plugin"
    (plugin / "bin").mkdir(parents=True)
    (plugin / "bin/valuetrace").write_text("synthetic")
    (plugin / "requirements.txt").write_text("")
    empty_wheels = tmp_path / "empty wheel cache"
    empty_wheels.mkdir()
    env = dict(
        os.environ,
        HELM_PLUGIN_DIR=str(plugin),
        PYTHON_BIN=sys.executable,
        PIP_NO_INDEX="1",
        PIP_FIND_LINKS=str(empty_wheels),
    )
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        ["/bin/sh", str(ROOT / "scripts/install.sh")],
        env=env,
        cwd=chart_cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0
    assert not (chart_cwd / "CWD_VENV_EXECUTED").exists()
    assert not list(plugin.glob(".venv-install.*"))
