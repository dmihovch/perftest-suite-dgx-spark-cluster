#!/usr/bin/env bash
set -uo pipefail

# ---------------------------------------------------------------------------
# perftest-suite.sh - pairwise RDMA perftest matrix for DGX Spark systems. Every
# RDMA device that is UP with an IPv4 address is tested.
#
#   ./perftest-suite.sh host --config spark-a.json --peer spark-b --run-name <campaign>/run-001
#   ./perftest-suite.sh client --config spark-b.json --peer spark-a --run-name <campaign>/run-001
#
#   --run-name <campaign>/<run-id>  shared identifier; pass the SAME value on
#                      both machines to store results under matching role dirs
#   --notes <file>     copy a free-form notes sheet into the run directory
#
# The peer's addresses come from --config (a machine-specific JSON file),
# --peer-map (explicit per-device map),
# --peer-ips (addresses matched to local devices by subnet), or --peer-host
# (a hostname resolved through /etc/hosts or DNS). Devices are discovered at
# runtime, so any RDMA device that is UP with an IPv4 address is tested.
#
#
#
#
# Output per run (see README.md for the full schema):
#
#   <results-root>/<campaign>/<role>/<run-id>/
#     environment/                  hardware and link-state capture
#     commands.txt                  exact command and peer endpoint for this role
#     results.csv                   parsed perftest tables, GB/s and usec
#     <test>[_bidi]_<device>.log    raw perftest output, never discarded
#     notes.<name>                  the --notes file, if one was given
#   <results-root>/<campaign>/<role>-logs/<run-id>.log
#                                  run timeline, process status, and retries
# ---------------------------------------------------------------------------

# `set -e` is deliberately not used: environment probes (ethtool, lspci,
# dpkg-query, ...) are best-effort and record what is available instead of
# aborting the run. Failure points that matter (directory creation, CSV
# writes, invalid input) are checked explicitly.

readonly RESULTS_ROOT_DEFAULT="./perftest-results"
readonly BASE_PORT_DEFAULT=18515
readonly CONNECTION_TYPE_DEFAULT="RC"
readonly LATENCY_ITERATIONS_DEFAULT=15000
readonly BANDWIDTH_ITERATIONS_DEFAULT=15000
readonly RETRIES_DEFAULT=3
readonly INITIAL_DELAY_DEFAULT=3
readonly ERROR_LOG_TAIL_LINES=12
# Bounds a perftest process whose peer never connects, so one failed client
# cannot stall a campaign (the concurrent pass waits on every device at once).
readonly TEST_TIMEOUT_DEFAULT=120
# Test modes. isolation runs one device at a time; concurrent runs every
# discovered device at once so the aggregate and any shared bottleneck show up.
# The default runs both: isolation gives each device's own ceiling, concurrent
# saturates the fabric so a run reports the link's true aggregate speed.
readonly ISOLATION_MODE="isolation"
readonly CONCURRENT_MODE="concurrent"
readonly MODES_DEFAULT="$ISOLATION_MODE,$CONCURRENT_MODE"
# Pair run identifiers are <campaign>/run-NNN. Campaign segments start with
# an alphanumeric character and contain only [A-Za-z0-9._-], preventing
# absolute paths, '..' traversal, and spaces.
readonly RUN_NAME_PATTERN='^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*/run-[0-9]+$'
# perftest --report_gbits prints decimal Gbit/s; GB/s = Gbit/s / 8.
readonly GBITS_PER_GBYTE=8

readonly LATENCY_TESTS=(ib_send_lat ib_write_lat ib_read_lat)
readonly BANDWIDTH_TESTS=(ib_send_bw ib_write_bw ib_read_bw)
readonly BANDWIDTH_BIDI_TESTS=("${BANDWIDTH_TESTS[@]}")
readonly SIZE_SWEEP_TESTS=("${LATENCY_TESTS[@]}" "${BANDWIDTH_TESTS[@]}")
readonly SIZE_SWEEP_FLAG="-a"

readonly CSV_HEADER="test,device,role,mode,bidi,size_bytes,bw_peak_GBps,bw_avg_GBps,msg_rate_Mpps,lat_min_usec,lat_max_usec,lat_typical_usec,lat_avg_usec,lat_stdev_usec,lat_p99_usec,lat_p999_usec,error"

declare -A test_start_seconds_by_pid=()
declare -A test_details_by_pid=()
event_log_file=""

usage() {
  echo "usage: $0 {host|client} [--run-name <name>] [--notes <file>] [--modes <list>] [--config <file> [--peer <name>] | --peer-map <device=ip,...> | --peer-ips <csv> | --peer-host <hostname>]" >&2
  echo "  --modes: comma-separated subset of $ISOLATION_MODE,$CONCURRENT_MODE (default $MODES_DEFAULT)" >&2
  echo "  --config: JSON file for this machine; --peer selects a peer profile (omittable when there is one peer)" >&2
  echo "  --peer-map: comma-separated local RDMA device=peer IPv4 address pairs; use for shared-subnet or routed fabrics" >&2
  echo "  --peer-ips: comma-separated peer addresses, matched to local devices by subnet" >&2
  echo "  --peer-host: peer hostname, resolved via /etc/hosts or DNS" >&2
}

fail() {
  echo "error: $*" >&2
  exit 1
}

log_event() {
  local event="$*"
  local timestamp
  timestamp=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  printf '%s %s role=%s\n' "$timestamp" "$event" "$role" \
    >> "$event_log_file" || fail "cannot write $event_log_file"
}

log_test_output_tail() {
  local log_file="$1" line quoted_line quoted_log_file
  printf -v quoted_log_file '%q' "$log_file"
  while IFS= read -r line; do
    printf -v quoted_line '%q' "$line"
    log_event "perftest_output line=$quoted_line raw_log=$quoted_log_file"
  done < <(tail -n "$ERROR_LOG_TAIL_LINES" "$log_file")
}

