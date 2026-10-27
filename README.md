# perftest-suite

Pairwise RDMA perftest suite for DGX Spark systems. `host` runs the perftest
server. `client` runs the perftest client. Start both commands concurrently
with matching pair and run identifiers.

## Topology

Devices are discovered at runtime. Every RDMA device that is UP and has an
IPv4 address is tested. Pass a JSON topology file for the current node with
`--config`; use `--peer` to select a peer when the file lists more than one.
Peer addresses come from the JSON file or explicit peer options.

For direct links or switched fabrics that put each RDMA link on a distinct
subnet, use `--peer-host` or `--peer-ips`. These options match peer addresses
to RDMA devices by subnet. Use `--peer-map` when multiple links share one subnet
or the peer is reached through a routed path. The suite maps each current-node
RDMA device to a peer IP and binds perftest to the device's IP address.

Each run tests one pair of systems. A full pairwise matrix for N systems has
N(N-1)/2 pairs. Each system needs one JSON peer profile for every other system.
Start the host and client commands for each pair.

## Prereqs

```
apt install perftest iproute2 jq
```

`ethtool` and `pciutils` (which provides `lspci`) populate captured link speed,
PCIe, driver, and firmware fields. The suite records empty values when these
tools are unavailable.

Use the same perftest version on both systems, and ensure both builds support
`--bind_source_ip`. The suite requires `ip`, `jq`, `ss`, `timeout`, and
`column`. `getent` resolves `--peer-host` names.

## Synchronizing the repo and results

Each script runs one rsync transfer between the current machine and one target.
`deploy-repo.sh` copies a source directory to a remote destination.
`collect-results.sh` copies a remote source directory to a local destination.
Run each script once per hop when a target is reachable only through another
machine. The scripts exclude repository files that are not needed to run the
tests during deployment. The target machines do not need internet access.

Use rsync's `host:path` syntax for a remote directory. SSH uses the configured
hostname alias and username. The machine running each command needs an SSH
entry for its target. Install `rsync` on both ends of each transfer.

```sshconfig
Host gx10-00
  HostName <first-target-address>
  User <login>

Host gx10-01
  HostName <second-target-address>
  User <login>
```

Replace the address and login placeholders with the target values.

```bash
# On the current machine: copy the repo to the first target.
./deploy-repo.sh /path/to/perftest-suite 'gx10-00:~/perftest-suite-cor/'

# On the first target: copy the repo to the second target.
./deploy-repo.sh ~/perftest-suite-cor 'gx10-01:~/perftest-suite-cor/'

# On the first target: pull the campaign from the second target.
./collect-results.sh 'gx10-01:~/perftest-suite-cor/perftest-results/test-test-test/' \
  ~/perftest-suite-cor/perftest-results/test-test-test

# On the current machine: pull the staged campaign from the first target.
./collect-results.sh 'gx10-00:~/perftest-suite-cor/perftest-results/test-test-test/' \
  /path/to/perftest-results/test-test-test
```

`deploy-repo.sh` takes a local source directory and an rsync remote destination.
`collect-results.sh` takes an rsync remote source and a local destination
directory.

For fixed-volume transfer-time estimates using installed perftest executables
and the existing result-collection workflow, see
[`rdma-transfer-suite/README.md`](rdma-transfer-suite/README.md).

The plot script requires Python 3, numpy, pandas, and matplotlib. Install its
dependencies in a virtual environment:

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

### Machine configuration

Each file maps current-node RDMA device names to the IPv4 addresses of the
selected peer's interfaces. Device names must match names discovered on the
current node. The peer file must map each peer device back to the current
interface. Explicit mappings work with direct cables, switched fabrics, and
routed networks.

For example, save this as `spark-a.json` on Spark A:

```json
{
  "schema_version": 1,
  "node": "spark-a",
  "peers": {
    "spark-b": {
      "spark_a_device_1": "10.20.0.2",
      "spark_a_device_2": "10.21.0.2"
    }
  }
}
```

Save the reciprocal mapping as `spark-b.json` on Spark B:

```json
{
  "schema_version": 1,
  "node": "spark-b",
  "peers": {
    "spark-a": {
      "spark_b_device_1": "10.20.0.1",
      "spark_b_device_2": "10.21.0.1"
    }
  }
}
```

