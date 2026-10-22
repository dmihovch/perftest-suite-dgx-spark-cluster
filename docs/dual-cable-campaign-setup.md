# Dual-cable RDMA campaign setup

This document is a topology-specific reference for the original Diya/Hoedus
two-Spark cluster and its sibling LLM-serving benchmark. The example addresses,
hostnames, and interface names do not configure arbitrary Spark clusters. Use
the repository root `README.md` for generic RDMA perftest runs.

This repository holds the RDMA perftest suite. The Netplan, /etc/hosts, and
verification sections 1-4 configure the cluster network and run the suite. The
sections after that (NCCL, Ray, model canaries, full campaign) run from the
sibling `dgx-spark-llm-serving-benchmark` repository; `scripts/` paths in those
sections resolve there.

The benchmark scripts in `scripts/` accept a comma-separated `CX_IFACE` for
multi-cable setups. `NCCL_SOCKET_IFNAME` and `GLOO_SOCKET_IFNAME` inherit the
full list. NCCL stripes across all listed interfaces. `DIYA_IP`, `HOEDUS_IP`,
and `HEAD_IP` stay on one subnet for the Ray rendezvous.

Reference for the original single-cable TCP setup: `scripts/connectx_env.sh`
header comment and `docs/rdma-testing.md`.

## Netplan

Each QSFP cable carries two ports. Two cables expose four interfaces per node.

Diya (`/etc/netplan/40-cx7.yaml`):
```yaml
network:
  version: 2
  ethernets:
    enp1s0f0np0:
      addresses: [10.10.10.1/24]
      dhcp4: no
      mtu: 4096
    enP2p1s0f0np0:
      addresses: [10.10.11.1/24]
      dhcp4: no
      mtu: 4096
    enp1s0f1np1:
      addresses: [10.10.12.1/24]
      dhcp4: no
      mtu: 4096
    enP2p1s0f1np1:
      addresses: [10.10.13.1/24]
      dhcp4: no
      mtu: 4096
```

Hoedus (`/etc/netplan/40-cx7.yaml`):
```yaml
network:
  version: 2
  ethernets:
    enp1s0f0np0:
      addresses: [10.10.10.2/24]
      dhcp4: no
      mtu: 4096
    enP2p1s0f0np0:
      addresses: [10.10.11.2/24]
      dhcp4: no
      mtu: 4096
    enp1s0f1np1:
      addresses: [10.10.12.2/24]
      dhcp4: no
      mtu: 4096
    enP2p1s0f1np1:
      addresses: [10.10.13.2/24]
      dhcp4: no
      mtu: 4096
```

Apply on both nodes:
```bash
sudo chmod 600 /etc/netplan/40-cx7.yaml
sudo netplan apply
```

## `/etc/hosts`

Both nodes:
```
10.10.10.1  Diya
10.10.10.2  Hoedus
10.10.11.1  Diya
10.10.11.2  Hoedus
10.10.12.1  Diya
10.10.12.2  Hoedus
10.10.13.1  Diya
10.10.13.2  Hoedus
```

## Environment

Export before running any script, or add to `.env`:
```bash
export CX_IFACE="enp1s0f0np0,enP2p1s0f0np0,enp1s0f1np1,enP2p1s0f1np1"
export NCCL_IB_DISABLE=0
```

`DIYA_IP`, `HOEDUS_IP`, and `HEAD_IP` keep their defaults (`10.10.10.1`,
`10.10.10.2`). Ray uses one subnet for the head rendezvous. The other three
subnets carry NCCL traffic.

If NCCL does not discover all four RDMA devices, set `NCCL_IB_HCA`:
```bash
export NCCL_IB_HCA="rocep1s0f0,roceP2p1s0f0,rocep1s0f1,roceP2p1s0f1"
```
Find device names with `ls /sys/class/infiniband/`.

## Verification sequence

Run in order. Each step must pass before proceeding.

### 1. Link state

On both nodes:
```bash
ip -br link show enp1s0f0np0 enP2p1s0f0np0 enp1s0f1np1 enP2p1s0f1np1
```
All four must show `UP`.

### 2. Ping all subnets

On Diya:
```bash
ping -c 3 10.10.10.2
ping -c 3 10.10.11.2
ping -c 3 10.10.12.2
ping -c 3 10.10.13.2
```
All four must show latency under 2 ms.

### 3. RDMA device discovery

On both nodes:
```bash
ibdev2netdev
```
Four RDMA devices must appear. Example:
```
rocep1s0f0      enp1s0f0np0
roceP2p1s0f0    enP2p1s0f0np0
rocep1s0f1      enp1s0f1np1
roceP2p1s0f1    enP2p1s0f1np1
```

### 4. RDMA perftest on all devices

On Hoedus (host):
```bash
cd ~/fsaii/perftest-suite-dgx-spark-cluster
./perftest-suite.sh host --run-name dual-200Gb-Oct-8/run-001 \
    --modes isolation,concurrent \
    --peer-host Diya
```

On Diya (client):
```bash
cd ~/fsaii/perftest-suite-dgx-spark-cluster
./perftest-suite.sh client --run-name dual-200Gb-Oct-8/run-001 \
    --modes isolation,concurrent \
    --peer-host Hoedus
```

`--peer-host` resolves the peer's addresses from `/etc/hosts` (both nodes map
every cable IP to both hostnames, see above); `--peer-ips` with the four
addresses works identically.

