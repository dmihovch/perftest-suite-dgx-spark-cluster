#!/usr/bin/env bash
#
# Kills any perftest ib_* process left running in the background, so a stale
# server stops holding its port before the next run.

set -euo pipefail

readonly PERFTEST_BINARIES=(
	ib_send_lat ib_write_lat ib_read_lat ib_atomic_lat
	ib_send_bw ib_write_bw ib_read_bw ib_atomic_bw
)

for binary in "${PERFTEST_BINARIES[@]}"; do
	if pkill -x "$binary" 2>/dev/null; then
		echo "killed $binary"
	fi
done