For N Sparks, add one peer entry to each system's file for every other system.
Each peer entry contains the peer's IPv4 address for every current-node RDMA
device. A file with multiple peer entries requires `--peer` to select one.

### Single paired run

Pass the same `--run-name` to both commands. Host and client results use
matching directories. Example:

host:
```
./perftest-suite.sh host --config spark-a.json --run-name spark-a-to-spark-b/run-001
```
client:
```
./perftest-suite.sh client --config spark-b.json --run-name spark-a-to-spark-b/run-001
```

For a config with multiple peer entries, select the target system with `--peer`.
Spark A uses `--config spark-a.json --peer spark-b`. Spark B uses
`--config spark-b.json --peer spark-a`.

JSON files store peer mappings for repeatable runs. Peer options support
one-off runs. `--peer-map` maps each current-node device to a peer address.
`--peer-ips` supplies one address per device on a distinct subnet. The order
does not matter. `--peer-host` resolves a hostname and matches addresses by
subnet. Device names and discovery order can differ between systems. With
explicit maps, reverse the current/peer IP pairs on the other
system:

```
./perftest-suite.sh host --run-name spark-a-to-spark-b/run-001 \
    --peer-map 'spark_a_device_1=10.20.0.2,spark_a_device_2=10.20.0.3'
./perftest-suite.sh client --run-name spark-a-to-spark-b/run-001 \
    --peer-map 'spark_b_device_1=10.20.0.1,spark_b_device_2=10.20.0.4'
```

Replace the example device names and addresses with the values from each
machine. Each mapped address must be the unique IP of the corresponding peer
interface. The reversed maps on both systems must describe the same interface
pairs. Direct peer options are mutually exclusive with each other and with
`--config`. They also accept `PEER_MAP`, `PEER_IPS`, and `PEER_HOST`
environment variables.

Without `--run-name`, each command uses a timestamped directory. Host and
client commands need the same explicit run name to write to matching paths.

### Campaign (N repeated runs)

Use `runner.sh` for repeated tests between one pair. Start the host side first,
then the client side, with the same `--campaign` on both machines:

```
./runner.sh 5 host --campaign spark-a-to-spark-b --config spark-a.json
./runner.sh 5 client --campaign spark-a-to-spark-b --config spark-b.json
```

Run these commands on gx10-00 and gx10-01 with the matching files in `configs/`:

```
./runner.sh 5 host --campaign test-new --config configs/gx10-00.json
./runner.sh 5 client --campaign test-new --config configs/gx10-01.json
```

For a config with multiple peer entries, pass `--peer <name>` on both systems.
Run the campaign for each unique system pair.

Each campaign uses matching run numbers on the current and target systems:

```
perftest-results/spark-a-to-spark-b/host/run-001
perftest-results/spark-a-to-spark-b/host/run-002
perftest-results/spark-a-to-spark-b/client/run-001
perftest-results/spark-a-to-spark-b/client/run-002
...
```

`runner.sh` stops the campaign when a run fails. A campaign name reuses the
same `run-00N` directory names. Use a new campaign name for a separate run set.

## Tests

Each mode runs up to 9 perftest commands per device. Send, write, and read run
as separate latency, unidirectional bandwidth, and bidirectional bandwidth
commands. Every command uses perftest's `-a` sweep, which measures payload
sizes by powers of two from 2 bytes through 8 MiB. Read commands are skipped
for `CONN=UD`; each size produces a separate result row.

```
ib_send_lat   ib_write_lat   ib_read_lat
ib_send_bw    ib_write_bw    ib_read_bw
ib_send_bw -b ib_write_bw -b ib_read_bw -b     (-b = bidirectional)
```

## Modes

`--modes` selects the test passes. The default is `isolation,concurrent`.

- `isolation` runs one device at a time and retries failed tests. Other suite
  devices do not run concurrently during this pass.
- `concurrent` runs every discovered device at once, one test at a time. The sum
  of per-device rows is the measured aggregate rate. Compare it with isolation
  results to measure the effect of concurrent device traffic.
- A failed client is retried while its server waits for a connection. Each
  process stops after `TEST_TIMEOUT` seconds.

`runner.sh` uses the modes listed in `SUITE_MODES`. The default list is
`isolation,concurrent`.

## Env

