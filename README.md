# perftest-suite

RDMA perftest matrix for two DGX Sparks joined by one or more QSFP cables.
`host` = perftest server (binds). `client` = perftest client. Run both concurrently.

## Topology

Each physical QSFP port carries 2 logical RDMA devices. Devices are discovered at
runtime: every RDMA device that is UP and has an IPv4 address is tested, so adding
a cable automatically adds its devices to the run.

The peer's addresses come from `--peer-ips` (a comma-separated list) or
`--peer-host` (a hostname, resolved through `/etc/hosts` or DNS). Each address
is matched to the local device on the same subnet, so the list order does not
matter and any IP scheme works. On the diya/hoedus cluster, where `/etc/hosts`
on both nodes maps every cable IP to both hostnames, `--peer-host Hoedus`
resolves to exactly the peer's four addresses. Without either option the peer
IP is derived from the local IP by flipping the last octet between the odd/even
pair (`1<->2`, `3<->4`, ...), which matches the cisc-diya / cisc-hoedus netplan
scheme (diya = odd, hoedus = even, same /24):

| subnet         | cisc-diya | cisc-hoedus |
|----------------|-----------|-------------|
| 10.10.10.0/24  | .1        | .2          |
| 10.10.11.0/24  | .1        | .2          |
| 10.10.12.0/24  | .1        | .2          |
| 10.10.13.0/24  | .1        | .2          |

The odd/even derivation is a fallback convention, not an invariant: on a cluster
with any other addressing scheme, pass `--peer-ips` or `--peer-host` on both
machines.

## Prereqs

```
apt install perftest iproute2 jq
```

`ethtool` and `pciutils` (provides `lspci`) are optional but strongly
recommended: the suite captures link speed, PCIe width/speed, and
driver/firmware versions into every run. Without them the environment capture
is incomplete.

Identical perftest version on both nodes. The script uses `ip -j` and `jq` to
discover devices and their addresses, and `ss` for the port preflight. `getent`
(glibc, installed with Ubuntu) resolves `--peer-host` names.

The plot script needs numpy, pandas, and matplotlib (Python 3). Create a venv
once per machine:

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

### Single paired run

Pass the same `--run-name` on both machines so the two sides land in matching
directories (host/ vs client/). Name the peer machine with `--peer-host` (its
addresses are resolved from `/etc/hosts` or DNS), or list its addresses with
`--peer-ips` (any order; they are matched by subnet):

host:
```
./perftest-suite.sh host --run-name 2026-10-06-cable-a/run-001 --peer-host Hoedus
```
client:
```
./perftest-suite.sh client --run-name 2026-10-06-cable-a/run-001 --peer-host Diya
```

Either option also accepts an environment equivalent, `PEER_HOST` / `PEER_IPS`,
and `--peer-host` and `--peer-ips` are mutually exclusive.

Without `--run-name` the output lands in a timestamped directory; that is for
ad-hoc runs only, since a host and its client started this way do not share a
directory name.

### Campaign (N repeated runs)

Use `runner.sh`. Start the host side first, then the client side, with the
same `--campaign` on both machines. Name the peer machine with `--peer-host`
(or export `PEER_HOST` once per shell) so the campaign works on any addressing
scheme:

```
./runner.sh 5 host --campaign cables-oct06 --peer-host Hoedus     # cisc-diya
./runner.sh 5 client --campaign cables-oct06 --peer-host Diya     # cisc-hoedus
```

All runs are filed under one campaign directory, and run numbers are
deterministic, so both machines' directories match exactly:

```
perftest-results/cables-oct06/run-001/{host,client}
perftest-results/cables-oct06/run-002/{host,client}
...
```

`runner.sh` stops the whole campaign if any run fails. Reusing a campaign name
re-creates the same `run-00N` names, so pick a new campaign name (or delete
the old one) rather than appending to an existing campaign.

## Tests

Per device (11 tests), run once per mode. Total runs = devices x 11 x modes.

