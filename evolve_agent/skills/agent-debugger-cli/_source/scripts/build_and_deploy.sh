#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")"/.. && pwd)"
OUT_DIR="$HERE/dist"
rm -rf "$OUT_DIR" "$HERE/build" "$HERE"/*.egg-info
python3 -m build --wheel "$HERE" --outdir "$OUT_DIR"
WHL=$(ls "$OUT_DIR"/agent_debugger_core-0.0.0-*.whl | head -1)
test -f "$WHL"
DEST="$HERE/../agent_debugger_core-0.0.0-py3-none-any.whl"
cp "$WHL" "$DEST"
echo "Deployed: $DEST"
