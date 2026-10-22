#!/usr/bin/env bash
set -euo pipefail

# Runs repeated perftest-suite runs for one host/client pair, all filed under
# <campaign>/<role>/run-NNN directories. Start the HOST side first, then the
# CLIENT side, with the same --campaign name on both machines.
#
#   ./runner.sh 5 host --campaign spark-a-to-spark-b --config spark-a.json
#   ./runner.sh 5 client --campaign spark-a-to-spark-b --config spark-b.json
#
# Pass --config for a machine-specific JSON topology file and --peer when the
# file defines multiple peers. The campaign stops on the first failed run.

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SUITE_SCRIPT="$SCRIPT_DIR/perftest-suite.sh"
readonly CAMPAIGN_NAME_PATTERN='^[A-Za-z0-9][A-Za-z0-9._-]*$'
readonly RUN_INDEX_WIDTH=3
# Every mode the suite supports, so one campaign captures the whole matrix.
# Add a mode here when the suite gains one.
readonly SUITE_MODES="isolation,concurrent"

usage() {
  echo "usage: $0 <iterations> {host|client} [--campaign <name>] [--config <file> [--peer <name>] | --peer-map <device=ip,...> | --peer-ips <csv> | --peer-host <hostname>]" >&2
  echo "  --campaign: subdirectory under perftest-results/ holding every run of" >&2
  echo "              this campaign. Must be identical on both machines." >&2
  echo "  results: perftest-results/<campaign>/{host,client}/run-NNN/" >&2
  echo "  --config: per-machine JSON topology file; --peer selects a peer profile" >&2
  echo "  --peer-map / --peer-ips / --peer-host: direct peer selection options" >&2
}

iterations=""
role=""
campaign=""
peer_ips=""
peer_host=""
peer_map=""
peer_config=""
peer_name=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --campaign)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      campaign="$2"
      shift
      ;;
    --peer-ips)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      peer_ips="$2"
      shift
      ;;
    --peer-map)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      peer_map="$2"
      shift
      ;;
    --peer-host)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      peer_host="$2"
      shift
      ;;
    --config)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      [ -n "$2" ] || { echo "error: --config requires a non-empty file path" >&2; exit 2; }
      peer_config="$2"
      shift
      ;;
    --peer)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      [ -n "$2" ] || { echo "error: --peer requires a non-empty peer name" >&2; exit 2; }
      peer_name="$2"
      shift
      ;;
    *)
      if [ -z "$iterations" ] && [[ "$1" =~ ^[0-9]+$ ]]; then
        iterations="$1"
      elif [ -z "$role" ] && { [ "$1" = "host" ] || [ "$1" = "client" ]; }; then
        role="$1"
      else
        usage
        exit 2
      fi
      ;;
  esac
  shift
done

if [ -z "$iterations" ] || [ -z "$role" ]; then
  usage
  exit 2
fi

if [ -n "$peer_config" ]; then
  [ -z "$peer_ips" ] && [ -z "$peer_map" ] && [ -z "$peer_host" ] \
    || { echo "error: --config cannot be combined with direct peer options" >&2; exit 2; }
elif [ -n "$peer_name" ]; then
  echo "error: --peer requires --config" >&2
  exit 2
fi

# Force base 10 so a value like "08" is 8, not an octal literal.
iteration_count=$((10#$iterations))
if [ "$iteration_count" -lt 1 ]; then
  echo "error: iterations must be at least 1" >&2
  exit 2
fi

if [ -z "$campaign" ]; then
  campaign="$(date +%Y-%m-%d)"
  echo "campaign: none given, defaulting to '$campaign'"
fi
if [[ ! "$campaign" =~ $CAMPAIGN_NAME_PATTERN ]]; then
  echo "error: invalid campaign name '$campaign' (letters, digits, '.', '_', '-' only)" >&2
  exit 2
fi

[ -x "$SUITE_SCRIPT" ] || { echo "error: suite not found: $SUITE_SCRIPT" >&2; exit 1; }

for ((run_index = 1; run_index <= iteration_count; run_index++)); do
  run_name=$(printf '%s/run-%0*d' "$campaign" "$RUN_INDEX_WIDTH" "$run_index")
  echo
  echo "== campaign '$campaign' run $run_index/$iteration_count ($role) =="
  suite_arguments=("$role" --run-name "$run_name" --modes "$SUITE_MODES")
  if [ -n "$peer_config" ]; then
    suite_arguments+=(--config "$peer_config")
  fi
  if [ -n "$peer_name" ]; then
    suite_arguments+=(--peer "$peer_name")
  fi
  if [ -n "$peer_ips" ]; then
    suite_arguments+=(--peer-ips "$peer_ips")
  fi
  if [ -n "$peer_map" ]; then
    suite_arguments+=(--peer-map "$peer_map")
  fi
  if [ -n "$peer_host" ]; then
    suite_arguments+=(--peer-host "$peer_host")
  fi
  "$SUITE_SCRIPT" "${suite_arguments[@]}"
done

echo
echo "campaign complete: $campaign ($iteration_count run(s), $role)"