| var             | default                   | meaning                            |
|-----------------|---------------------------|------------------------------------|
| `OUTDIR`        | `./perftest-results`      | results root; runs live under it   |
| `BASE_PORT`     | `18515`                   | first TCP port; one port per device/test |
| `CONN`          | `RC`                      | `-c` connection type               |
| `LAT_ITERS`     | `15000`                   | `-n` iterations for `*_lat`        |
| `BW_ITERS`      | `15000`                   | `-n` iterations for `*_bw` (≤20000)|
| `RETRIES`       | `3`                       | connect/bind retries (both roles)  |
| `INITIAL_DELAY` | `3`                       | client startup delay (s)           |
| `MODES`         | `isolation,concurrent`    | passes to run; see Modes           |
| `TEST_TIMEOUT`  | `120`                     | per-process timeout (s)            |
| `PEER_MAP`      | (none)                    | current-node RDMA device to peer IPv4 map; same as `--peer-map` |
| `PEER_IPS`      | (none)                    | peer addresses, comma-separated; same as `--peer-ips` |
| `PEER_HOST`     | (none)                    | peer hostname, resolved via /etc/hosts or DNS; same as `--peer-host` |

`BW_ITERS` is validated to be ≤ 20000: perftest disables BW peak computation
above that.

## Output

Each run writes to `<OUTDIR>/<campaign>/<role>/<run-id>/`:

```
cables-oct06/
  host/
    run-001/
      environment/                  hardware + link-state capture
        system.json                 kernel, distro, perftest/rdma-core versions
        <device>/
          link.json                 parsed link speed, PCIe, MTU, driver
          ethtool.txt               raw `ethtool <netdev>`
          ethtool-i.txt             raw `ethtool -i <netdev>` (driver/firmware)
          ibv_devinfo.txt           raw `ibv_devinfo -d <device> -v`
          lspci.txt                 raw `lspci -vv -s <addr>` (PCIe link)
      results.csv                   parsed perftest tables, all message sizes
      commands.txt                  exact command and peer endpoint for this role
      <test>[_bidi]_<device>_<mode>.log   raw perftest output
      notes.<name>                  the --notes file, if one was given
    run-002/
      ... same layout ...
  client/
    run-001/
      ... same layout ...
    run-002/
      ... same layout ...
```

`link.json` fields: `device`, `netdev`, `local_ip`, `peer_ip`, `mtu`,
`link_speed_mbps`, `autoneg`, `link_detected`, `pcie_speed`, `pcie_width`,
`active_width`, `active_speed_gbps`, `driver`, `driver_version`,
`firmware_version`. A field is empty when the capture tool or device does not
provide its value. `active_width` and `active_speed_gbps` come from
`ibv_devinfo`. Their product is the device port rate used in the bandwidth
figures. `pcie_speed` and `pcie_width` use sysfs (`current_link_speed`,
`current_link_width`) when `lspci` cannot read link capabilities as the current
user.

The suite prints one link-state row per device at startup:

```
link state (device | local -> peer | link speed | pcie | mtu):
  rdma_device_0  | 192.0.2.2 -> 192.0.2.1 | 200000Mb/s | 16GT/s x8 | mtu 4096
```

## results.csv

Each row records one test and message size. Bandwidth uses **GB/s** (decimal
gigabytes per second). Latency uses microseconds.

```
test,device,role,mode,bidi,size_bytes,bw_peak_GBps,bw_avg_GBps,msg_rate_Mpps,lat_min_usec,lat_max_usec,lat_typical_usec,lat_avg_usec,lat_stdev_usec,lat_p99_usec,lat_p999_usec,error
```

- Bandwidth rows (`ib_*_bw`) fill the `bw_*` and `msg_rate_*` columns; latency
  rows fill the `lat_*` columns. The other columns are empty.
- `size_bytes` identifies each point in the perftest `-a` sweep, which covers
  powers of two from 2 bytes through 8 MiB for these verbs.
- `lat_avg_usec` records average measured latency for one message at each size.
  `LAT_ITERS` applies to each payload size in the latency sweep.
- `BW_ITERS` applies to each payload size in the bandwidth sweep.
- `mode` is `isolation` or `concurrent`; each (test, device, mode, direction,
  size) has one result row. Result trees written before the column existed are
  read as `isolation`.
