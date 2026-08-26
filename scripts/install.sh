#!/usr/bin/env sh
set -eu

PLUGIN_DIR="${HELM_PLUGIN_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3 is required to install Helm ValueTrace." >&2
  exit 1
fi

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 or newer is required to install Helm ValueTrace." >&2
  exit 1
fi

"$PYTHON_BIN" -m venv "$PLUGIN_DIR/.venv"
"$PLUGIN_DIR/.venv/bin/python" -m pip install --disable-pip-version-check --quiet -r "$PLUGIN_DIR/requirements.txt"
chmod +x "$PLUGIN_DIR/bin/valuetrace"

echo "Helm ValueTrace installed successfully."
echo "Try: helm valuetrace --help"
