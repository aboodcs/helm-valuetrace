#!/usr/bin/env sh
set -eu
SOURCE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
case "$PYTHON_BIN" in /*) ;; */*) PYTHON_BIN="$PWD/$PYTHON_BIN" ;; esac
cd -- "$SOURCE_DIR"
command -v helm >/dev/null 2>&1 || { echo 'Helm is required.' >&2; exit 1; }
"$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || {
  echo 'Python 3.10 or newer is required.' >&2; exit 1;
}
PLUGIN_HOME="${HELM_PLUGINS:-$(helm env HELM_PLUGINS)}"
[ -n "$PLUGIN_HOME" ] || { echo 'Helm returned an empty plugin directory.' >&2; exit 1; }
mkdir -p "$PLUGIN_HOME"
TARGET_DIR="$PLUGIN_HOME/helm-valuetrace"
STATE="$("$PYTHON_BIN" "$SOURCE_DIR/scripts/install_state.py" "$TARGET_DIR")"
case "$STATE" in
  unrelated) echo 'Refusing to replace an unrelated plugin/directory or symlink installation.' >&2; exit 1 ;;
  incomplete) echo 'Recovering incomplete ValueTrace installation; previous files will be retained.' ;;
esac
WORK_DIR="$(mktemp -d "$PLUGIN_HOME/.valuetrace-work.XXXXXX")"
STAGING_DIR="$WORK_DIR/new"
BACKUP_DIR="$WORK_DIR/previous"
PROMOTED=0
COMMITTED=0
RECOVERY_DIR=""
cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$COMMITTED" -eq 0 ]; then
    if [ "$PROMOTED" -eq 1 ] && [ -d "$TARGET_DIR" ]; then
      if ! mv "$TARGET_DIR" "$WORK_DIR/failed"; then
        echo "Recovery required: installation files retained in $WORK_DIR" >&2
        exit 1
      fi
    fi
    if [ -d "$BACKUP_DIR" ]; then
      if ! mv "$BACKUP_DIR" "$TARGET_DIR"; then
        echo "Recovery required: previous installation retained at $BACKUP_DIR" >&2
        exit 1
      fi
    fi
  fi
  if [ -n "$RECOVERY_DIR" ]; then
    rmdir "$RECOVERY_DIR" 2>/dev/null || :
  fi
  rm -rf -- "$WORK_DIR"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
mkdir -p "$STAGING_DIR/bin" "$STAGING_DIR/scripts" "$STAGING_DIR/src"
touch "$STAGING_DIR/.valuetrace-install"
cp "$SOURCE_DIR/plugin.yaml" "$SOURCE_DIR/requirements.txt" "$STAGING_DIR/"
cp "$SOURCE_DIR/bin/valuetrace" "$STAGING_DIR/bin/"
cp "$SOURCE_DIR/scripts/install.sh" "$STAGING_DIR/scripts/"
cp -R "$SOURCE_DIR/src/helm_valuetrace" "$STAGING_DIR/src/"
chmod +x "$STAGING_DIR/bin/valuetrace" "$STAGING_DIR/scripts/install.sh"
HELM_PLUGIN_DIR="$STAGING_DIR" PYTHON_BIN="$PYTHON_BIN" "$STAGING_DIR/scripts/install.sh"
HELM_PLUGIN_DIR="$STAGING_DIR" "$STAGING_DIR/bin/valuetrace" --version
if [ "$STATE" = incomplete ]; then
  RECOVERY_DIR="$(mktemp -d "$PLUGIN_HOME/.valuetrace-recovery.XXXXXX")"
  BACKUP_DIR="$RECOVERY_DIR/previous"
fi
if [ -d "$TARGET_DIR" ]; then
  mv "$TARGET_DIR" "$BACKUP_DIR"
fi
PROMOTED=1
mv "$STAGING_DIR" "$TARGET_DIR"
helm valuetrace --version
COMMITTED=1
if [ "$STATE" = incomplete ]; then
  echo "Previous incomplete files retained at $BACKUP_DIR"
fi
echo "Installed Helm ValueTrace in $TARGET_DIR"
