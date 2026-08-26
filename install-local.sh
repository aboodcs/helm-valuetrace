#!/usr/bin/env sh
set -eu

SOURCE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

if ! command -v helm >/dev/null 2>&1; then
  echo "Helm is required. Install Helm first, then run this script again." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required." >&2
  exit 1
fi

PLUGIN_HOME="$(helm env HELM_PLUGINS)"
TARGET_DIR="$PLUGIN_HOME/helm-valuetrace"
mkdir -p "$PLUGIN_HOME"

if helm plugin list 2>/dev/null | awk 'NR > 1 {print $1}' | grep -qx 'valuetrace'; then
  echo "Removing the previous ValueTrace installation..."
  helm plugin uninstall valuetrace >/dev/null
fi

if helm plugin list 2>/dev/null | awk 'NR > 1 {print $1}' | grep -qx 'values-source'; then
  echo "Removing the legacy values-source installation..."
  helm plugin uninstall values-source >/dev/null
fi

if [ -e "$TARGET_DIR" ] || [ -L "$TARGET_DIR" ]; then
  echo "Cannot install because the target already exists: $TARGET_DIR" >&2
  echo "Move or remove that specific directory, then run this installer again." >&2
  exit 1
fi

STAGING_DIR="$(mktemp -d "$PLUGIN_HOME/.helm-valuetrace.install.XXXXXX")"
cleanup() {
  if [ -d "$STAGING_DIR" ]; then
    rm -rf -- "$STAGING_DIR"
  fi
}
trap cleanup EXIT HUP INT TERM

mkdir -p "$STAGING_DIR/bin" "$STAGING_DIR/scripts" "$STAGING_DIR/src/helm_valuetrace"
cp "$SOURCE_DIR/plugin.yaml" "$STAGING_DIR/plugin.yaml"
cp "$SOURCE_DIR/requirements.txt" "$STAGING_DIR/requirements.txt"
cp "$SOURCE_DIR/bin/valuetrace" "$STAGING_DIR/bin/valuetrace"
cp "$SOURCE_DIR/scripts/install.sh" "$STAGING_DIR/scripts/install.sh"
cp "$SOURCE_DIR/src/helm_valuetrace/__init__.py" "$STAGING_DIR/src/helm_valuetrace/__init__.py"
cp "$SOURCE_DIR/src/helm_valuetrace/core.py" "$STAGING_DIR/src/helm_valuetrace/core.py"
cp "$SOURCE_DIR/src/helm_valuetrace/cli.py" "$STAGING_DIR/src/helm_valuetrace/cli.py"
chmod +x "$STAGING_DIR/bin/valuetrace" "$STAGING_DIR/scripts/install.sh"

mv "$STAGING_DIR" "$TARGET_DIR"
trap - EXIT HUP INT TERM

HELM_PLUGIN_DIR="$TARGET_DIR" "$TARGET_DIR/scripts/install.sh"

if ! helm plugin list | awk 'NR > 1 {print $1}' | grep -qx 'valuetrace'; then
  echo "Installation files were copied, but Helm did not detect the plugin." >&2
  exit 1
fi

echo
echo "Permanent installation directory: $TARGET_DIR"
echo "You may now delete the downloaded ZIP and extracted folder."