- `bidi=true` rows are the `-b` tests; perftest reports the aggregate of both
  directions in those rows.
- Values are converted from perftest's `--report_gbits` output:
  `GB/s = Gbit/s / 8`.
- The `error` column is empty on success. It records failures such as
  `perftest rc=N` and `no parseable rows in perftest output`.
- perftest prints its measurement table on the **client**. Host-side
  `ib_send_bw` peak is zero, and host CSV rows are omitted for `ib_read_lat`.
  Use the client's CSV for these measurements. Host logs record server output.

## Notes

- `-c UD` has no read verbs. `ib_read_*` tests require `CONN=RC` or `CONN=UC`.
- Both roles must use identical `CONN` and discover the same number of
  corresponding devices. Port slots use current/peer IP pairs. Device names and
  discovery order can differ. Check the discovered-device tables and peer maps
  before starting a run.
- The suite checks ports before each run and stops stale `ib_*` server
  processes between tests.
- `-F` suppresses the perftest cpufreq warning.
- `--notes` writes free-form text to `notes.<name>` in the run directory.

## Figures

`plot_perftest.py` reads `results.csv` files under the results tree and writes
figures to `figs/`:

```
python plot_perftest.py
python plot_perftest.py --results /path/to/perftest-results \
    --out /path/to/figures --format pdf
```

`--results` and `--out` default to `perftest-results/` and `figs/` beside the
script. `--results` can point to the whole tree or one campaign directory.
`--format` accepts `pdf`, `png`, or `both`; `--dpi` sets PNG resolution.
The script requires numpy, pandas, and matplotlib. It searches nested campaign
directories. Output subdirectories use URL-encoded campaign paths.

The script reads both client and host CSV files. A client row is preferred
because perftest prints its measurement table on the client. A measured host
row supplies a value when its matching client row has no measurement. Rows
match by campaign, run, endpoint pair, test, direction, mode, and payload size.
The coverage report prints CSV row counts and error rows. Result trees without
a `mode` column are read as isolation runs.

The script writes up to nine figures per campaign. Each verb (`send`, `write`,
and `read`) has latency, throughput, and estimated transmission-time figures.
Link panels use a two-column layout and logarithmic axes with numeric tick
labels.

- `latency_<verb>` plots typical latency and p99 latency in microseconds.
- `bandwidth_<verb>` plots unidirectional and bidirectional throughput in
  decimal GB/s. The log y axis uses numeric GB/s tick labels, and curve labels
  show the largest-payload throughput. Horizontal references show the nominal
  one-direction and full-duplex link rates when link metadata is available.
- `transmission_time_<verb>` estimates transfer time for 0.1, 1, 10, and 100
  decimal GB volumes. Each estimate adds the smallest-payload typical latency
  as startup time to the transfer volume divided by the largest-payload
  average throughput. The graph states this estimate model and uses log axes
  for volume and time.
- Link panels use a canonical local/peer IP pair from `link.json`. Colors
  distinguish isolation and concurrent measurements. A shaded band
  shows the 10th–90th percentile across runs. Failure annotations identify
  links without successful concurrent measurements.

Figure names are `latency_send`, `latency_write`, `latency_read`,
`bandwidth_send`, `bandwidth_write`, `bandwidth_read`,
`transmission_time_send`, `transmission_time_write`, and
`transmission_time_read`, with the selected output format as the extension.
The results CSV retains individual bandwidth and latency measurements,
including latency percentiles.

## Transfer-time estimates

`rdma-transfer-suite/` uses the suite's host/client configs, campaign runs,
RDMA links, and isolation/concurrent modes for fixed-volume `ib_*_bw` tests.
It estimates client data-phase time from perftest's reported average
bandwidth, records the report-rounding bound, and retains process wall time
and the exact requested payload volume. See
[rdma-transfer-suite/README.md](rdma-transfer-suite/README.md) for commands
and result fields.

The suite writes one run-level event log per role to
`perftest-results/<campaign>/{host,client}-logs/run-NNN.log`. The log uses UTC
timestamps and records device mappings, preflight and environment capture,
test commands, retry attempts, process exit codes, elapsed seconds, and result
row counts. Failed tests include a short excerpt of perftest output. Raw
perftest output remains in the role's `run-NNN/` directory.