```
ib_send_lat   ib_write_lat   ib_read_lat   ib_atomic_lat
ib_send_bw    ib_write_bw    ib_read_bw    ib_atomic_bw
ib_send_bw -b ib_write_bw -b ib_read_bw -b     (-b = bidirectional)
```

## Modes

`--modes` selects which passes run; the default is `isolation,concurrent`.

- `isolation` runs one device at a time, retrying a failed test. It measures
  each device's own ceiling with no contention.
- `concurrent` runs every discovered device at once, one test at a time. This is
  the saturation pass: the sum of the per-device rows is the aggregate the
  interconnect sustains, so a run reports the link's true speed (e.g. 200 Gbit/s
  unidirectional / 400 Gbit/s bidirectional on the two-NIC DGX Spark), and the
  gap to the isolation pass is the shared bottleneck. A failed client is retried
  as in the isolation pass; because its server is still blocked waiting, the
  retry reconnects to it. Each process is bounded by `TEST_TIMEOUT`, so a client
  that never connects cannot stall the campaign indefinitely.

`runner.sh` runs every mode in its `SUITE_MODES` (`isolation,concurrent`), so one
campaign captures the whole matrix. Add a mode there when the suite gains one.

## Env

| var             | default                   | meaning                            |
|-----------------|---------------------------|------------------------------------|
| `OUTDIR`        | `./perftest-results`      | results root; runs live under it   |
| `BASE_PORT`     | `18515`                   | first TCP port; +1 per run         |
| `CONN`          | `RC`                      | `-c` connection type               |
| `LAT_ITERS`     | `15000`                   | `-n` iterations for `*_lat`        |
| `BW_ITERS`      | `15000`                   | `-n` iterations for `*_bw` (≤20000)|
| `RETRIES`       | `3`                       | connect/bind retries (both roles)  |
| `INITIAL_DELAY` | `3`                       | client startup delay (s)           |
| `MODES`         | `isolation,concurrent`    | passes to run; see Modes           |
| `TEST_TIMEOUT`  | `120`                     | per-process timeout (s)            |
| `PEER_IPS`      | (none)                    | peer addresses, comma-separated; same as `--peer-ips` |
| `PEER_HOST`     | (none)                    | peer hostname, resolved via /etc/hosts or DNS; same as `--peer-host` |

`BW_ITERS` is validated to be ≤ 20000: perftest disables BW peak computation
above that.

## Output

Each run writes to `<OUTDIR>/<run-name>/<role>/`:

```
cables-oct06/run-001/
  host/
    environment/                    hardware + link-state capture
      system.json                   kernel, distro, perftest/rdma-core versions
      <device>/
        link.json                   parsed link speed, PCIe, MTU, driver
        ethtool.txt                 raw `ethtool <netdev>`
        ethtool-i.txt               raw `ethtool -i <netdev>` (driver/firmware)
        ibv_devinfo.txt             raw `ibv_devinfo -d <device> -v`
        lspci.txt                   raw `lspci -vv -s <addr>` (PCIe link)
    results.csv                     parsed perftest tables, all message sizes
    commands.txt                    every test's host and client command
    <test>[_bidi]_<device>_<mode>.log   raw perftest output, never discarded
    notes.<name>                    the --notes file, if one was given
  client/
    ... same layout ...
```

`link.json` fields: `device`, `netdev`, `local_ip`, `peer_ip`, `mtu`,
`link_speed_mbps`, `autoneg`, `link_detected`, `pcie_speed`, `pcie_width`,
`active_width`, `active_speed_gbps`, `driver`, `driver_version`,
`firmware_version`. Empty string means the tool or field was unavailable on that
machine. `active_width` and `active_speed_gbps` come from `ibv_devinfo`; their
product is the device's own link rate, which the figures use as the reference.
`pcie_speed` and `pcie_width` fall back to sysfs (`current_link_speed`,
`current_link_width`) when `lspci` cannot read the link capabilities without
root.

The suite also prints a one-line link-state table per device at startup:

