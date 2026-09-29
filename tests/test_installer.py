"""Installer fault injection and lifecycle tests.

Most tests simulate faults using fake stubs and require no network access.
The lifecycle test (test_repeat_install_and_launch_failure_rollback) runs the
real install.sh using configured pip indexes or PIP_FIND_LINKS. Supply a wheelhouse to run
these tests fully offline.
"""

import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _network_available() -> bool:
    try:
        socket.setdefaulttimeout(3)
        socket.getaddrinfo("pypi.org", 443)
        return True
    except OSError:
        return False


_NEEDS_NETWORK = pytest.mark.skipif(
    not os.environ.get("PIP_FIND_LINKS") and not _network_available(),
    reason="PyPI not reachable — install.sh pip install -r requirements.txt requires network",
)


class InstallerTests(unittest.TestCase):
    def test_failed_dependency_upgrade_preserves_installation(self):
        with tempfile.TemporaryDirectory(prefix="valuetrace space ") as tmp:
            root = Path(tmp)
            plugins = root / "plugins with spaces"
            target = plugins / "helm-valuetrace"
            target.mkdir(parents=True)
            (target / "plugin.yaml").write_text("name: valuetrace\n")
            (target / "working-marker").write_text("old installation")
            legacy = plugins / "values-source"
            legacy.mkdir()
            (legacy / "marker").write_text("untouched")
            bins = root / "bin"
            bins.mkdir()
            helm = bins / "helm"
            helm.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = env ]; then printf "%s\\n" "$TEST_PLUGINS"; else exit 91; fi\n'
            )
            helm.chmod(0o755)
            python = bins / "test-python"
            python.write_text('#!/bin/sh\nif [ "$1" = -m ]; then exit 73; fi\nexec python3 "$@"\n')
            python.chmod(0o755)
            env = dict(
                os.environ,
                PATH=f"{bins}:{os.environ['PATH']}",
                TEST_PLUGINS=str(plugins),
                PYTHON_BIN=str(python),
            )
            proc = subprocess.run(
                ["sh", str(ROOT / "install-local.sh")], env=env, capture_output=True, text=True
            )
            self.assertEqual(proc.returncode, 73, proc.stderr)
            self.assertEqual((target / "working-marker").read_text(), "old installation")
            self.assertEqual((legacy / "marker").read_text(), "untouched")
            self.assertEqual(
                sorted(p.name for p in plugins.iterdir()), ["helm-valuetrace", "values-source"]
            )

    @_NEEDS_NETWORK
    def test_repeat_install_and_launch_failure_rollback(self):
        import sys

        with tempfile.TemporaryDirectory(prefix="valuetrace lifecycle ") as tmp:
            root = Path(tmp)
            plugins = root / "plugins with spaces"
            bins = root / "bin"
            bins.mkdir()
            helm = bins / "helm"
            helm.write_text(
                '#!/bin/sh\nif [ "$1" = env ]; then printf "%s\\n" "$TEST_PLUGINS"; exit; fi\n'
                '[ "${FAIL_LAUNCH:-0}" = 0 ] || exit 75\n'
                'shift\nexec "$TEST_PLUGINS/helm-valuetrace/bin/valuetrace" "$@"\n'
            )
            helm.chmod(0o755)
            python = bins / "test-python"
            python.write_text(
                f"#!{sys.executable}\n"
                "import os, subprocess, sys, venv\n"
                'if sys.argv[1:3] == ["-m", "venv"]:\n'
                "    venv.create(sys.argv[3], with_pip=True)\n"
                "else:\n"
                "    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n"
            )
            python.chmod(0o755)
            env = dict(
                os.environ,
                PATH=f"{bins}:{os.environ['PATH']}",
                TEST_PLUGINS=str(plugins),
                PYTHON_BIN=str(python),
            )
            for _ in range(2):
                proc = subprocess.run(
                    ["sh", str(ROOT / "install-local.sh")], env=env, capture_output=True, text=True
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
            marker = plugins / "helm-valuetrace" / "marker"
            marker.write_text("working installation")
            proc = subprocess.run(
                ["sh", str(ROOT / "install-local.sh")],
                env=dict(env, FAIL_LAUNCH="1"),
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 75, proc.stderr)
            self.assertEqual(marker.read_text(), "working installation")

    @_NEEDS_NETWORK
    def test_real_repair_and_corrupt_manifest_recovery(self):
        """Runs in CI online or fully offline with PIP_FIND_LINKS supplied."""
        with tempfile.TemporaryDirectory(prefix="valuetrace recovery ") as tmp:
            plugins = Path(tmp) / "plugins"
            target = plugins / "helm-valuetrace"
            (target / "src").mkdir(parents=True)
            env = dict(os.environ, HELM_PLUGINS=str(plugins))
            for corruption in (None, "missing", "invalid"):
                if corruption == "missing":
                    (target / "plugin.yaml").unlink()
                elif corruption == "invalid":
                    (target / "plugin.yaml").write_text("name: [broken yaml")
                proc = subprocess.run(
                    ["sh", str(ROOT / "install-local.sh")],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("Recovering incomplete", proc.stdout)
                self.assertTrue((target / ".venv/bin/python").is_file())
                check = subprocess.run(
                    ["helm", "valuetrace", "-V"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(check.returncode, 0, check.stderr)
            self.assertEqual(len(list(plugins.glob(".valuetrace-recovery.*/previous"))), 3)
            proc = subprocess.run(
                ["sh", str(ROOT / "uninstall-local.sh")],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Uninstalled", proc.stdout)
            self.assertFalse(target.exists())
