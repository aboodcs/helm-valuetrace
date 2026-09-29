#!/usr/bin/env sh
set -eu
PLUGIN_DIR="${HELM_PLUGIN_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
PLUGIN_DIR="$(CDPATH= cd -- "$PLUGIN_DIR" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
case "$PYTHON_BIN" in /*) ;; */*) PYTHON_BIN="$PWD/$PYTHON_BIN" ;; esac
cd -- "$PLUGIN_DIR"
"$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || {
  echo 'Python 3.10 or newer is required.' >&2; exit 1;
}
WORK_DIR="$(mktemp -d "$PLUGIN_DIR/.venv-install.XXXXXX")"
PROMOTED=0
COMMITTED=0
cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$COMMITTED" -eq 0 ]; then
    if [ "$PROMOTED" -eq 1 ] && [ -e "$PLUGIN_DIR/.venv" ]; then
      mv "$PLUGIN_DIR/.venv" "$WORK_DIR/failed" || {
        echo "Recovery required: environment files retained in $WORK_DIR" >&2; exit 1;
      }
    fi
    if [ -e "$WORK_DIR/previous" ]; then
      mv "$WORK_DIR/previous" "$PLUGIN_DIR/.venv" || {
        echo "Previous environment retained at $WORK_DIR/previous" >&2; exit 1;
      }
    fi
  fi
  rm -rf -- "$WORK_DIR"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
"$PYTHON_BIN" -m venv "$WORK_DIR/new" || {
  status=$?
  echo 'Python virtual environment creation failed. Install Python venv/ensurepip support.' >&2
  exit "$status"
}
"$WORK_DIR/new/bin/python" -m pip install --disable-pip-version-check -r "$PLUGIN_DIR/requirements.txt" || {
  status=$?
  echo 'Dependency download/install failed. Check pip index configuration or PIP_NO_INDEX and PIP_FIND_LINKS for an offline wheelhouse.' >&2
  exit "$status"
}
"$WORK_DIR/new/bin/python" -c 'import sys; del sys.path[0]; import yaml, jsonschema'
if [ -e "$PLUGIN_DIR/.venv" ]; then
  mv "$PLUGIN_DIR/.venv" "$WORK_DIR/previous"
fi
PROMOTED=1
mv "$WORK_DIR/new" "$PLUGIN_DIR/.venv"
chmod +x "$PLUGIN_DIR/bin/valuetrace"
COMMITTED=1
echo 'Helm ValueTrace dependencies installed.'
