#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_WIDTH=3
CAMPAIGN_PATTERN='^[A-Za-z0-9][A-Za-z0-9._-]*$'

usage() {
  echo "usage: $0 <iterations> {host|client} [--campaign <name>] --config <file> [suite options]" >&2
  echo "  start host first; use the same campaign, peer, modes, and transfer options on both systems" >&2
}

if [ "$#" -lt 2 ]; then
  usage
  exit 2
fi

iteration_text="$1"
role="$2"
shift 2

if [[ ! "$iteration_text" =~ ^[0-9]+$ ]] || (( 10#$iteration_text < 1 )); then
  echo "error: iterations must be a positive integer" >&2
  exit 2
fi
if [ "$role" != "host" ] && [ "$role" != "client" ]; then
  usage
  exit 2
fi

campaign="$(date +%Y-%m-%d)"
suite_arguments=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --campaign)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      campaign="$2"
      shift 2
      ;;
    *)
      suite_arguments+=("$1")
      shift
      ;;
  esac
done

if [[ ! "$campaign" =~ $CAMPAIGN_PATTERN ]]; then
  echo "error: invalid campaign name '$campaign'" >&2
  exit 2
fi

iteration_count=$((10#$iteration_text))
for ((run_index = 1; run_index <= iteration_count; run_index++)); do
  run_name="${campaign}/run-$(printf "%0*d" "$RUN_WIDTH" "$run_index")"
  echo "== ${role} ${run_name} (${run_index}/${iteration_count}) =="
  python3 "$SCRIPT_DIR/rdma-transfer-suite.py" "$role" \
    --run-name "$run_name" "${suite_arguments[@]}"
done

echo "campaign complete: ${campaign} (${iteration_count} run(s), ${role})"
