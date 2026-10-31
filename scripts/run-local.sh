#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CODEX_BIN="$ROOT/.tools/node_modules/.bin/codex"

if [[ ! -x "$CODEX_BIN" ]]; then
  "$ROOT/scripts/install-codex-local.sh"
fi

cd "$ROOT"
export CRA_CODEX_PATH="${CRA_CODEX_PATH:-$CODEX_BIN}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$ROOT/.uv-cache}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$ROOT/.uv-python}"
exec uv run codex-responses-adapter
