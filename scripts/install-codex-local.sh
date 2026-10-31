#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$ROOT/.npm-cache"
npm_config_cache="$ROOT/.npm-cache" \
  npm install --prefix "$ROOT/.tools" --no-save @openai/codex@latest

CODEX_BIN="$ROOT/.tools/node_modules/.bin/codex"
"$CODEX_BIN" --version
printf '\nLocal Codex installed at: %s\n' "$CODEX_BIN"
