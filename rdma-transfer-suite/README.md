# RDMA transfer-time suite

This suite estimates fixed-volume RDMA data-phase time from average bandwidth
reported by the installed perftest executables. It uses the host/client
pattern, JSON peer configs, campaign runs, verbs, and isolation/concurrent
modes from `perftest-suite.sh`.

## Deploy and prerequisites

Deploy the repository with its existing `deploy-repo.sh`; the target nodes do
not need internet access. The suite finds `ib_send_bw`, `ib_write_bw`, or
`ib_read_bw` through `PATH` on each node. Use the same perftest version on both
ends, with support for `--bind_source_ip`, `-N`, and `--report_gbits`.

```bash
./deploy-repo.sh /path/to/perftest-suite 'gx10-00:~/perftest-suite-cor/'
./deploy-repo.sh /path/to/perftest-suite 'gx10-01:~/perftest-suite-cor/'
```

When the development machine can reach only one node, deploy there, then run
that node's `deploy-repo.sh` to copy the repository to the other node. The
suite scripts and configurations are architecture-independent; perftest runs
on each node where its installed executable is present in `PATH`.

## Measure a campaign

Start the host process first:

```bash
./rdma-transfer-suite/runner.sh 5 host \
  --campaign spark-a-to-spark-b --config configs/gx10-00.json \
  --verb write --transfer-bytes 1000000000 --message-bytes 1000000
```

Run the client on the other system with its own config:

```bash
./rdma-transfer-suite/runner.sh 5 client \
  --campaign spark-a-to-spark-b --config configs/gx10-01.json \
  --verb write --transfer-bytes 1000000000 --message-bytes 1000000
```

Pass `--peer NAME` when the config contains multiple peer profiles. The
default modes are `isolation,concurrent`. Isolation runs links sequentially.
The host starts and waits for one server per link in that order. Concurrent
starts all configured links together. The host starts one server per link,
then waits for the concurrent clients. The client waits three seconds before
each isolation link and each concurrent group by default.

The default transfer contains 1,000 operations of 1,000,000 bytes per link
and mode. The payload volume must be divisible by the operation size. The
suite requires at least 100 operations per run. It uses `-N` to disable
perftest's separate peak-bandwidth calculation.

Use a different port base if the default range is in use:

```bash
--base-port 18600
```

Both systems must use the same base port and mode ordering. The suite assigns
ports deterministically by the sorted local/peer IPv4 endpoint pair and mode.

## Time estimate

The suite calculates `estimated_data_phase_elapsed_s` from perftest's reported
average bandwidth and the configured fixed payload:

```text
time = transfer_bytes * 8 / (reported_average_Gbit_s * 1,000,000,000)
```

With standard perftest output, average bandwidth is printed to two decimal
places in Gbit/s. The suite derives a rounding bound from the precision shown
in the parsed result. For a displayed bandwidth `B` and display step `q`, it
uses the interval from `8 * transfer_bytes / ((B + q/2) * 1e9)` to
`8 * transfer_bytes / ((B - q/2) * 1e9)`. At 200.00 Gbit/s, a 1 GB transfer
is estimated at 40 ms with about ±1 microsecond from display rounding alone.
The bound does not include run-to-run variation or other measurement effects.

`client_process_wall_elapsed_s` remains a diagnostic. It spans the client
process from just before launch until exit, including process startup, RDMA
setup, transfer, and teardown. `data_phase_GBps` is the reported average
bandwidth converted from Gbit/s to decimal GB/s.

## Results

Each run writes to
`transmission-results/<campaign>/{host,client}/run-NNN/`. A run directory
contains `run.json`, `commands.txt`, and one raw perftest log per link and
mode. The client run also contains `measurements.csv`.

## Collect results

Use the repository's `collect-results.sh` from the development machine to
copy each endpoint's role directory. The source path ends in `/` so rsync
copies its run directories into the matching local destination:

```bash
./collect-results.sh \
  'gx10-00:~/perftest-suite-cor/transmission-results/spark-a-to-spark-b/host/' \
  ./transmission-results/spark-a-to-spark-b/host
./collect-results.sh \
  'gx10-01:~/perftest-suite-cor/transmission-results/spark-a-to-spark-b/client/' \
  ./transmission-results/spark-a-to-spark-b/client
```

Run one collection per reachable hop when the development machine cannot SSH
directly to a node. The suite stores `measurements.csv` and raw client logs on
the client, with host logs and metadata in the host directory.
