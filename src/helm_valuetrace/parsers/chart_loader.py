"""
Secure chart loading from local .tgz archives.

Security requirements enforced:
- Never extract outside the temporary directory (path traversal rejection)
- Reject archive entries with absolute paths
- Reject path traversal sequences (../)
- Reject symlinks
- Total extraction size is bounded to MAX_EXTRACT_SIZE
- Temporary directory is always cleaned up via context management

Usage::

    with TgzChartLoader(Path("mychart-0.1.0.tgz")) as chart_dir:
        # chart_dir is a pathlib.Path to the extracted chart directory
        result = trace_values(...)
"""

from __future__ import annotations

import gzip
import tarfile
import tempfile
from pathlib import Path
from typing import BinaryIO

from helm_valuetrace.exceptions import ChartError, ValueTraceError

MAX_EXTRACT_SIZE: int = 50 * 1024 * 1024
MAX_ARCHIVE_STREAM_SIZE: int = 64 * 1024 * 1024
MAX_ARCHIVE_INPUT_SIZE: int = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBERS: int = 10_000


class _BoundedStream:
    def __init__(self, stream: BinaryIO, *, limit: int, description: str) -> None:
        self.stream = stream
        self.consumed = 0
        self.limit = limit
        self.description = description

    def read(self, size: int = -1) -> bytes:
        remaining = self.limit - self.consumed
        data = self.stream.read(min(size, remaining + 1) if size >= 0 else remaining + 1)
        self.consumed += len(data)
        if self.consumed > self.limit:
            raise ChartError(f"Chart archive {self.description} exceeds the safety limit")
        return data


def _validate_entry(member: tarfile.TarInfo, extract_dir: Path) -> None:
    """Raise ChartError if *member* is unsafe to extract.

    Checks (in order):
    1. Absolute path — rejected immediately.
    2. Path traversal — any component that would escape extract_dir.
    3. Symlinks and hard-links — rejected (potential escape vectors).
    """
    if member.size < 0 or member.issparse():
        raise ChartError("Unsafe archive entry (invalid size or sparse file)")

    if member.name.startswith("/"):
        raise ChartError(f"Unsafe archive entry (absolute path): {member.name!r}")

    target = (extract_dir / member.name).resolve()
    try:
        target.relative_to(extract_dir.resolve())
    except ValueError:
        raise ChartError(f"Unsafe archive entry (path traversal): {member.name!r}") from None

    if member.issym() or member.islnk():
        raise ChartError(f"Unsafe archive entry (symlink or hard-link): {member.name!r}")

    if not (member.isfile() or member.isdir()):
        raise ChartError("Unsafe archive entry (special file)")


def is_tgz_archive(path: Path) -> bool:
    """Return True if path appears to be a tar.gz / tgz archive file."""
    name = path.name.lower()
    return name.endswith((".tgz", ".tar.gz"))


def extract_chart_archive(tgz_path: Path) -> TgzChartLoader:
    """Convenience context manager returning a TgzChartLoader."""
    return TgzChartLoader(tgz_path)


class TgzChartLoader:
    """Context manager that securely extracts a Helm .tgz chart archive.

    On ``__enter__`` the archive is validated and extracted to a temporary
    directory.  The chart directory inside the archive is detected and
    returned.  On ``__exit__`` the temporary directory is always deleted.

    Example::

        with TgzChartLoader(Path("chart-0.1.0.tgz")) as chart_dir:
            result = trace_values(chart_dir / "values.yaml", ...)
    """

    def __init__(self, tgz_path: Path) -> None:
        self._tgz_path = tgz_path
        self._tmpdir: tempfile.TemporaryDirectory | None = None  # type: ignore[type-arg]
        self._chart_dir: Path | None = None

    def __enter__(self) -> Path:
        if not self._tgz_path.is_file():
            raise ValueTraceError(f"Chart archive not found: {self._tgz_path}")

        self._tmpdir = tempfile.TemporaryDirectory(prefix="helm-valuetrace-")
        extract_dir = Path(self._tmpdir.name)
        try:
            with (
                self._tgz_path.open("rb") as raw,
                gzip.GzipFile(
                    fileobj=_BoundedStream(
                        raw, limit=MAX_ARCHIVE_INPUT_SIZE, description="compressed input"
                    )
                ) as compressed,
            ):
                stream = _BoundedStream(
                    compressed, limit=MAX_ARCHIVE_STREAM_SIZE, description="decompressed stream"
                )
                with tarfile.open(fileobj=stream, mode="r|") as tar:
                    total_size = 0
                    roots: set[str] = set()
                    for count, member in enumerate(tar, 1):
                        if count > MAX_ARCHIVE_MEMBERS:
                            raise ChartError("Chart archive contains too many entries")
                        _validate_entry(member, extract_dir)
                        relative = (extract_dir / member.name).resolve().relative_to(extract_dir)
                        if relative.parts:
                            roots.add(relative.parts[0])
                        if len(roots) > 1:
                            raise ChartError("Unsupported archive layout: multiple chart roots")
                        total_size += member.size
                        if total_size > MAX_EXTRACT_SIZE:
                            raise ChartError("Chart archive too large (payload safety limit)")
                        if hasattr(tarfile, "data_filter"):
                            tar.extract(member, extract_dir, filter="data")
                        else:
                            tar.extract(member, extract_dir)

            chart_dir = self._find_chart_dir(extract_dir)
            if chart_dir is None:
                raise ChartError(f"No Chart.yaml found inside archive: {self._tgz_path}")

            from helm_valuetrace.parsers.yaml_loader import load_values_file

            try:
                metadata, _, _ = load_values_file(chart_dir / "Chart.yaml")
            except ValueTraceError:
                raise ChartError("Malformed Chart.yaml inside archive") from None
            if not all(
                isinstance(metadata.get(key), str) and metadata[key]
                for key in ("name", "apiVersion")
            ):
                raise ChartError(
                    "Malformed Chart.yaml inside archive: "
                    "required 'name' and 'apiVersion' must be nonempty strings"
                )
            self._chart_dir = chart_dir
            return chart_dir
        except BaseException as exc:
            self.__exit__()
            if isinstance(exc, ChartError):
                raise
            if isinstance(
                exc, (tarfile.TarError, OSError, EOFError, UnicodeError, ValueTraceError)
            ):
                raise ChartError(f"Cannot open chart archive: {self._tgz_path}") from None
            raise

    def __exit__(self, *args: object) -> None:
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None

    @staticmethod
    def _find_chart_dir(extract_dir: Path) -> Path | None:
        """Require Helm's single top-level chart directory; never select a child chart."""
        for child in extract_dir.iterdir():
            if child.is_dir() and (child / "Chart.yaml").is_file():
                return child
        return None
