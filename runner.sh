#!/usr/bin/env bash
set -euo pipefail

# Runs a campaign of N perftest-suite runs, all filed under one campaign
# directory. Start the HOST side first, then the CLIENT side, with the same
# --campaign name on both machines; run directories are deterministic
# (<campaign>/run-001, run-002, ...), so the two machines' directories match
# exactly and differ only in the host/ vs client/ role subdirectory.
#
#   ./runner.sh 5 host --campaign cables-oct06     # on cisc-diya
#   ./runner.sh 5 client --campaign cables-oct06   # on cisc-hoedus
#
# Pass --peer-host (or --peer-ips, or the PEER_HOST / PEER_IPS env vars) so the
# campaign works on any addressing scheme; see perftest-suite.sh. The campaign
# stops on the first failed run.

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SUITE_SCRIPT="$SCRIPT_DIR/perftest-suite.sh"
readonly CAMPAIGN_NAME_PATTERN='^[A-Za-z0-9][A-Za-z0-9._-]*$'
readonly RUN_INDEX_WIDTH=3
# Every mode the suite supports, so one campaign captures the whole matrix.
# Add a mode here when the suite gains one.
readonly SUITE_MODES="isolation,concurrent"

usage() {
  echo "usage: $0 <iterations> {host|client} [--campaign <name>] [--peer-ips <csv> | --peer-host <hostname>]" >&2
  echo "  --campaign: subdirectory under perftest-results/ holding every run of" >&2
  echo "              this campaign. Must be identical on both machines." >&2
  echo "  --peer-ips / --peer-host: peer addresses or hostname, forwarded to the" >&2
  echo "              suite (see perftest-suite.sh); give one of the two" >&2
}

iterations=""
role=""
campaign=""
peer_ips=""
peer_host=""
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
    --peer-host)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      peer_host="$2"
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
  if [ -n "$peer_ips" ]; then
    suite_arguments+=(--peer-ips "$peer_ips")
  fi
  if [ -n "$peer_host" ]; then
    suite_arguments+=(--peer-host "$peer_host")
  fi
  "$SUITE_SCRIPT" "${suite_arguments[@]}"
done

echo
echo "campaign complete: $campaign ($iteration_count run(s), $role)"
