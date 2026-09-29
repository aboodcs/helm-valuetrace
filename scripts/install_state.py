"""Classify a local install without importing it or requiring third-party YAML.

Ambiguous empty legacy remnants are recoverable only by preserving them. Callers
must retain incomplete directories, never delete them on the strength of layout.
"""

import re
import sys
from pathlib import Path


def installation_state(target: Path) -> str:
    if target.is_symlink():
        return "unrelated"
    if not target.exists():
        return "absent"
    if not target.is_dir():
        return "unrelated"
    manifest = target / "plugin.yaml"
    try:
        try:
            text = manifest.read_text() if manifest.is_file() and not manifest.is_symlink() else ""
        except UnicodeError:
            text = ""
        names = re.findall(r"(?m)^name:\s*([^\n#]+)", text)
        name = names[0].strip().strip("\"'") if names else ""
        if re.fullmatch(r"[A-Za-z0-9_-]+", name) and name != "valuetrace":
            return "unrelated"
        owned = (target / ".valuetrace-install").is_file() or (
            target / "src/helm_valuetrace"
        ).is_dir()
        if name == "valuetrace" and len(names) == 1:
            canonical = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text()

            def normalize(value: str) -> str:
                return re.sub(r"(?m)^version:.*$", "version: VERSION", value).strip()

            if (
                normalize(text) == normalize(canonical)
                and (target / "bin/valuetrace").is_file()
                and (target / "src/helm_valuetrace/cli.py").is_file()
                and (target / ".venv/bin/python").is_file()
            ):
                return "valid"
            return "incomplete"
        entries = list(target.iterdir())
        empty_remnant = not entries or (
            len(entries) == 1
            and entries[0].name == "src"
            and not entries[0].is_symlink()
            and entries[0].is_dir()
            and not any(entries[0].iterdir())
        )
        return "incomplete" if owned or empty_remnant else "unrelated"
    except (OSError, UnicodeError):
        return "unrelated"


if __name__ == "__main__":
    print(installation_state(Path(sys.argv[1])))