```
link state (device | local -> peer | link speed | pcie | mtu):
  rocep1s0f0     | 10.10.10.2 -> 10.10.10.1 | 200000Mb/s | 16GT/s x8 | mtu 4096
```

## results.csv

Tidy long format: one row per (test, message size). Units: bandwidth in
**GB/s** (decimal gigabytes per second), latency in microseconds.

```
test,device,role,mode,bidi,size_bytes,bw_peak_GBps,bw_avg_GBps,msg_rate_Mpps,lat_min_usec,lat_max_usec,lat_typical_usec,lat_avg_usec,lat_stdev_usec,lat_p99_usec,lat_p999_usec,error
```

- Bandwidth rows (`ib_*_bw`) fill the `bw_*` and `msg_rate_*` columns; latency
  rows fill the `lat_*` columns. The other columns are empty.
- `mode` is `isolation` or `concurrent`; the same (test, device) appears once per
  mode. Result trees written before the column existed are read as `isolation`.
- `bidi=true` rows are the `-b` tests; perftest reports the aggregate of both
  directions in those rows.
- Values are converted from perftest's `--report_gbits` output:
  `GB/s = Gbit/s / 8`.
- The `error` column is empty on success. On failure it records what happened
  (`perftest rc=N`, `no parseable rows in perftest output`), so a failed test
  is never silently dropped.
- perftest prints its results table on the **client**. Some host-side cells
  are zero by perftest design (host `ib_send_bw` peak, host `ib_read_lat` /
  `ib_atomic_lat`). Read BW/latency from the client's `results.csv`; the
  host's file confirms the server bound and ran.

## Notes

- `-c UD` has no read verbs; `ib_read_*` skipped when `CONN` is not `RC`/`UC`.
- Both roles must use identical `CONN` and discover the same devices in the
  same order; ports are allocated by position, so any divergence misaligns
  them. Check the "discovered devices" tables on both sides match before
  trusting a run. `--peer-ips` / `--peer-host` addresses are matched by subnet,
  so their order does not need to match the device order.
- Ports are checked free before the run; stale `ib_*` servers are killed
  between tests.
- `-F` suppresses the perftest cpufreq warning.
- `--notes` is still free-form prose; replacing it with structured, validated
  metadata (operator, cable part numbers, purpose, ...) is planned.

## Figures

`plot_perftest.py` in this directory reads every `results.csv` under a
campaign tree and writes the paper figures to `figs/`:

```
python plot_perftest.py     # from this repository
```

`--results` and `--out` default to `perftest-results/` and `figs/` next to the
script. The script imports numpy, pandas, and matplotlib only, so it and a
campaign tree can be copied elsewhere and plotted from any working directory.
It reads both roles and prefers client rows, because perftest prints the
measurement tables on the client and leaves the read and atomic latency tables
and the send_bw peak empty on the host side; a host row is used only for a key
with no client row. The figures cover the bandwidth verbs at the tested
message size, the send/write/read/atomic latency percentile tiers, per-run
deviation from each repeat group's mean, and the aggregate bandwidth. The
bandwidth figure draws one rate rule from the captured `ibv_devinfo` link rate
(`active_speed x active_width`), so a bar at the rule is a saturated device
link; its bars are per-direction rates, and the bidirectional bars are the
perftest -b aggregate halved, so no bar compares a two-direction sum against a
one-direction rate. The aggregate figure sums the per-device rates of the
isolation and concurrent passes, unidirectional and bidirectional, and draws
the fabric ceiling from `FABRIC_UNI_GBPS` / `FABRIC_BIDI_GBPS` (in Gbit/s) when
set, falling back to the sum of the device link rates (doubled for the
bidirectional panel). Set them to the interconnect's known caps so a bar at the
rule is a saturated fabric; on the two-NIC DGX Spark that is
`FABRIC_UNI_GBPS=200 FABRIC_BIDI_GBPS=400`. The unidirectional bars are the
per-device sums of each run; the bidirectional bars are the perftest -b
aggregates, which already count both directions. `ib_atomic_bw` runs at the
default 8 B; the script prints its message rate with the role counts before
drawing.
