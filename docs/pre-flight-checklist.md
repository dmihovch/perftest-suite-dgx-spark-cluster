# Pre-flight checklist

This checklist records the original Diya/Hoedus two-Spark setup and also
contains steps for the sibling LLM-serving benchmark repository. The netplan,
hostnames, and interface examples below are topology-specific. For generic
RDMA perftest usage, see the repository root `README.md`.

This repository holds the RDMA perftest suite. Steps 1-8 configure the cluster
network, install this repository's requirements.txt (plot dependencies), and run
the suite. Steps 9-14 (NCCL, Ray, models, campaign) run from the sibling
`dgx-spark-llm-serving-benchmark` repository; `scripts/` paths in those steps
resolve there.

Run in order. Each step must pass before the next.

## 1. Netplan

Both nodes, `/etc/netplan/40-cx7.yaml`:

Diya:
```yaml
network:
  version: 2
  ethernets:
    enp1s0f0np0:    {addresses: [10.10.10.1/24], dhcp4: no, mtu: 4096}
    enP2p1s0f0np0:  {addresses: [10.10.11.1/24], dhcp4: no, mtu: 4096}
    enp1s0f1np1:    {addresses: [10.10.12.1/24], dhcp4: no, mtu: 4096}
    enP2p1s0f1np1:  {addresses: [10.10.13.1/24], dhcp4: no, mtu: 4096}
```

Hoedus:
```yaml
network:
  version: 2
  ethernets:
    enp1s0f0np0:    {addresses: [10.10.10.2/24], dhcp4: no, mtu: 4096}
    enP2p1s0f0np0:  {addresses: [10.10.11.2/24], dhcp4: no, mtu: 4096}
    enp1s0f1np1:    {addresses: [10.10.12.2/24], dhcp4: no, mtu: 4096}
    enP2p1s0f1np1:  {addresses: [10.10.13.2/24], dhcp4: no, mtu: 4096}
```

```bash
sudo chmod 600 /etc/netplan/40-cx7.yaml
sudo netplan apply
```

## 2. /etc/hosts

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

## 3. Python venv

Diya only (needed by plot_perftest.py):
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 4. Environment

Diya only. Source once per shell session:
```bash
source rdma-dual-cable.env
```

If the internal NVMe has less than ~600 GB free, uncomment `HF_CACHE` in
`rdma-dual-cable.env` and set it to an external drive path.

## 5. Link state

Both nodes:
```bash
ip -br link show enp1s0f0np0 enP2p1s0f0np0 enp1s0f1np1 enP2p1s0f1np1
```
All four `UP`.

## 6. Ping

Diya:
```bash
ping -c 3 10.10.10.2
ping -c 3 10.10.11.2
ping -c 3 10.10.12.2
ping -c 3 10.10.13.2
```
All four under 2 ms.

## 7. RDMA devices

Both nodes:
```bash
ibdev2netdev
```
Four devices.

## 8. RDMA perftest

Hoedus:
```bash
cd ~/fsaii/perftest-suite-dgx-spark-cluster
./perftest-suite.sh host --run-name dual-200Gb-Oct-8/run-001 --modes isolation,concurrent
```

Diya:
```bash
cd ~/fsaii/perftest-suite-dgx-spark-cluster
./perftest-suite.sh client --run-name dual-200Gb-Oct-8/run-001 --modes isolation,concurrent
```

Check `perftest-results/dual-200Gb-Oct-8/host/run-001/results.csv`. Each device's
isolation bandwidth should be a stable fraction of its `ibv_devinfo` link rate,
and the concurrent aggregate should approach the sum of the isolation rates.
`ib_write_lat` under 2 us.

## 9. NCCL all-reduce

Diya:
```bash
NCCL_DEBUG=INFO bash scripts/interconnect_microbench.sh
```

`NCCL INFO NET/IB : Using` must list four devices. Bus bandwidth at 1 GiB near
100 GB/s. If fewer than four devices appear:
```bash
export NCCL_IB_HCA="rocep1s0f0,roceP2p1s0f0,rocep1s0f1,roceP2p1s0f1"
```
Rerun step 9 after exporting.

## 10. Ray cluster

Diya:
```bash
scripts/start_ray.sh head
```

Hoedus:
```bash
scripts/start_ray.sh worker
```

Diya, verify:
```bash
docker exec vllm-spark ray status
```
Two nodes `Active`.

## 11. Ray all-reduce

Diya:
```bash
bash scripts/ray_allreduce_bench.sh
```

`results/allreduce_ray_*.csv` bus bandwidth at 1 GiB must reflect four ports.

## 12. Pre-download

Diya:
```bash
scripts/predownload.sh all
scripts/mirror_worker_cache.sh all
```

`predownload.sh` queries the HF API for every unique repo's size in a prefetch
phase before any download starts. An API failure in that phase exits before the
first download. The script then checks available disk space plus OS buffer.

## 13. Canary (quick pre-flight test)

Launches every model, validates the streaming contract, tears down. No
measurement. Run before the campaign to catch config and memory issues:
```bash
CANARY_ONLY=1 bash scripts/run_models.sh single
CANARY_ONLY=1 bash scripts/run_models.sh tp2
```

## 14. Campaign 

Runs the full matrix: tp2, pp2, single. All models, all concurrencies.
`--skip-done` active. The process must survive SSH disconnects, run under `tmux`,
On Diya:
```bash
OUT_DIR="results" bash scripts/run_job.sh
```
