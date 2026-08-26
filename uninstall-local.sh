#!/usr/bin/env sh
set -eu

if ! command -v helm >/dev/null 2>&1; then
  echo "Helm is required." >&2
  exit 1
fi

if helm plugin list 2>/dev/null | awk 'NR > 1 {print $1}' | grep -qx 'valuetrace'; then
  helm plugin uninstall valuetrace
else
  echo "ValueTrace is not installed."
fi
