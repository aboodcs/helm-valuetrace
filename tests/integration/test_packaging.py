"""
Integration tests for packaging, wheel/sdist artifacts, and clean installation (Phase 3).

Verifies:
1. python -m build succeeds and generates wheel + sdist
2. Wheel contains all package modules and plugin runtime assets
3. Sdist contains all source files and metadata
4. Clean wheel installation into a virtualenv works
5. Clean sdist installation into a virtualenv works
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _network_available() -> bool:
    """Return True if PyPI is reachable (needed for pip install tests)."""
    try:
        socket.setdefaulttimeout(3)
        socket.getaddrinfo("pypi.org", 443)
        return True
    except OSError:
        return False


_NEEDS_NETWORK = pytest.mark.skipif(
    not os.environ.get("PIP_FIND_LINKS") and not _network_available(),
    reason="PyPI not reachable in this environment (offline/sandboxed) — "
    "pip install tests require network access",
)


def clean_runtime_env() -> dict[str, str]:
    """Artifact checks must not fall back to source or caller Python settings."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


class TestPackagingArtifacts(unittest.TestCase):
    """Builds the package and verifies artifact contents and installation."""

    build_dir: tempfile.TemporaryDirectory  # type: ignore[type-arg]
    dist_dir: Path
    whl_path: Path
    sdist_path: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.build_dir = tempfile.TemporaryDirectory(prefix="valuetrace-build-test-")
        cls.dist_dir = Path(cls.build_dir.name) / "dist"
        cls.dist_dir.mkdir(parents=True, exist_ok=True)

        env = dict(
            os.environ,
            PYTHONPATH=os.environ.get("PYTHONPATH", str(REPO_ROOT / "src")),
        )
        cmd = [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(cls.dist_dir),
            str(REPO_ROOT),
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False, env=env, timeout=180
        )
        if proc.returncode != 0:
            raise RuntimeError(f"python -m build failed ({proc.returncode}): {proc.stderr}")

        wheels = list(cls.dist_dir.glob("*.whl"))
        sdists = list(cls.dist_dir.glob("*.tar.gz"))
        if not wheels or not sdists:
            raise RuntimeError("Build did not produce both wheel and sdist")
        cls.whl_path = wheels[0]
        cls.sdist_path = sdists[0]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.build_dir.cleanup()

    def test_build_succeeds(self) -> None:
        self.assertTrue(self.whl_path.is_file())
        self.assertTrue(self.sdist_path.is_file())
        self.assertGreater(self.whl_path.stat().st_size, 1000)
        self.assertGreater(self.sdist_path.stat().st_size, 1000)

    def test_wheel_contains_required_package_modules(self) -> None:
        with zipfile.ZipFile(self.whl_path) as z:
            names = set(z.namelist())

        required_modules = [
            "helm_valuetrace/__init__.py",
            "helm_valuetrace/py.typed",
            "helm_valuetrace/cli.py",
            "helm_valuetrace/core.py",
            "helm_valuetrace/models.py",
            "helm_valuetrace/subcharts.py",
            "helm_valuetrace/parsers/__init__.py",
            "helm_valuetrace/parsers/chart_loader.py",
            "helm_valuetrace/parsers/set_parser.py",
            "helm_valuetrace/analysis/__init__.py",
            "helm_valuetrace/analysis/schema.py",
            "helm_valuetrace/analysis/secrets.py",
            "helm_valuetrace/analysis/templates.py",
            "helm_valuetrace/output/__init__.py",
            "helm_valuetrace/output/explain.py",
        ]
        for mod in required_modules:
            self.assertIn(mod, names, f"Missing {mod} in wheel")

        data_files = [
            "helm_valuetrace-0.2.0.data/data/plugin.yaml",
            "helm_valuetrace-0.2.0.data/data/scripts/install.sh",
            "helm_valuetrace-0.2.0.data/data/bin/valuetrace",
        ]
        for df in data_files:
            self.assertIn(df, names, f"Missing {df} in wheel")

    def test_sdist_contains_required_source_files(self) -> None:
        with tarfile.open(self.sdist_path, "r:gz") as tar:
            names = set(tar.getnames())

        required_suffixes = [
            "plugin.yaml",
            "pyproject.toml",
            "requirements.txt",
            "README.md",
            "LICENSE",
            "bin/valuetrace",
            "scripts/install.sh",
            "scripts/install_state.py",
            "install-local.sh",
            "uninstall-local.sh",
            "src/helm_valuetrace/__init__.py",
            "src/helm_valuetrace/py.typed",
            "src/helm_valuetrace/parsers/set_parser.py",
            "src/helm_valuetrace/parsers/chart_loader.py",
            "src/helm_valuetrace/analysis/schema.py",
            "src/helm_valuetrace/output/explain.py",
        ]
        for req in required_suffixes:
            found = any(name.endswith(req) for name in names)
            self.assertTrue(found, f"Missing {req} in sdist")

    @_NEEDS_NETWORK
    def test_clean_wheel_installation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="valuetrace-venv-") as tmpdir:
            venv_dir = Path(tmpdir)
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir)],
                check=True,
                timeout=60,
            )

            pip_bin = venv_dir / "bin" / "pip"
            py_bin = venv_dir / "bin" / "python"
            install_res = subprocess.run(
                [
                    str(pip_bin),
                    "install",
                    "--force-reinstall",
                    str(self.whl_path),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
                env=clean_runtime_env(),
                cwd=tmpdir,
            )
            self.assertEqual(install_res.returncode, 0, install_res.stderr)

            import_res = subprocess.run(
                [
                    str(py_bin),
                    "-c",
                    "import helm_valuetrace; print(helm_valuetrace.__version__); "
                    "print(helm_valuetrace.__file__)",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
                env=clean_runtime_env(),
                cwd=tmpdir,
            )
            self.assertEqual(import_res.returncode, 0)
            version, installed_path = import_res.stdout.strip().splitlines()
            self.assertEqual(version, "0.2.0")
            self.assertTrue(Path(installed_path).resolve().is_relative_to(venv_dir.resolve()))

            cli_bin = venv_dir / "bin" / "helm-valuetrace"
            cli_res = subprocess.run(
                [str(cli_bin), "--version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
                env=clean_runtime_env(),
                cwd=tmpdir,
            )
            self.assertEqual(cli_res.returncode, 0, cli_res.stderr)
            self.assertIn("0.2.0", cli_res.stdout)

    @_NEEDS_NETWORK
    def test_clean_sdist_installation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="valuetrace-venv-") as tmpdir:
            venv_dir = Path(tmpdir)
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir)],
                check=True,
                timeout=60,
            )

            pip_bin = venv_dir / "bin" / "pip"
            py_bin = venv_dir / "bin" / "python"
            install_res = subprocess.run(
                [
                    str(pip_bin),
                    "install",
                    str(self.sdist_path),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
                env=clean_runtime_env(),
                cwd=tmpdir,
            )
            self.assertEqual(install_res.returncode, 0, install_res.stderr)

            import_res = subprocess.run(
                [
                    str(py_bin),
                    "-c",
                    "import helm_valuetrace; print(helm_valuetrace.__version__); "
                    "print(helm_valuetrace.__file__)",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
                env=clean_runtime_env(),
                cwd=tmpdir,
            )
            self.assertEqual(import_res.returncode, 0)
            version, installed_path = import_res.stdout.strip().splitlines()
            self.assertEqual(version, "0.2.0")
            self.assertTrue(Path(installed_path).resolve().is_relative_to(venv_dir.resolve()))

    @_NEEDS_NETWORK
    def test_helm_plugin_runtime_from_sdist(self) -> None:
        """Verify the Helm plugin installs and runs from the built sdist artifact."""
        helm_check = subprocess.run(
            ["helm", "version", "--short"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if helm_check.returncode != 0:
            self.skipTest("helm binary not available")

        with tempfile.TemporaryDirectory(prefix="valuetrace-helm-") as tmpdir:
            tmp = Path(tmpdir)
            with tarfile.open(self.sdist_path, "r:gz") as t:
                if hasattr(tarfile, "data_filter"):
                    t.extractall(tmp, filter="data")
                else:
                    t.extractall(tmp)

            extracted = next(
                p for p in tmp.iterdir() if p.is_dir() and (p / "plugin.yaml").is_file()
            )
            helm_home = tmp / "helm"
            helm_home.mkdir()

            env = dict(
                clean_runtime_env(),
                HELM_DATA_HOME=str(helm_home),
                HELM_CONFIG_HOME=str(helm_home),
                HELM_CACHE_HOME=str(helm_home),
                HELM_PLUGINS=str(helm_home / "plugins"),
                PYTHON_BIN=sys.executable,
            )

            p_inst = subprocess.run(
                ["helm", "plugin", "install", str(extracted)],
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
                env=env,
            )
            self.assertEqual(p_inst.returncode, 0, f"helm plugin install failed: {p_inst.stderr}")

            p_ver = subprocess.run(
                ["helm", "valuetrace", "--version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
                env=env,
            )
            self.assertEqual(p_ver.returncode, 0)
            self.assertIn("0.2.0", p_ver.stdout)

            basic_chart = REPO_ROOT / "tests" / "fixtures" / "_explain_test_chart"
            p_trace = subprocess.run(
                ["helm", "valuetrace", str(basic_chart), "-o", "json", "--no-redact"],
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
                env=env,
            )
            self.assertEqual(p_trace.returncode, 0)
            import json

            doc = json.loads(p_trace.stdout)
            self.assertIn("values", doc)
            keys = {r["key"] for r in doc["values"]}
            self.assertIn("replicaCount", keys)


if __name__ == "__main__":
    unittest.main()