Check `perftest-results/dual-200Gb-Oct-8/host/run-001/results.csv` and
`perftest-results/dual-200Gb-Oct-8/client/run-001/results.csv`. Each device's
isolation bandwidth should be a stable fraction of its `ibv_devinfo` link rate
(`active_speed x active_width`), and the
concurrent aggregate (the sum of the `mode=concurrent` rows) is the saturation
reading: it should sit at the fabric caps (200 Gbit/s unidirectional, 400
Gbit/s bidirectional on this two-NIC system) rather than at the sum of the
isolation rates, and a gap between the two sums means the devices share a
bottleneck. `ib_write_lat` must be under 2 µs.

### 5. NCCL all-reduce

On Diya:
```bash
export CX_IFACE="enp1s0f0np0,enP2p1s0f0np0,enp1s0f1np1,enP2p1s0f1np1"
export NCCL_IB_DISABLE=0
export NCCL_DEBUG=INFO
bash scripts/interconnect_microbench.sh
```

Check output for `NCCL INFO NET/IB : Using` lines listing all four RDMA
devices. Bus bandwidth at 1 GiB must approach aggregate throughput
(4 × 25 GB/s = 100 GB/s one-way; all-reduce busbw equals algbw at world
size 2).

If NCCL uses fewer than four devices, set `NCCL_IB_HCA` explicitly (see
Environment section above) and rerun.

### 6. Ray cluster

On Diya:
```bash
export CX_IFACE="enp1s0f0np0,enP2p1s0f0np0,enp1s0f1np1,enP2p1s0f1np1"
export NCCL_IB_DISABLE=0
scripts/start_ray.sh head
```

On Hoedus:
```bash
export CX_IFACE="enp1s0f0np0,enP2p1s0f0np0,enp1s0f1np1,enP2p1s0f1np1"
export NCCL_IB_DISABLE=0
scripts/start_ray.sh worker
```

On Diya, verify:
```bash
docker exec vllm-spark ray status
```
Two nodes must show `Active`.

### 7. Ray all-reduce microbench

On Diya:
```bash
export CX_IFACE="enp1s0f0np0,enP2p1s0f0np0,enp1s0f1np1,enP2p1s0f1np1"
export NCCL_IB_DISABLE=0
bash scripts/ray_allreduce_bench.sh
```

Check `results/allreduce_ray_*.csv`. Bus bandwidth at 1 GiB must reflect
aggregate throughput across all four ports.

### 8. Model canary

On Diya, with Ray running from step 6:
```bash
export CX_IFACE="enp1s0f0np0,enP2p1s0f0np0,enp1s0f1np1,enP2p1s0f1np1"
export NCCL_IB_DISABLE=0
CANARY_ONLY=1 MODEL_LABELS="nemotron-super-49b-nvfp4" bash scripts/run_models.sh single
```

Loads the smallest new NVFP4 model (31.1 GB), validates the streaming
contract, tears down. Repeat for a dual-node BF16 model:
```bash
CANARY_ONLY=1 MODEL_LABELS="nemotron-puzzle-75b-bf16" bash scripts/run_models.sh tp2
```

## Full campaign

After all verification steps pass:
```bash
export CX_IFACE="enp1s0f0np0,enP2p1s0f0np0,enp1s0f1np1,enP2p1s0f1np1"
export NCCL_IB_DISABLE=0
export OUT_DIR="results"
bash scripts/run_job.sh
```

`run_job.sh` runs tp2, pp2, then single. `--skip-done` is active. A crash
resumes at the last incomplete point.

## Fingerprint

`run_models.sh` records `connectx_iface=$CX_IFACE` in the serving-policy
fingerprint. A campaign must use one `CX_IFACE` value for all points. Mixing
values in the same `OUT_DIR` causes the benchmark to reject mismatched rows.

## Disk space and HF_CACHE location

Default `HF_CACHE` is `$HOME/.cache/huggingface`. Model weights for the full
matrix total ~1.05 TB (existing models ~500 GB, new models ~547 GB). The DGX
Spark internal NVMe is 1–2 TB. The root filesystem shares this drive with the
OS. A full disk on the root filesystem blocks `sudo` and file deletion.

Set `HF_CACHE` to a path with enough space before downloading. Options:

- External USB-C NVMe enclosure with a 2 TB or larger drive, mounted at a
  dedicated path.
- A separate partition on the internal NVMe if one was provisioned.

Example:
```bash
sudo mkdir -p /mnt/model-cache
sudo mount /dev/nvme1n1p1 /mnt/model-cache   # adjust device
sudo chown $USER:$USER /mnt/model-cache
export HF_CACHE=/mnt/model-cache
```

Add `HF_CACHE` to `.env` so all scripts use it:
```
HF_CACHE=/mnt/model-cache
```

`predownload.sh` queries `https://huggingface.co/api/models/{repo}` for every
unique repo's `safetensors.total` size in a prefetch phase that runs before any
download starts. An API failure in the prefetch phase exits the script before
the first container runs. The script refuses to download if available disk
space is less than the total download size plus an OS safety buffer (20 GiB or
5% of total disk, whichever is larger). Override the buffer with
`PREDOWNLOAD_MIN_FREE_GB`. Requires network access. Skipped in `CHECK_ONLY`
mode.

## Model pre-download

Revisions are pinned in `configs/models.yaml`. Pre-download before running:
```bash
scripts/predownload.sh all
scripts/mirror_worker_cache.sh all
```