# True when the argument is a dotted-quad IPv4 address with octets in 0..255.
is_valid_ipv4() {
  local address="$1" octet
  [[ "$address" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
  for octet in ${address//./ }; do
    (( 10#$octet <= 255 )) || return 1
  done
}

# Convert an IPv4 address to a 32-bit integer.
ipv4_to_int() {
  local address="$1" octet1 octet2 octet3 octet4
  IFS='.' read -r octet1 octet2 octet3 octet4 <<< "$address"
  echo $(( (10#$octet1 << 24) + (10#$octet2 << 16) + (10#$octet3 << 8) + 10#$octet4 ))
}

# Print the network prefix of address/prefix as a 32-bit integer.
ipv4_network_int() {
  local address="$1" prefix="$2"
  local mask
  if [ "$prefix" -ge 32 ]; then
    mask=4294967295
  elif [ "$prefix" -le 0 ]; then
    mask=0
  else
    mask=$(( ((1 << prefix) - 1) << (32 - prefix) ))
  fi
  echo $(( $(ipv4_to_int "$address") & mask ))
}

# True when address lies in the subnet of network_ip/prefix.
ipv4_in_subnet() {
  local address="$1" network_ip="$2" prefix="$3"
  [ "$(ipv4_network_int "$address" "$prefix")" -eq "$(ipv4_network_int "$network_ip" "$prefix")" ]
}

# Print every IPv4 address that resolves for a hostname, one per line,
# deduplicated. Resolution goes through /etc/hosts or DNS (getent ahostsv4).
resolve_peer_host() {
  local hostname="$1" address
  getent ahostsv4 "$hostname" 2>/dev/null \
    | awk '{print $1}' | sort -u \
    | while read -r address; do
        if is_valid_ipv4 "$address"; then
          echo "$address"
        fi
      done
}

# Load the selected peer profile from a machine-specific JSON topology file.
load_peer_config() {
  local -a configured_peers=()
  local configured_peer

  command -v jq >/dev/null 2>&1 || fail "--config requires jq"
  [ -f "$peer_config_file" ] && [ -r "$peer_config_file" ] \
    || fail "peer config is not a readable file: $peer_config_file"
  jq -e -s '
    length == 1 and
    (.[0] |
      type == "object" and
      .schema_version == 1 and
      (.node | type == "string" and length > 0) and
      (.peers | type == "object" and length > 0) and
      all(.peers | to_entries[];
        (.key | type == "string" and length > 0) and
        (.value | type == "object" and length > 0 and
          all(to_entries[];
            (.key | test("^[A-Za-z0-9_.:-]+$")) and
            (.value | type == "string" and test("^[0-9]{1,3}(\\.[0-9]{1,3}){3}$"))))
      )
    )
  ' "$peer_config_file" >/dev/null 2>&1 \
    || fail "invalid peer config '$peer_config_file'; expected schema_version, node, and peer device-to-IPv4 maps"

  config_node=$(jq -r '.node' "$peer_config_file")
  while IFS= read -r configured_peer; do
    configured_peers+=("$configured_peer")
  done < <(jq -r '.peers | keys[]' "$peer_config_file")

  if [ -z "$peer_name" ]; then
    [ "${#configured_peers[@]}" -eq 1 ] \
      || fail "peer config contains ${#configured_peers[@]} peers; select one with --peer"
    peer_name="${configured_peers[0]}"
  elif ! jq -e --arg peer "$peer_name" '.peers | has($peer)' "$peer_config_file" >/dev/null; then
    fail "peer '$peer_name' is not defined in '$peer_config_file'"
  fi
  [ "$peer_name" != "$config_node" ] || fail "peer '$peer_name' is the local node in '$peer_config_file'"

  peer_map_csv=$(jq -r --arg peer "$peer_name" \
    '.peers[$peer] | to_entries | map(.key + "=" + .value) | join(",")' \
    "$peer_config_file")
  [ -n "$peer_map_csv" ] || fail "peer '$peer_name' has no device mappings in '$peer_config_file'"
}

# Fill peer_ips[] from an explicit device map, subnet-matched addresses, or a
# resolved peer hostname. A device map is required when several local devices
# share a subnet or peer addresses are reachable through routed paths.
assign_peer_ips() {
  peer_ips=()
  local -a given_peers=()
  local -a mapped_devices=()
  local -a mapped_ips=()
  local peer_ip index match resolved entry mapped_device mapped_ip known_device previous_index

  local peer_source_count=0
  [ -n "$peer_map_csv" ] && ((peer_source_count += 1))
  [ -n "$peer_ips_csv" ] && ((peer_source_count += 1))
  [ -n "$peer_host" ] && ((peer_source_count += 1))
  if [ "$peer_source_count" -gt 1 ]; then
    fail "give only one of --peer-map, --peer-ips, or --peer-host"
  fi

  if [ -n "$peer_map_csv" ]; then
    IFS=',' read -r -a given_peers <<< "$peer_map_csv"
    if [ "${#given_peers[@]}" -ne "${#devices[@]}" ]; then
      fail "PEER_MAP has ${#given_peers[@]} mapping(s) but ${#devices[@]} device(s) were discovered"
    fi
    for entry in "${given_peers[@]}"; do
      [[ "$entry" == *=* ]] || fail "invalid peer mapping '$entry'; expected local-device=peer-ip"
      mapped_device="${entry%%=*}"
      mapped_ip="${entry#*=}"
      [ -n "$mapped_device" ] || fail "invalid peer mapping '$entry': local device is empty"
      is_valid_ipv4 "$mapped_ip" || fail "invalid peer IP '$mapped_ip' in --peer-map/PEER_MAP"
      known_device=0
      for index in "${!devices[@]}"; do
        if [ "${devices[$index]}" = "$mapped_device" ]; then
          known_device=1
          break
        fi
      done
      [ "$known_device" -eq 1 ] || fail "unknown local RDMA device '$mapped_device' in --peer-map/PEER_MAP"
      for previous_index in "${!mapped_devices[@]}"; do
        [ "${mapped_devices[$previous_index]}" != "$mapped_device" ] \
          || fail "duplicate local RDMA device '$mapped_device' in --peer-map/PEER_MAP"
      done
      mapped_devices+=("$mapped_device")
      mapped_ips+=("$mapped_ip")
    done
    for index in "${!devices[@]}"; do
      match=""
      for previous_index in "${!mapped_devices[@]}"; do
        if [ "${mapped_devices[$previous_index]}" = "${devices[$index]}" ]; then
          match="${mapped_ips[$previous_index]}"
          break
        fi
      done
      [ -n "$match" ] || fail "no peer mapping for local RDMA device '${devices[$index]}'"
      peer_ips+=("$match")
    done
    return
  fi

  if [ -n "$peer_ips_csv" ]; then
    IFS=',' read -r -a given_peers <<< "$peer_ips_csv"
    if [ "${#given_peers[@]}" -ne "${#devices[@]}" ]; then
      fail "PEER_IPS has ${#given_peers[@]} address(es) but ${#devices[@]} device(s) were discovered"
    fi
    for peer_ip in "${given_peers[@]}"; do
      is_valid_ipv4 "$peer_ip" || fail "invalid peer IP '$peer_ip' in --peer-ips/PEER_IPS"
    done
  elif [ -n "$peer_host" ]; then
    if ! command -v getent >/dev/null 2>&1; then
      fail "--peer-host requires getent (glibc); use --peer-ips or --peer-map instead"
    fi
    while read -r resolved; do
      given_peers+=("$resolved")
    done < <(resolve_peer_host "$peer_host")
    if [ "${#given_peers[@]}" -eq 0 ]; then
      fail "no IPv4 address found for peer host '$peer_host'; add it to /etc/hosts or DNS, or use --peer-ips/--peer-map"
    fi
  else
    fail "specify --peer-map, --peer-ips, or --peer-host; peer addresses are never inferred"
  fi

  for index in "${!devices[@]}"; do
    match=""
    for peer_ip in "${given_peers[@]}"; do
      if ipv4_in_subnet "$peer_ip" "${local_ips[$index]}" "${local_prefixes[$index]}"; then
        if [ -n "$match" ]; then
          fail "multiple peer addresses on the same subnet as ${local_ips[$index]}: $match and $peer_ip; use --peer-map to select one address per RDMA device"
        fi
        match="$peer_ip"
      fi
    done
    if [ -z "$match" ]; then
      fail "no peer address on the same subnet as ${local_ips[$index]}/${local_prefixes[$index]} (peer addresses: ${given_peers[*]})"
    fi
    peer_ips+=("$match")
  done
}

# Give both roles the same port slots for matching interface IP pairs, even if
# their RDMA device names or discovery order differ.
assign_port_indices() {
  port_indices=()
  local -a link_keys=()
  local -a sorted_link_keys=()
  local device_index local_ip_number peer_ip_number link_key previous_index sorted_index

  for device_index in "${!devices[@]}"; do
    for previous_index in "${!peer_ips[@]}"; do
      if (( previous_index < device_index )) && [ "${peer_ips[$previous_index]}" = "${peer_ips[$device_index]}" ]; then
        fail "peer IP '${peer_ips[$device_index]}' is mapped to multiple local devices; map each device to a distinct peer interface IP"
      fi
    done
    local_ip_number=$(ipv4_to_int "${local_ips[$device_index]}")
    peer_ip_number=$(ipv4_to_int "${peer_ips[$device_index]}")
    (( local_ip_number != peer_ip_number )) \
      || fail "local and peer IPs are identical for device '${devices[$device_index]}'"
    if (( local_ip_number < peer_ip_number )); then
      link_keys+=("$local_ip_number|$peer_ip_number")
    else
      link_keys+=("$peer_ip_number|$local_ip_number")
    fi
  done

  while IFS= read -r link_key; do
    sorted_link_keys+=("$link_key")
  done < <(printf '%s\n' "${link_keys[@]}" | LC_ALL=C sort)

  for ((device_index = 1; device_index < ${#sorted_link_keys[@]}; device_index++)); do
    [ "${sorted_link_keys[$device_index]}" != "${sorted_link_keys[$((device_index - 1))]}" ] \
      || fail "multiple RDMA devices resolve to the same local/peer IP pair; use unique interface IPs"
  done

  for link_key in "${link_keys[@]}"; do
    for sorted_index in "${!sorted_link_keys[@]}"; do
      if [ "${sorted_link_keys[$sorted_index]}" = "$link_key" ]; then
        port_indices+=("$sorted_index")
        break
      fi
    done
  done

  # Keep sequential tests in the same link-pair order on both systems.
  ordered_device_indices=()
  for device_index in "${!devices[@]}"; do
    ordered_device_indices[${port_indices[$device_index]}]="$device_index"
  done
}

# Fill devices[]/local_ips[]/local_prefixes[]/netdevs[] with every RDMA device
# that is UP and has an IPv4 address. DOWN / address-less devices are logged and
# skipped. The prefix length is captured for peer-IP subnet matching.
discover_devices() {
  devices=()
  local_ips=()
  local_prefixes=()
  netdevs=()

  local device netdev address prefix state addr_info
  for device in /sys/class/infiniband/*; do
    device=$(basename "$device")

    # The RDMA device name (rocep1s0f0) differs from its netdev name
    # (enp1s0f0np0); the sysfs symlink bridges the two.
    netdev=$(find "/sys/class/infiniband/$device/device/net/" -mindepth 1 -maxdepth 1 \
             -printf '%f\n' 2>/dev/null | head -n1)
    if [ -z "$netdev" ]; then
      echo "skipping $device: no netdev found" >&2
      continue
    fi

    addr_info=$(ip -j addr show dev "$netdev" 2>/dev/null)
    address=$(echo "$addr_info" \
              | jq -r '.[0].addr_info[]? | select(.family=="inet") | .local' \
              | head -n1)
    prefix=$(echo "$addr_info" \
             | jq -r '.[0].addr_info[]? | select(.family=="inet") | .prefixlen' \
             | head -n1)
    state=$(echo "$addr_info" | jq -r '.[0].operstate // "UNKNOWN"')

    if [ -z "$address" ]; then
      echo "skipping $device ($netdev): no IPv4 address" >&2
      continue
    fi
    if [ "$state" != "UP" ]; then
      echo "skipping $device ($netdev, $address): state=$state" >&2
      continue
    fi

    devices+=("$device")
    local_ips+=("$address")
    local_prefixes+=("$prefix")
    netdevs+=("$netdev")
  done
}

validate_configuration() {
  [[ "$base_port" =~ ^[0-9]+$ ]] && (( 10#$base_port >= 1 && 10#$base_port <= 65535 )) \
    || fail "BASE_PORT must be an integer in 1..65535 (got '$base_port')"
  [[ "$latency_iterations" =~ ^[0-9]+$ ]] && (( 10#$latency_iterations >= 1 )) \
    || fail "LAT_ITERS must be a positive integer (got '$latency_iterations')"
  # perftest disables BW peak computation above 20000 iterations.
  [[ "$bandwidth_iterations" =~ ^[0-9]+$ ]] && (( 10#$bandwidth_iterations >= 1 && 10#$bandwidth_iterations <= 20000 )) \
    || fail "BW_ITERS must be an integer in 1..20000 (got '$bandwidth_iterations')"
  [[ "$retries" =~ ^[0-9]+$ ]] && (( 10#$retries >= 1 )) \
    || fail "RETRIES must be a positive integer (got '$retries')"
  [[ "$initial_delay" =~ ^[0-9]+$ ]] \
    || fail "INITIAL_DELAY must be a non-negative integer (got '$initial_delay')"
  [[ "$test_timeout" =~ ^[0-9]+$ ]] && (( 10#$test_timeout >= 1 )) \
    || fail "TEST_TIMEOUT must be a positive integer (got '$test_timeout')"
  case "$connection_type" in
    RC|UC|UD|XRC|DC) ;;
    *) fail "CONN must be one of RC, UC, UD, XRC, DC (got '$connection_type')" ;;
  esac
}

# UD has no read verbs, so read tests are skipped unless the connection type
# supports them.
should_skip_read_test() {
  local test="$1"
  [[ "$test" == ib_read_* ]] && [[ "$connection_type" != "RC" && "$connection_type" != "UC" ]]
}

# Build the ordered test list as "binary|extra_flag" entries. The order is fixed
# so both roles allocate the same port to each test on a paired interface.
build_test_matrix() {
  test_matrix=()
  local test
  for test in "${LATENCY_TESTS[@]}"; do
    if should_skip_read_test "$test"; then continue; fi
    test_matrix+=("$test|")
  done
  for test in "${BANDWIDTH_TESTS[@]}"; do
    if should_skip_read_test "$test"; then continue; fi
    test_matrix+=("$test|")
  done
  for test in "${BANDWIDTH_BIDI_TESTS[@]}"; do
    if should_skip_read_test "$test"; then continue; fi
    test_matrix+=("$test|-b")
  done
}

preflight() {
  local missing=""
  local binary
  for binary in "${LATENCY_TESTS[@]}" "${BANDWIDTH_TESTS[@]}"; do
    command -v "$binary" >/dev/null 2>&1 || missing="$missing $binary"
  done
  local tool
  for tool in ip jq ss timeout column; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
  done
  if [ -n "$missing" ]; then
    fail "missing required tools:$missing"
  fi

  local perftest_help
  perftest_help=$(ib_send_bw --help 2>&1 || true)
  [[ "$perftest_help" == *"--bind_source_ip"* ]] \
    || fail "installed perftest does not support --bind_source_ip; install a build that supports source-IP binding"

  local sweep_binary sweep_help
  for sweep_binary in "${SIZE_SWEEP_TESTS[@]}"; do
    sweep_help=$("$sweep_binary" --help 2>&1 || true)
    [[ "$sweep_help" == *"--all"* ]] \
      || fail "installed $sweep_binary does not support the required -a size sweep"
  done

  # Optional: their absence degrades the environment capture, not the tests.
  for tool in ethtool lspci ibv_devinfo; do
    command -v "$tool" >/dev/null 2>&1 \
      || echo "warning: $tool not installed; environment capture will be incomplete" >&2
  done

  local device
  for device in "${devices[@]}"; do
    [ -e "/sys/class/infiniband/$device" ] || fail "device disappeared: $device"
  done

  local port_count=$(( ${#devices[@]} * tests_per_device ))
  local last_port=$(( base_port + port_count - 1 ))
  [ "$last_port" -le 65535 ] \
    || fail "BASE_PORT $base_port plus $port_count test ports exceeds 65535"
  local port
  for (( port = base_port; port < base_port + port_count; port++ )); do
    if ss -ltn 2>/dev/null | grep -q ":$port "; then
      fail "port $port already in use; kill stale ib_* servers first"
    fi
  done
}

# ---------------------------------------------------------------------------
# Environment capture: link state and hardware, so every run carries the
# information needed to interpret its numbers (link speed, PCIe width/speed,
# MTU, driver/firmware/software versions). Raw tool output is kept verbatim
# alongside a parsed JSON summary. Missing tools/fields record empty strings.
# ---------------------------------------------------------------------------
capture_environment() {
  local out_dir="$1"
  local environment_dir="$out_dir/environment"
  mkdir -p "$environment_dir" || fail "cannot create $environment_dir"

  capture_system_environment "$environment_dir"

  echo "link state (device | local -> peer | link speed | pcie | mtu):"
  local index
  for index in "${!devices[@]}"; do
    capture_device_environment "$environment_dir" \
      "${devices[$index]}" "${netdevs[$index]}" "${local_ips[$index]}" "${peer_ips[$index]}"
  done
}

capture_system_environment() {
  local environment_dir="$1"

  local hostname_value kernel distro perftest_version rdma_core_version captured_at
  hostname_value=$(hostname)
  kernel=$(uname -r)
  distro=$(lsb_release -ds 2>/dev/null || true)
  perftest_version=$(dpkg-query -W -f='${Version}' perftest 2>/dev/null || true)
  rdma_core_version=$(dpkg-query -W -f='${Version}' rdma-core 2>/dev/null || true)
  captured_at=$(date -Is)

  jq -n \
    --arg hostname "$hostname_value" \
    --arg role "$role" \
    --arg kernel "$kernel" \
    --arg distro "$distro" \
    --arg perftest_version "$perftest_version" \
    --arg rdma_core_version "$rdma_core_version" \
    --arg captured_at "$captured_at" \
    '{hostname: $hostname,
      role: $role,
      kernel: $kernel,
      distro: $distro,
      perftest_version: $perftest_version,
      rdma_core_version: $rdma_core_version,
      captured_at: $captured_at}' \
    > "$environment_dir/system.json" || fail "cannot write $environment_dir/system.json"
}

capture_device_environment() {
  local environment_dir="$1" device="$2" netdev="$3" local_ip="$4" peer_ip="$5"
  local device_dir="$environment_dir/$device"
  mkdir -p "$device_dir" || fail "cannot create $device_dir"

  # Raw tool dumps: what cannot be parsed now can still be read later.
  if command -v ethtool >/dev/null 2>&1; then
    ethtool "$netdev" > "$device_dir/ethtool.txt" 2>&1 || true
    ethtool -i "$netdev" > "$device_dir/ethtool-i.txt" 2>&1 || true
  fi
  if command -v lspci >/dev/null 2>&1; then
    local pci_address
    pci_address=$(basename "$(readlink -f "/sys/class/infiniband/$device/device")" 2>/dev/null || true)
    if [[ "$pci_address" =~ ^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$ ]]; then
      lspci -vv -s "$pci_address" > "$device_dir/lspci.txt" 2>&1 || true
    fi
  fi
  if command -v ibv_devinfo >/dev/null 2>&1; then
    ibv_devinfo -d "$device" -v > "$device_dir/ibv_devinfo.txt" 2>&1 || true
  fi

  local link_speed_mbps="" autoneg="" link_detected="" mtu="" pcie_speed="" pcie_width=""
  local driver="" driver_version="" firmware_version=""
  local active_width="" active_speed_gbps=""

  if [ -f "$device_dir/ethtool.txt" ]; then
    link_speed_mbps=$(awk '/^[[:space:]]*Speed:/ { gsub(/[^0-9]/, "", $2); if ($2 ~ /^[0-9]+$/) print $2; exit }' "$device_dir/ethtool.txt")
    autoneg=$(awk '/^[[:space:]]*Auto-negotiation:/ { print $2; exit }' "$device_dir/ethtool.txt")
    link_detected=$(awk '/^[[:space:]]*Link detected:/ { print $3; exit }' "$device_dir/ethtool.txt")
  fi
  if [ -f "$device_dir/ethtool-i.txt" ]; then
    driver=$(awk '/^driver:/ { sub(/^driver:[[:space:]]*/, ""); print; exit }' "$device_dir/ethtool-i.txt")
    driver_version=$(awk '/^version:/ { sub(/^version:[[:space:]]*/, ""); print; exit }' "$device_dir/ethtool-i.txt")
    firmware_version=$(awk '/^firmware-version:/ { sub(/^firmware-version:[[:space:]]*/, ""); print; exit }' "$device_dir/ethtool-i.txt")
  fi
  if command -v ip >/dev/null 2>&1; then
    mtu=$(ip -j link show dev "$netdev" 2>/dev/null | jq -r '.[0].mtu // empty' 2>/dev/null || true)
  fi
  # ibv_devinfo reports the device's own port rate as active_speed x
  # active_width; the plot uses it as the per-device reference line. A capture
  # taken before the -d fix holds every device, so the block for this device is
  # selected by its hca_id rather than taking the first match.
  if [ -f "$device_dir/ibv_devinfo.txt" ]; then
    active_width=$(awk -v target="$device" '
      /^hca_id:/ { in_device = ($2 == target); next }
      in_device && /^[[:space:]]*active_width:/ && match($0, /[0-9]+X/) {
        width = substr($0, RSTART, RLENGTH)
        sub(/X$/, "", width)
        print width
        exit
      }' "$device_dir/ibv_devinfo.txt")
    active_speed_gbps=$(awk -v target="$device" '
      /^hca_id:/ { in_device = ($2 == target); next }
      in_device && /^[[:space:]]*active_speed:/ && match($0, /[0-9.]+ Gbps/) {
        speed = substr($0, RSTART, RLENGTH)
        sub(/ Gbps$/, "", speed)
        print speed
        exit
      }' "$device_dir/ibv_devinfo.txt")
  fi
  if [ -f "$device_dir/lspci.txt" ]; then
    local pcie_line
    pcie_line=$(awk '
      /^[[:space:]]*LnkSta:/ {
        if (match($0, /Speed [0-9.]+GT\/s/)) {
          pcie_speed = substr($0, RSTART, RLENGTH)
          sub(/^Speed /, "", pcie_speed)
        }
        if (match($0, /Width x[0-9]+/)) {
          pcie_width = substr($0, RSTART, RLENGTH)
          sub(/^Width /, "", pcie_width)
        }
        print pcie_speed "|" pcie_width
        exit
      }' "$device_dir/lspci.txt")
    pcie_speed=${pcie_line%%|*}
    pcie_width=${pcie_line##*|}
  fi
  # lspci needs root to read the link capabilities; sysfs exposes the same
  # negotiated speed and width without it, so the PCIe ceiling is never blank.
  local pcie_sysfs="/sys/class/infiniband/$device/device"
  if [ -z "$pcie_speed" ] && [ -r "$pcie_sysfs/current_link_speed" ]; then
    pcie_speed=$(cat "$pcie_sysfs/current_link_speed" 2>/dev/null || true)
  fi
  if [ -z "$pcie_width" ] && [ -r "$pcie_sysfs/current_link_width" ]; then
    pcie_width=$(cat "$pcie_sysfs/current_link_width" 2>/dev/null || true)
  fi

  jq -n \
    --arg device "$device" \
    --arg netdev "$netdev" \
    --arg local_ip "$local_ip" \
    --arg peer_ip "$peer_ip" \
    --argjson mtu "${mtu:-null}" \
    --argjson link_speed_mbps "${link_speed_mbps:-null}" \
    --arg autoneg "$autoneg" \
    --arg link_detected "$link_detected" \
    --arg pcie_speed "$pcie_speed" \
    --arg pcie_width "$pcie_width" \
    --argjson active_width "${active_width:-null}" \
    --argjson active_speed_gbps "${active_speed_gbps:-null}" \
    --arg driver "$driver" \
    --arg driver_version "$driver_version" \
    --arg firmware_version "$firmware_version" \
    '{device: $device,
      netdev: $netdev,
      local_ip: $local_ip,
      peer_ip: $peer_ip,
      mtu: $mtu,
      link_speed_mbps: $link_speed_mbps,
      autoneg: $autoneg,
      link_detected: $link_detected,
      pcie_speed: $pcie_speed,
      pcie_width: $pcie_width,
      active_width: $active_width,
      active_speed_gbps: $active_speed_gbps,
      driver: $driver,
      driver_version: $driver_version,
      firmware_version: $firmware_version}' \
    > "$device_dir/link.json" || fail "cannot write $device_dir/link.json"

  local speed_display="${link_speed_mbps:+${link_speed_mbps}Mb/s}"
  printf '  %-15s | %s -> %s | %s | %s | mtu %s\n' \
    "$device" "$local_ip" "$peer_ip" "${speed_display:-unknown}" \
    "${pcie_speed:-unknown} ${pcie_width:-unknown}" "${mtu:-unknown}"
}

# ---------------------------------------------------------------------------
# perftest table parsing. perftest prints (with --report_gbits):
#   *_bw : size, iters, BW peak[Gb/sec], BW average[Gb/sec], MsgRate[Mpps]
#   *_lat: size, iters, t_min, t_max, t_typical, [t_avg, t_stdev, p99, p999]
# Only rows whose numeric columns all parse as numbers are emitted; header and
# trailer lines are skipped by construction. An empty result is reported by
# the caller as a failure row, so bad output is never silently absorbed.
# ---------------------------------------------------------------------------
parse_perftest_log() {
  local log_file="$1" kind="$2" binary="$3" device="$4" mode="$5" bidi="$6"

  if [ "$kind" = "bw" ]; then
    awk -v test="$binary" -v device="$device" -v role="$role" -v mode="$mode" \
        -v bidi="$bidi" -v divisor="$GBITS_PER_GBYTE" '
      function is_numeric(value) {
        return value ~ /^[0-9]+(\.[0-9]+)?$/
      }
      $1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ &&
        is_numeric($3) && is_numeric($4) && is_numeric($5) {
        printf "%s,%s,%s,%s,%s,%s,%.3f,%.3f,%.3f,,,,,,,,\n",
          test, device, role, mode, bidi, $1, $3 / divisor, $4 / divisor, $5
      }' "$log_file"
  else
    awk -v test="$binary" -v device="$device" -v role="$role" -v mode="$mode" \
        -v bidi="$bidi" '
      function is_numeric(value) {
        return value ~ /^[0-9]+(\.[0-9]+)?$/
      }
      $1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ &&
        is_numeric($3) && is_numeric($4) && is_numeric($5) {
        average = (NF >= 6 && is_numeric($6)) ? $6 : ""
        stdev = (NF >= 7 && is_numeric($7)) ? $7 : ""
        p99 = (NF >= 8 && is_numeric($8)) ? $8 : ""
        p999 = (NF >= 9 && is_numeric($9)) ? $9 : ""
        printf "%s,%s,%s,%s,%s,%s,,,,%s,%s,%s,%s,%s,%s,%s,\n",
          test, device, role, mode, bidi, $1, $3, $4, $5, average, stdev, p99, p999
      }' "$log_file"
  fi
}

append_error_row() {
  local binary="$1" device="$2" mode="$3" bidi="$4" message="$5"
  local sanitized_message=${message//,/;}
  sanitized_message=${sanitized_message//$'\n'/ }

  # Columns 1..5 identify the test, 6..16 hold no measurement, 17 is the error.
  local -a row_fields=("$binary" "$device" "$role" "$mode" "$bidi"
                      "" "" "" "" "" "" "" "" "" "" ""
                      "$sanitized_message")
  local row="" separator="" field
  for field in "${row_fields[@]}"; do
    row+="$separator$field"
    separator=","
  done
  printf '%s\n' "$row" >> "$csv_file"
}

# Build the perftest argv and recorded command for this role. The selected local
# address keeps connection setup on the interface being tested.
build_test_commands() {
  local binary="$1" extra_flag="$2" device="$3" local_ip="$4" peer_ip="$5" port="$6"

  local iterations
  if [[ "$binary" == *_lat ]]; then
    iterations="$latency_iterations"
  else
    iterations="$bandwidth_iterations"
  fi

  local -a base_arguments=(-d "$device" --bind_source_ip "$local_ip" -i 1 -F -p "$port" -c "$connection_type" -n "$iterations" --report_gbits)
  if [ -n "$extra_flag" ]; then
    base_arguments+=("$extra_flag")
  fi
  local sweep_test
  for sweep_test in "${SIZE_SWEEP_TESTS[@]}"; do
    if [ "$binary" = "$sweep_test" ]; then
      base_arguments+=("$SIZE_SWEEP_FLAG")
      break
    fi
  done

  test_base_arguments=("${base_arguments[@]}")
  test_local_command="$binary ${base_arguments[*]}"
  [ "$role" = client ] && test_local_command+=" $peer_ip"
}

# Launch one perftest process in the background, writing its log header first.
# Sets test_pid. The timeout bounds a process whose peer never connects, so a
# failed client cannot stall the campaign.
start_test_process() {
  local binary="$1" extra_flag="$2" device="$3" local_ip="$4" server_ip="$5" port="$6" mode="$7" attempt="${8:-1}"
  local quoted_command quoted_log_file

  build_test_commands "$binary" "$extra_flag" "$device" "$local_ip" "$server_ip" "$port"

  local tag="${binary}${extra_flag:+_bidi}_${device}_${mode}"
  local log_file="$out_dir/$tag.log"
  printf -v quoted_command '%q' "$test_local_command"
  printf -v quoted_log_file '%q' "$log_file"

  # Header records the command for this local role. The peer may use a different
  # RDMA device name, so its exact command is recorded by the peer's run.
  if [ ! -f "$log_file" ]; then
    {
      echo "# test   : ${binary}${extra_flag:+ ($extra_flag)}"
      echo "# device : $device"
      echo "# role   : $role"
      echo "# local  : $local_ip"
      echo "# peer   : $server_ip"
      echo "# port   : $port"
      echo "# command: $test_local_command"
      echo "# ---"
    } > "$log_file" || fail "cannot write $log_file"
  fi

  echo ">>> [$role] $test_local_command"

  local -a local_arguments=("${test_base_arguments[@]}")
  [ "$role" = client ] && local_arguments+=("$server_ip")
  timeout "$test_timeout" "$binary" "${local_arguments[@]}" >> "$log_file" 2>&1 &
  test_pid=$!
  test_start_seconds_by_pid[$test_pid]=$SECONDS
  test_details_by_pid[$test_pid]="tag=$tag mode=$mode attempt=$attempt device=$device port=$port"
  log_event "process_start tag=$tag mode=$mode attempt=$attempt pid=$test_pid local_ip=$local_ip peer_ip=$server_ip port=$port command=$quoted_command raw_log=$quoted_log_file"
}

# Wait for a launched process and set test_exit_code to its status.
wait_test_process() {
  local pid="$1"
  local started_at="${test_start_seconds_by_pid[$pid]:-$SECONDS}"
  local details="${test_details_by_pid[$pid]:-tag=unknown}"
  local elapsed_seconds
  test_exit_code=0
  wait "$pid" || test_exit_code=$?
  elapsed_seconds=$((SECONDS - started_at))
  log_event "process_exit $details pid=$pid exit_code=$test_exit_code elapsed_seconds=$elapsed_seconds"
  unset "test_start_seconds_by_pid[$pid]"
  unset "test_details_by_pid[$pid]"
}

# Record a finished test: parse its log into the CSV, or write an error row.
record_test_result() {
  local binary="$1" extra_flag="$2" device="$3" local_ip="$4" server_ip="$5" port="$6" mode="$7"

  build_test_commands "$binary" "$extra_flag" "$device" "$local_ip" "$server_ip" "$port"

  local tag="${binary}${extra_flag:+_bidi}_${device}_${mode}"
  local log_file="$out_dir/$tag.log"
  local quoted_log_file
  printf -v quoted_log_file '%q' "$log_file"
  local bidi="false"
  [ -n "$extra_flag" ] && bidi="true"
  local kind
  if [[ "$binary" == *_lat ]]; then
    kind="lat"
  else
    kind="bw"
  fi

  if [ "$test_exit_code" -ne 0 ]; then
    echo "!! $tag failed rc=$test_exit_code" >&2
    append_error_row "$binary" "$device" "$mode" "$bidi" "perftest rc=$test_exit_code"
    log_event "measurement_error tag=$tag exit_code=$test_exit_code reason=perftest_failure raw_log=$quoted_log_file"
    log_test_output_tail "$log_file"
    failed_test_count=$((failed_test_count + 1))
  else
    parse_perftest_log "$log_file" "$kind" "$binary" "$device" "$mode" "$bidi" > "$parse_tmp_file"
    if [ -s "$parse_tmp_file" ]; then
      local parsed_row_count
      parsed_row_count=$(wc -l < "$parse_tmp_file")
      cat "$parse_tmp_file" >> "$csv_file"
      log_event "measurement_recorded tag=$tag rows=$parsed_row_count raw_log=$quoted_log_file"
      rm -f "$parse_tmp_file"
    elif [ "$role" = "host" ] && [ "$binary" = "ib_read_lat" ]; then
      # The read-latency server succeeds without emitting the client table.
      log_event "measurement_recorded tag=$tag rows=0 reason=read_latency_server_has_no_table raw_log=$quoted_log_file"
      rm -f "$parse_tmp_file"
    else
      echo "!! $tag produced no parseable rows" >&2
      append_error_row "$binary" "$device" "$mode" "$bidi" "no parseable rows in perftest output"
      log_event "measurement_error tag=$tag reason=no_parseable_rows raw_log=$quoted_log_file"
      log_test_output_tail "$log_file"
      failed_test_count=$((failed_test_count + 1))
      rm -f "$parse_tmp_file"
    fi
  fi

  {
    echo "$tag"
    echo "  role   : $role"
    echo "  local  : $local_ip"
    echo "  peer   : $server_ip"
    echo "  command: $test_local_command"
  } >> "$commands_file"
}

# One device at a time, retrying a failed test. This is the isolation pass: it
# measures each device's own ceiling with no contention.
run_isolation_pass() {
  local device_index device local_ip server_ip test_index binary extra_flag port attempt
  for device_index in "${ordered_device_indices[@]}"; do
    device="${devices[$device_index]}"
    local_ip="${local_ips[$device_index]}"
    server_ip="${peer_ips[$device_index]}"
    for test_index in "${!test_matrix[@]}"; do
      IFS='|' read -r binary extra_flag <<< "${test_matrix[$test_index]}"
      port=$(( base_port + port_indices[device_index] * tests_per_device + test_index ))
      for attempt in $(seq 1 "$retries"); do
        start_test_process "$binary" "$extra_flag" "$device" "$local_ip" "$server_ip" "$port" "$ISOLATION_MODE" "$attempt"
        wait_test_process "$test_pid"
        [ "$test_exit_code" -eq 0 ] && break
        sleep 1
      done
      record_test_result "$binary" "$extra_flag" "$device" "$local_ip" "$server_ip" "$port" "$ISOLATION_MODE"
      # Make sure this test's server is gone before the next test reuses a port.
      pkill -f "^$binary " 2>/dev/null || true
      sleep 1
    done
  done
}

# Every device at once, one test at a time. This is the concurrent pass: the sum
# of the per-device rows is the aggregate the interconnect actually sustains,
# and any gap to the isolation pass exposes a shared bottleneck.
run_concurrent_pass() {
  local test_index binary extra_flag device_index device local_ip server_ip port attempt
  local -a pids=()
  for test_index in "${!test_matrix[@]}"; do
    IFS='|' read -r binary extra_flag <<< "${test_matrix[$test_index]}"
    pids=()
    for device_index in "${ordered_device_indices[@]}"; do
      device="${devices[$device_index]}"
      local_ip="${local_ips[$device_index]}"
      server_ip="${peer_ips[$device_index]}"
      port=$(( base_port + port_indices[device_index] * tests_per_device + test_index ))
      start_test_process "$binary" "$extra_flag" "$device" "$local_ip" "$server_ip" "$port" "$CONCURRENT_MODE" 1
      pids[$device_index]="$test_pid"
    done
    for device_index in "${ordered_device_indices[@]}"; do
      device="${devices[$device_index]}"
      local_ip="${local_ips[$device_index]}"
      server_ip="${peer_ips[$device_index]}"
      port=$(( base_port + port_indices[device_index] * tests_per_device + test_index ))
      wait_test_process "${pids[$device_index]}"
      # A failed client leaves its server blocked; retrying reconnects to it and
      # keeps the two roles in lockstep.
      for attempt in $(seq 1 "$retries"); do
        [ "$test_exit_code" -eq 0 ] && break
        sleep 1
        start_test_process "$binary" "$extra_flag" "$device" "$local_ip" "$server_ip" "$port" "$CONCURRENT_MODE" "$((attempt + 1))"
        wait_test_process "$test_pid"
      done
      record_test_result "$binary" "$extra_flag" "$device" "$local_ip" "$server_ip" "$port" "$CONCURRENT_MODE"
    done
    # Make sure this test's servers are gone before the next test reuses a port.
    pkill -f "^$binary " 2>/dev/null || true
    sleep 1
  done
}

print_results_summary() {
  echo
  echo "== results ($role) =="
  echo "metric,test,device,mode,bidi,value,at_size_bytes" | column -t -s','
  awk -F',' '
    NR == 1 { next }
    $17 != "" { next }
    {
      key = $1 "," $2 "," $4 "," $5
      if ($7 != "") {
        if ($7 + 0 > peak[key]) {
          peak[key] = $7 + 0
          peak_size[key] = $6
        }
      } else if ($12 != "") {
        if (typical[key] == "" || $12 + 0 < typical[key]) {
          typical[key] = $12 + 0
          typical_size[key] = $6
        }
      }
    }
    END {
      for (key in peak) {
        printf "BW,%s,%.3f,%s\n", key, peak[key], peak_size[key]
      }
      for (key in typical) {
        printf "LAT,%s,%.3f,%s\n", key, typical[key], typical_size[key]
      }
    }' "$csv_file" \
  | sort -t',' -k2,2 -k1,1 \
  | column -t -s','
  echo "results: $out_dir/results.csv"
}

# ---------------------------------------------------------------------------
# main flow
# ---------------------------------------------------------------------------
role=""
run_name=""
notes_file=""
peer_config_file=""
peer_name=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    host|client)
      role="$1"
      ;;
    --run-name)
      [ "$#" -ge 2 ] || fail "--run-name requires an argument"
      run_name="$2"
      shift
      ;;
    --notes)
      [ "$#" -ge 2 ] || fail "--notes requires an argument"
      notes_file="$2"
      shift
      ;;
    --modes)
      [ "$#" -ge 2 ] || fail "--modes requires an argument"
      modes_csv="$2"
      shift
      ;;
    --config)
      [ "$#" -ge 2 ] || fail "--config requires an argument"
      [ -n "$2" ] || fail "--config requires a non-empty file path"
      peer_config_file="$2"
      shift
      ;;
    --peer)
      [ "$#" -ge 2 ] || fail "--peer requires an argument"
      [ -n "$2" ] || fail "--peer requires a non-empty peer name"
      peer_name="$2"
      shift
      ;;
    --peer-ips)
      [ "$#" -ge 2 ] || fail "--peer-ips requires an argument"
      peer_ips_csv="$2"
      shift
      ;;
    --peer-map)
      [ "$#" -ge 2 ] || fail "--peer-map requires an argument"
      peer_map_csv="$2"
      shift
      ;;
    --peer-host)
      [ "$#" -ge 2 ] || fail "--peer-host requires an argument"
      peer_host="$2"
      shift
      ;;
    *)
      usage
      exit 2
      ;;
  esac
  shift
done

if [ -z "$role" ]; then
  usage
  exit 2
fi
if [ -n "$run_name" ] && [[ ! "$run_name" =~ $RUN_NAME_PATTERN ]]; then
  fail "invalid run name '$run_name': expected <campaign>/run-NNN using letters, digits, '.', '_' and '-' (no leading dot, no '..', no spaces)"
fi

results_root=${OUTDIR:-$RESULTS_ROOT_DEFAULT}
base_port=${BASE_PORT:-$BASE_PORT_DEFAULT}
connection_type=${CONN:-$CONNECTION_TYPE_DEFAULT}
latency_iterations=${LAT_ITERS:-$LATENCY_ITERATIONS_DEFAULT}
bandwidth_iterations=${BW_ITERS:-$BANDWIDTH_ITERATIONS_DEFAULT}   # keep <= 20000, else perftest disables BW peak
retries=${RETRIES:-$RETRIES_DEFAULT}
initial_delay=${INITIAL_DELAY:-$INITIAL_DELAY_DEFAULT}
test_timeout=${TEST_TIMEOUT:-$TEST_TIMEOUT_DEFAULT}
validate_configuration

# modes_csv stays unset unless --modes was given, so an explicit empty value
# fails the guard below instead of silently falling back to the default.
modes_csv="${modes_csv-${MODES:-$MODES_DEFAULT}}"
# Peer options also accept environment equivalents. One must be supplied so
# tests never rely on an address convention from a particular cluster.
peer_map_csv="${peer_map_csv-${PEER_MAP:-}}"
peer_ips_csv="${peer_ips_csv-${PEER_IPS:-}}"
peer_host="${peer_host-${PEER_HOST:-}}"
if [ -n "$peer_config_file" ]; then
  [ -z "$peer_map_csv" ] && [ -z "$peer_ips_csv" ] && [ -z "$peer_host" ] \
    || fail "--config cannot be combined with --peer-map, --peer-ips, --peer-host, or their environment variables"
  load_peer_config
elif [ -n "$peer_name" ]; then
  fail "--peer requires --config"
fi
modes=()
IFS=',' read -r -a modes <<< "$modes_csv"
if [ "${#modes[@]}" -eq 0 ]; then
  fail "no modes selected (--modes '$modes_csv')"
fi
for mode in "${modes[@]}"; do
  case "$mode" in
    "$ISOLATION_MODE"|"$CONCURRENT_MODE") ;;
    *) fail "unknown mode '$mode' (expected $ISOLATION_MODE or $CONCURRENT_MODE)" ;;
  esac
done

discover_devices
if [ "${#devices[@]}" -eq 0 ]; then
  fail "no UP RDMA devices with an IPv4 address were found"
fi

assign_peer_ips
assign_port_indices

build_test_matrix
tests_per_device=${#test_matrix[@]}

if [ -n "$run_name" ]; then
  campaign_name="${run_name%/*}"
  run_id="${run_name##*/}"
else
  # Ad-hoc runs use a timestamped campaign and a single numbered run.
  campaign_name="$(date +%Y%m%d-%H%M%S)"
  run_id="run-001"
fi
out_dir="$results_root/$campaign_name/$role/$run_id"
if [ -e "$out_dir" ]; then
  fail "output directory already exists: $out_dir (choose a new --run-name; existing data is never overwritten)"
fi
run_log_dir="$results_root/$campaign_name/$role-logs"
event_log_file="$run_log_dir/$run_id.log"
if [ -e "$event_log_file" ]; then
  fail "diagnostics log already exists: $event_log_file (choose a new --run-name; existing data is never overwritten)"
fi
mkdir -p "$run_log_dir" || fail "cannot create $run_log_dir"
: > "$event_log_file" || fail "cannot create $event_log_file"
log_event "run_start run_id=$run_id campaign=$campaign_name host=$(hostname) connection_type=$connection_type modes=${modes[*]} base_port=$base_port retries=$retries test_timeout_seconds=$test_timeout initial_delay_seconds=$initial_delay"
trap 'exit_status=$?; log_event "run_exit exit_code=$exit_status"; if [ -n "${parse_tmp_file:-}" ]; then rm -f "$parse_tmp_file"; fi' EXIT

log_event "preflight_start"
preflight
log_event "preflight_complete"
mkdir -p "$out_dir" || fail "cannot create $out_dir"
csv_file="$out_dir/results.csv"
commands_file="$out_dir/commands.txt"
parse_tmp_file="$out_dir/.parsing.tmp"
failed_test_count=0
printf -v quoted_csv_file '%q' "$csv_file"
printf -v quoted_commands_file '%q' "$commands_file"
printf -v quoted_environment_dir '%q' "$out_dir/environment"

echo "$CSV_HEADER" > "$csv_file" || fail "cannot write $csv_file"
: > "$commands_file" || fail "cannot create $commands_file"
log_event "result_files_created results_csv=$quoted_csv_file commands_file=$quoted_commands_file"

if [ -n "$notes_file" ]; then
  if [ ! -f "$notes_file" ]; then
    fail "notes file not found: $notes_file"
  fi
  cp "$notes_file" "$out_dir/notes.$(basename "$notes_file")" || fail "cannot copy notes file"
  echo "notes: $notes_file -> $out_dir/notes.$(basename "$notes_file")"
fi

echo "role=$role conn=$connection_type modes=${modes[*]} out=$out_dir"
echo "diagnostics_log=$event_log_file"
echo "discovered devices (device | local_ip | peer_ip):"
for index in "${!devices[@]}"; do
  echo "  ${devices[$index]} | ${local_ips[$index]} | ${peer_ips[$index]}"
  log_event "device_discovered name=${devices[$index]} local_ip=${local_ips[$index]} peer_ip=${peer_ips[$index]} port_slot=${port_indices[$index]}"
done

log_event "environment_capture_start directory=$quoted_environment_dir"
capture_environment "$out_dir"
log_event "environment_capture_complete directory=$quoted_environment_dir"

if [ "$role" = client ]; then
  echo "targets=${peer_ips[*]}; starting in ${initial_delay}s"
  log_event "initial_delay_start seconds=$initial_delay"
  sleep "$initial_delay"
  log_event "initial_delay_complete"
fi

for mode in "${modes[@]}"; do
  echo "== mode: $mode =="
  log_event "mode_start mode=$mode"
  case "$mode" in
    "$ISOLATION_MODE") run_isolation_pass ;;
    "$CONCURRENT_MODE") run_concurrent_pass ;;
  esac
  log_event "mode_complete mode=$mode failed_test_count=$failed_test_count"
done

print_results_summary
if [ "$failed_test_count" -gt 0 ]; then
  log_event "run_complete status=failed failed_test_count=$failed_test_count results_csv=$quoted_csv_file"
  echo "error: $failed_test_count test(s) failed; see $csv_file and the per-test logs" >&2
  exit 1
fi
log_event "run_complete status=success failed_test_count=0 results_csv=$quoted_csv_file"
