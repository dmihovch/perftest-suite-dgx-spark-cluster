#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_PATTERN='(^|[[:space:]/])(perftest-suite|runner)\.sh([[:space:]]|$)|(^|[[:space:]/])(host|client)_dual_ib_(test|read_bw|write_bw)\.sh([[:space:]]|$)'
readonly GRACE_SECONDS=2
readonly PERFTEST_BINARIES=(
  ib_send_bw ib_write_bw ib_read_bw ib_atomic_bw
  ib_send_lat ib_write_lat ib_read_lat ib_atomic_lat
)

command -v pgrep >/dev/null 2>&1 || {
  echo "error: pgrep is required (install procps)" >&2
  exit 1
}

declare -A selected_pids=()

collect_targets() {
  local process_name process_id process_directory process_command

  selected_pids=()
  for process_name in "${PERFTEST_BINARIES[@]}"; do
    while IFS= read -r process_id; do
      [ -n "$process_id" ] && selected_pids["$process_id"]=1
    done < <(pgrep -x "$process_name" || true)
  done

  while IFS= read -r process_id; do
    [ -n "$process_id" ] || continue
    [ "$process_id" != "$$" ] && [ "$process_id" != "$PPID" ] || continue
    process_directory=$(readlink -f "/proc/$process_id/cwd" 2>/dev/null || true)
    process_command=$(tr '\0' ' ' < "/proc/$process_id/cmdline" 2>/dev/null || true)
    if [ "$process_directory" = "$SCRIPT_DIR" ] \
      || [ "$process_directory" = "$SCRIPT_DIR/ib-test-scripts" ] \
      || [[ "$process_command" == *"$SCRIPT_DIR/"* ]]; then
      selected_pids["$process_id"]=1
    fi
  done < <(pgrep -f "$SCRIPT_PATTERN" || true)
}

collect_targets
if [ "${#selected_pids[@]}" -eq 0 ]; then
  echo "No matching perftest scripts or ib_* perftest commands are running."
  exit 0
fi

echo "Sending TERM to matching processes:"
for process_id in "${!selected_pids[@]}"; do
  ps -p "$process_id" -o pid=,args= 2>/dev/null || true
  kill -TERM "$process_id" 2>/dev/null || true
done

sleep "$GRACE_SECONDS"
collect_targets

if [ "${#selected_pids[@]}" -gt 0 ]; then
  echo "Force-killing processes still matched after ${GRACE_SECONDS}s:"
  for process_id in "${!selected_pids[@]}"; do
    ps -p "$process_id" -o pid=,args= 2>/dev/null || true
    kill -KILL "$process_id" 2>/dev/null || true
  done
fi
