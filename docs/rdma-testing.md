# RDMA Testing
## Tracking the things I am doing in order to setup and test RDMA

### Update to latest firmware:
- run `fwupdmgr get-upgrades` to query for available firmware
- run `fwupdmgr update <DEVICE (OPTIONAL)>` to update firmware. If you only want to update a certain device, pass in the device name as the optional argument

### Use DGX Dashboard to get newest software
- navigate to `localhost:11000/updates` and "Update"

### Setup users on both machines (same username)
- Add user to sudo group

### Clone Nvidia's guide:
```bash
git clone git@github.com:NVIDIA/dgx-spark-playbooks
```

### Following [NVIDIA Connect Two Sparks](https://github.com/NVIDIA/dgx-spark-playbooks/tree/main/nvidia/connect-two-sparks)

### Netplan Setup
- NOTE: I plugged the single QSFP cable into the leftmost port on both machines. If you plug it into the rightmost port, the you will use the ...1np1 interfaces instead of the 0np0 interfaces

ON DIYA
```bash
sudo tee /etc/netplan/40-cx7.yaml > /dev/null <<EOF
network:
  version: 2
  ethernets:
    enp1s0f0np0:
      addresses:
        - 10.10.10.1/24
      dhcp4: no
      mtu: 4096
    enP2p1s0f0np0:
      addresses:
        - 10.10.11.1/24
      dhcp4: no
      mtu: 4096
    enp1s0f1np1:
      addresses:
        - 10.10.12.1/24
      dhcp4: no
      mtu: 4096
    enP2p1s0f1np1:
      addresses:
        - 10.10.13.1/24
      dhcp4: no
      mtu: 4096
EOF

sudo chmod 600 /etc/netplan/40-cx7.yaml
sudo netplan apply
```
ON HOEDUS
```bash
## Create the netplan configuration file
sudo tee /etc/netplan/40-cx7.yaml > /dev/null <<EOF
network:
  version: 2
    enp1s0f0np0:
      addresses:
        - 10.10.10.2/24
      dhcp4: no
      mtu: 4096
    enP2p1s0f0np0:
      addresses:
        - 10.10.11.2/24
      dhcp4: no
      mtu: 4096
    enp1s0f1np1:
      addresses:
        - 10.10.12.2/24
      dhcp4: no
      mtu: 4096
    enP2p1s0f1np1:
      addresses:
        - 10.10.13.2/24
      dhcp4: no
      mtu: 4096
EOF

## Set appropriate permissions
sudo chmod 600 /etc/netplan/40-cx7.yaml

## Apply the configuration
sudo netplan apply
```

### Spark Discovery
Now we can run a script provided by NVIDIA to allow the sparks to discover eachother. On ONE of the nodes, run:
```bash
  bash dgx-spark-playbooks/nvidia/connect-two-sparks/assets/discover-sparks
```
Now the machines can ssh into eachother without passwords

### Rollback:
If you wish to remove the configuration we have just completed, run these commands on both nodes:
```bash
    sudo rm /etc/netplan/40-cx7.yaml
    sudo netplan apply
```

## Performance Benchmarking Guide (Following [performance benchmarking guide](https://github.com/NVIDIA/dgx-spark-playbooks/blob/main/nvidia/connect-two-sparks/assets/performance_benchmarking_guide.md))

NOTE for the upcoming write/read tests, you can also pass in `-p <port>` if the default of 18515 is no good

NOTE you could also test with HOEDUS being the host and DIYA being the client, if you reverse the commands and pass `10.10.10.2` to DIYA. You can also use the other interface `roceP2p1s0f0` or whatever the name of the other device from `ibdev2netdev` is

## RDMA Write Latency Test

ON DIYA
```bash
    ib_write_lat -d rocep1s0f0 -i 1 -F
```
replace rocep1s0f0 with whatever RDMA device is 'UP' from `ibdev2netdev`

ON HOEDUS
```bash
    ib_write_lat -d rocep1s0f0 -i 1 -F 10.10.10.1
```

## RDMA Read Latency Test

ON DIYA
```bash
    ib_read_lat -d rocep1s0f0 -i 1 -F
```
replace rocep1s0f0 with whatever RDMA device is 'UP' from `ibdev2netdev`

ON HOEDUS
```bash
    ib_read_lat -d rocep1s0f0 -i 1 -F 10.10.10.1
```

## RDMA Write Bandwidth Test

ON DIYA
```bash
    ib_write_bw -d rocep1s0f0 -i 1 -F
```
replace rocep1s0f0 with whatever RDMA device is 'UP' from `ibdev2netdev`

ON HOEDUS
```bash
    ib_write_bw -d rocep1s0f0 -i 1 -F 10.10.10.1
```

## RDMA Read Bandwidth Test

ON DIYA
```bash
    ib_read_bw -d rocep1s0f0 -i 1 -F
```
replace rocep1s0f0 with whatever RDMA device is 'UP' from `ibdev2netdev`

ON HOEDUS
```bash
    ib_read_bw -d rocep1s0f0 -i 1 -F 10.10.10.1
```

There are also helpers in `ib-test-scripts/` for running tests on both interfaces at the same time.

On Host
```bash
bash host_ib_test.sh <ib command you wish to test>
```
On Client
```bash
bash client_ib_test.sh <ib command you wish to test>
```
