#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <target:path> <destination-dir>" >&2
  exit 2
fi

source_path=${1%/}/
destination_dir=${2%/}/

exec rsync --archive --compress --partial --human-readable --progress -e ssh \
  "$source_path" "$destination_dir"
