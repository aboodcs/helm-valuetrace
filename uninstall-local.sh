#!/usr/bin/env sh
set -eu
SOURCE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
command -v helm >/dev/null 2>&1 || { echo 'Helm is required.' >&2; exit 1; }
PLUGIN_HOME="${HELM_PLUGINS:-$(helm env HELM_PLUGINS)}"
[ -n "$PLUGIN_HOME" ] || { echo 'Helm returned an empty plugin directory.' >&2; exit 1; }
TARGET_DIR="$PLUGIN_HOME/helm-valuetrace"
STATE="$("$PYTHON_BIN" "$SOURCE_DIR/scripts/install_state.py" "$TARGET_DIR")"
case "$STATE" in
  absent) echo 'ValueTrace is not installed.' ;;
  unrelated) echo 'Refusing to remove an unrelated directory or symlink.' >&2; exit 1 ;;
  incomplete)
    RECOVERY_DIR="$(mktemp -d "$PLUGIN_HOME/.valuetrace-recovery.XXXXXX")"
    mv "$TARGET_DIR" "$RECOVERY_DIR/previous"
    echo "Removed incomplete ValueTrace installation; files retained at $RECOVERY_DIR/previous"
    ;;
  valid)
    rm -rf -- "$TARGET_DIR"
    echo 'Uninstalled ValueTrace.'
    ;;
esac
