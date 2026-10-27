#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <source-dir> <target:path>" >&2
  exit 2
fi

source_dir=${1%/}
[[ -d "$source_dir" ]] || { echo "error: source directory not found: $source_dir" >&2; exit 2; }
target_path=${2%/}/

exec rsync --archive --compress --partial --human-readable --progress -e ssh \
  --exclude='/.git/' \
  --exclude='/.venv/' \
  --exclude='/perftest-results/' \
  --exclude='/transmission-results/' \
  --exclude='/.agents/' \
  --exclude='/.aws/' \
  --exclude='/.codex/' \
  --exclude='/README.md' \
  --exclude='/LICENSE' \
  --exclude='/docs/' \
  --exclude='/figs/' \
  --exclude='/plot_perftest.py' \
  --exclude='/requirements.txt' \
  --exclude='__pycache__/' \
  --exclude='*.py[cod]' \
  "$source_dir/" "$target_path"
