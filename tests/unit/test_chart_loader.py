"""Tests for TgzChartLoader (Phase 5 — .tgz chart support)."""

from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from helm_valuetrace.core import ValueTraceError
from helm_valuetrace.parsers.chart_loader import TgzChartLoader


def _make_tgz(files: dict[str, str]) -> Path:
    """Create a .tgz archive in a temp file and return its path.

    *files* maps archive member names to string contents.
    The archive is written to a NamedTemporaryFile that the caller owns.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".tgz", delete=False)  # noqa: SIM115
    tmp.close()
    archive_path = Path(tmp.name)

    with tarfile.open(archive_path, "w:gz") as tar:
        for name, content in files.items():
            encoded = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(encoded)
            tar.addfile(info, io.BytesIO(encoded))

    return archive_path


def _make_valid_chart_tgz(chart_name: str = "mychart") -> Path:
    """Create a minimal valid Helm chart archive."""
    return _make_tgz(
        {
            f"{chart_name}/Chart.yaml": (f"apiVersion: v2\nname: {chart_name}\nversion: 0.1.0\n"),
            f"{chart_name}/values.yaml": "replicaCount: 1\nimage:\n  repository: nginx\n",
        }
    )


class TestTgzChartLoaderNormal(unittest.TestCase):
    def test_extracts_chart_directory(self) -> None:
        archive = _make_valid_chart_tgz("testchart")
        try:
            with TgzChartLoader(archive) as chart_dir:
                self.assertTrue(chart_dir.is_dir())
                self.assertTrue((chart_dir / "Chart.yaml").is_file())
                self.assertTrue((chart_dir / "values.yaml").is_file())
        finally:
            archive.unlink(missing_ok=True)

    def test_returns_path_object(self) -> None:
        archive = _make_valid_chart_tgz()
        try:
            with TgzChartLoader(archive) as chart_dir:
                self.assertIsInstance(chart_dir, Path)
        finally:
            archive.unlink(missing_ok=True)

    def test_temp_dir_cleaned_up_after_exit(self) -> None:
        archive = _make_valid_chart_tgz()
        try:
            with TgzChartLoader(archive) as chart_dir:
                tmp_parent = chart_dir.parent
            self.assertFalse(tmp_parent.exists())
        finally:
            archive.unlink(missing_ok=True)

    def test_temp_dir_cleaned_up_on_error(self) -> None:
        """Even when the body raises, cleanup must happen."""
        archive = _make_valid_chart_tgz()
        captured_parent: Path | None = None
        try:
            try:
                with TgzChartLoader(archive) as chart_dir:
                    captured_parent = chart_dir.parent
                    raise RuntimeError("simulated error")
            except RuntimeError:
                pass
            if captured_parent is not None:
                self.assertFalse(captured_parent.exists())
        finally:
            archive.unlink(missing_ok=True)


class TestTgzChartLoaderSecurity(unittest.TestCase):
    def _assert_rejected(self, files: dict[str, str]) -> None:
        """Helper: assert that creating an archive with *files* raises ValueTraceError."""
        archive = _make_tgz(files)
        try:
            with self.assertRaises(ValueTraceError), TgzChartLoader(archive):
                pass
        finally:
            archive.unlink(missing_ok=True)

    def test_path_traversal_rejected(self) -> None:
        self._assert_rejected(
            {
                "chart/Chart.yaml": "apiVersion: v2\nname: x\nversion: 0.1.0\n",
                "../etc/passwd": "root:x:0:0:root:/root:/bin/bash",
            }
        )

    def test_absolute_path_rejected(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".tgz", delete=False)  # noqa: SIM115
        tmp.close()
        archive_path = Path(tmp.name)
        try:
            with tarfile.open(archive_path, "w:gz") as tar:
                content = b"apiVersion: v2\nname: x\nversion: 0.1.0\n"
                info = tarfile.TarInfo(name="chart/Chart.yaml")
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))

                secret = b"root:x:0:0"
                info2 = tarfile.TarInfo(name="/etc/passwd")
                info2.size = len(secret)
                tar.addfile(info2, io.BytesIO(secret))

            with self.assertRaises(ValueTraceError), TgzChartLoader(archive_path):
                pass
        finally:
            archive_path.unlink(missing_ok=True)

    def test_symlink_rejected(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".tgz", delete=False)  # noqa: SIM115
        tmp.close()
        archive_path = Path(tmp.name)
        try:
            with tarfile.open(archive_path, "w:gz") as tar:
                content = b"apiVersion: v2\nname: x\nversion: 0.1.0\n"
                info = tarfile.TarInfo(name="chart/Chart.yaml")
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))

                sym = tarfile.TarInfo(name="chart/evil-link")
                sym.type = tarfile.SYMTYPE
                sym.linkname = "/etc/passwd"
                sym.size = 0
                tar.addfile(sym)

            with self.assertRaises(ValueTraceError), TgzChartLoader(archive_path):
                pass
        finally:
            archive_path.unlink(missing_ok=True)

    def test_malformed_archive_raises(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".tgz", delete=False)  # noqa: SIM115
        tmp.write(b"this is not a valid tar.gz file at all")
        tmp.close()
        archive_path = Path(tmp.name)
        try:
            with self.assertRaises(ValueTraceError), TgzChartLoader(archive_path):
                pass
        finally:
            archive_path.unlink(missing_ok=True)

    def test_missing_chart_yaml_raises(self) -> None:
        """Archive with no Chart.yaml anywhere must raise."""
        archive = _make_tgz({"somefile.txt": "hello"})
        try:
            with self.assertRaises(ValueTraceError), TgzChartLoader(archive):
                pass
        finally:
            archive.unlink(missing_ok=True)

    def test_missing_archive_file_raises(self) -> None:
        with (
            self.assertRaises(ValueTraceError),
            TgzChartLoader(Path("/nonexistent/path/chart.tgz")),
        ):
            pass


class TestTgzChartLoaderCLI(unittest.TestCase):
    """Integration: CLI accepts a .tgz archive as the CHART argument."""

    def test_cli_accepts_tgz(self) -> None:
        import json
        import sys
        from io import StringIO

        from helm_valuetrace.cli import main

        archive = _make_valid_chart_tgz("myapp")
        try:
            buf = StringIO()
            old_stdout = sys.stdout
            sys.stdout = buf
            try:
                rc = main([str(archive), "-o", "json", "--no-redact"])
            finally:
                sys.stdout = old_stdout
            self.assertEqual(rc, 0)

            doc = json.loads(buf.getvalue())
            self.assertIn("values", doc)
            keys = {row["key"] for row in doc["values"]}
            self.assertIn("replicaCount", keys)
        finally:
            archive.unlink(missing_ok=True)

    def test_versioned_archive_detection(self) -> None:
        """Versioned archives such as mychart-1.2.3.tgz must be recognized as archives."""
        import json
        import sys
        from io import StringIO

        from helm_valuetrace.cli import main

        with tempfile.TemporaryDirectory() as tmpdir:
            for ext in [".tgz", ".tar.gz"]:
                archive_path = Path(tmpdir) / f"mychart-1.2.3{ext}"
                with tarfile.open(archive_path, "w:gz") as tar:
                    for name, content in [
                        ("mychart/Chart.yaml", "apiVersion: v2\nname: mychart\nversion: 1.2.3\n"),
                        ("mychart/values.yaml", "replicaCount: 3\n"),
                    ]:
                        data = content.encode("utf-8")
                        ti = tarfile.TarInfo(name)
                        ti.size = len(data)
                        tar.addfile(ti, io.BytesIO(data))

                buf = StringIO()
                old_stdout = sys.stdout
                sys.stdout = buf
                try:
                    rc = main([str(archive_path), "-o", "json", "--no-redact"])
                finally:
                    sys.stdout = old_stdout

                self.assertEqual(rc, 0, f"Failed for {archive_path.name}")
                doc = json.loads(buf.getvalue())
                vals = {r["key"]: r["value"] for r in doc["values"]}
                self.assertEqual(vals["replicaCount"], 3)

    def test_directory_archive_parity(self) -> None:
        """Values traced from an archive match the unpacked chart directory."""
        import json
        import sys
        from io import StringIO

        from helm_valuetrace.cli import main

        with tempfile.TemporaryDirectory() as tmpdir:
            chart_dir = Path(tmpdir) / "testchart"
            chart_dir.mkdir()
            (chart_dir / "Chart.yaml").write_text(
                "apiVersion: v2\nname: testchart\nversion: 0.1.0\n"
            )
            (chart_dir / "values.yaml").write_text(
                "image:\n  repository: nginx\n  tag: 1.25\nreplicaCount: 2\n"
            )

            archive_path = Path(tmpdir) / "testchart-0.1.0.tgz"
            with tarfile.open(archive_path, "w:gz") as tar:
                tar.add(chart_dir, arcname="testchart")

            def _get_vals(target: Path) -> dict:
                buf = StringIO()
                old_stdout = sys.stdout
                sys.stdout = buf
                try:
                    main([str(target), "-o", "json", "--no-redact"])
                finally:
                    sys.stdout = old_stdout
                return {r["key"]: r["value"] for r in json.loads(buf.getvalue())["values"]}

            dir_vals = _get_vals(chart_dir)
            archive_vals = _get_vals(archive_path)
            self.assertEqual(dir_vals, archive_vals)

    def test_explain_against_archive(self) -> None:
        """The explain subcommand works against .tgz chart archives."""
        import sys
        from io import StringIO

        from helm_valuetrace.cli import main

        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "mychart-1.0.0.tgz"
            with tarfile.open(archive_path, "w:gz") as tar:
                for name, content in [
                    ("mychart/Chart.yaml", "apiVersion: v2\nname: mychart\nversion: 1.0.0\n"),
                    ("mychart/values.yaml", "replicaCount: 5\n"),
                ]:
                    data = content.encode("utf-8")
                    ti = tarfile.TarInfo(name)
                    ti.size = len(data)
                    tar.addfile(ti, io.BytesIO(data))

            buf = StringIO()
            old_stdout = sys.stdout
            sys.stdout = buf
            try:
                rc = main([str(archive_path), "explain", "replicaCount", "--no-redact"])
            finally:
                sys.stdout = old_stdout

            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("replicaCount", out)
            self.assertIn("5", out)

    def test_archive_with_overrides(self) -> None:
        """Values file and all --set-* flags work when targeting an archive."""
        import json
        import sys
        from io import StringIO

        from helm_valuetrace.cli import main

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            archive_path = tmp / "mychart-1.0.0.tgz"
            with tarfile.open(archive_path, "w:gz") as tar:
                for name, content in [
                    ("mychart/Chart.yaml", "apiVersion: v2\nname: mychart\nversion: 1.0.0\n"),
                    ("mychart/values.yaml", "replicaCount: 1\nimage:\n  repo: base\n"),
                ]:
                    data = content.encode("utf-8")
                    ti = tarfile.TarInfo(name)
                    ti.size = len(data)
                    tar.addfile(ti, io.BytesIO(data))

            override_file = tmp / "custom.yaml"
            override_file.write_text("image:\n  repo: custom\n")
            secret_file = tmp / "cert.txt"
            secret_file.write_text("file-cert")

            args = [
                str(archive_path),
                "-o",
                "json",
                "--no-redact",
                "-f",
                str(override_file),
                "--set",
                "replicaCount=10",
                "--set-string",
                "env=prod",
                "--set-json",
                'settings={"debug":false}',
                "--set-file",
                f"tls={secret_file}",
                "--set-literal",
                "lit=literal.val",
            ]

            buf = StringIO()
            old_stdout = sys.stdout
            sys.stdout = buf
            try:
                rc = main(args)
            finally:
                sys.stdout = old_stdout

            self.assertEqual(rc, 0)
            vals = {r["key"]: r["value"] for r in json.loads(buf.getvalue())["values"]}
            self.assertEqual(vals["replicaCount"], 10)
            self.assertEqual(vals["image.repo"], "custom")
            self.assertEqual(vals["env"], "prod")
            self.assertEqual(vals["settings.debug"], False)
            self.assertEqual(vals["tls"], "file-cert")
            self.assertEqual(vals["lit"], "literal.val")


class TestTgzArchiveErrors(unittest.TestCase):
    """Clean error handling and exit codes for invalid archives."""

    def _run_cli(self, args: list[str]) -> tuple[int, str, str]:
        import sys
        from io import StringIO

        from helm_valuetrace.cli import main

        buf_out, buf_err = StringIO(), StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = buf_out, buf_err
        try:
            rc = main(args)
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        return rc, buf_out.getvalue(), buf_err.getvalue()

    def test_nonexistent_archive_returns_code_one_no_traceback(self) -> None:
        rc, _out, err = self._run_cli(["/nonexistent/path/mychart-1.0.0.tgz"])
        self.assertEqual(rc, 1)
        self.assertIn("Chart archive not found", err)
        self.assertNotIn("Traceback", err)

    def test_corrupt_archive_returns_code_three_no_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            corrupt = Path(tmpdir) / "corrupt-0.1.0.tgz"
            corrupt.write_bytes(b"garbage not gzip")
            rc, _out, err = self._run_cli([str(corrupt)])
            self.assertEqual(rc, 3)
            self.assertIn("Cannot open chart archive", err)
            self.assertNotIn("Traceback", err)

    def test_non_helm_tarball_returns_code_three_no_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tarball = Path(tmpdir) / "nothelm-0.1.0.tgz"
            with tarfile.open(tarball, "w:gz") as tar:
                data = b"hello"
                ti = tarfile.TarInfo("file.txt")
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))

            rc, _out, err = self._run_cli([str(tarball)])
            self.assertEqual(rc, 3)
            self.assertIn("No Chart.yaml found inside archive", err)
            self.assertNotIn("Traceback", err)

    def test_archive_missing_chart_yaml_returns_code_three_no_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tarball = Path(tmpdir) / "nochart-0.1.0.tgz"
            with tarfile.open(tarball, "w:gz") as tar:
                data = b"foo: bar"
                ti = tarfile.TarInfo("mychart/values.yaml")
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))

            rc, _out, err = self._run_cli([str(tarball)])
            self.assertEqual(rc, 3)
            self.assertIn("No Chart.yaml found inside archive", err)
            self.assertNotIn("Traceback", err)

    def test_archive_with_malformed_chart_yaml_returns_code_three_no_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tarball = Path(tmpdir) / "malformed-0.1.0.tgz"
            with tarfile.open(tarball, "w:gz") as tar:
                data = b"::: invalid yaml :::"
                ti = tarfile.TarInfo("mychart/Chart.yaml")
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))

            rc, _out, err = self._run_cli([str(tarball)])
            self.assertEqual(rc, 3)
            self.assertIn("Malformed Chart.yaml", err)
            self.assertNotIn("Traceback", err)

    def test_archive_with_path_traversal_returns_code_three_no_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tarball = Path(tmpdir) / "traversal-0.1.0.tgz"
            with tarfile.open(tarball, "w:gz") as tar:
                ti = tarfile.TarInfo(name="../evil.txt")
                data = b"malicious content"
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))

            rc, _out, err = self._run_cli([str(tarball)])
            self.assertEqual(rc, 3)
            self.assertIn("Unsafe archive entry (path traversal)", err)
            self.assertNotIn("Traceback", err)

    def test_archive_with_absolute_path_returns_code_three_no_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tarball = Path(tmpdir) / "absolute-0.1.0.tgz"
            with tarfile.open(tarball, "w:gz") as tar:
                ti = tarfile.TarInfo(name="/etc/passwd")
                data = b"root:x:0:0"
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))

            rc, _out, err = self._run_cli([str(tarball)])
            self.assertEqual(rc, 3)
            self.assertIn("Unsafe archive entry (absolute path)", err)
            self.assertNotIn("Traceback", err)

    def test_archive_with_symlink_returns_code_three_no_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tarball = Path(tmpdir) / "symlink-0.1.0.tgz"
            with tarfile.open(tarball, "w:gz") as tar:
                ti = tarfile.TarInfo(name="chart/link")
                ti.type = tarfile.SYMTYPE
                ti.linkname = "/etc/passwd"
                tar.addfile(ti)

            rc, _out, err = self._run_cli([str(tarball)])
            self.assertEqual(rc, 3)
            self.assertIn("Unsafe archive entry (symlink or hard-link)", err)
            self.assertNotIn("Traceback", err)

    def test_archive_with_hardlink_returns_code_three_no_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tarball = Path(tmpdir) / "hardlink-0.1.0.tgz"
            with tarfile.open(tarball, "w:gz") as tar:
                ti = tarfile.TarInfo(name="chart/hardlink")
                ti.type = tarfile.LNKTYPE
                ti.linkname = "/etc/passwd"
                tar.addfile(ti)

            rc, _out, err = self._run_cli([str(tarball)])
            self.assertEqual(rc, 3)
            self.assertIn("Unsafe archive entry (symlink or hard-link)", err)
            self.assertNotIn("Traceback", err)

    def test_archive_oversized_returns_code_three_no_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tarball = Path(tmpdir) / "oversized-0.1.0.tgz"
            with tarfile.open(tarball, "w:gz") as tar:
                ti = tarfile.TarInfo(name="chart/huge.bin")
                ti.size = 51 * 1024 * 1024
                tar.addfile(ti, io.BytesIO(b"x" * (51 * 1024 * 1024)))

            rc, _out, err = self._run_cli([str(tarball)])
            self.assertEqual(rc, 3)
            self.assertIn("Chart archive too large", err)
            self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
