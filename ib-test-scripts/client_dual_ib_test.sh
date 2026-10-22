#!/usr/bin/env bash
# Legacy Diya/Hoedus example with fixed RDMA devices and peer IPs.
# Use ../perftest-suite.sh for arbitrary hosts, interfaces, and topologies.
#

if [ ! -n "$1" ]; then
	echo "Usage: $0 <ib test command>"
	exit 1
fi

if [[ $1 != "ib_"* ]]; then
	echo "Please provide a valid infiniband test command"
	exit 1
fi

if command -v "$1" >/dev/null 2>&1; then
	echo "$1 command found"
else
	echo "$1 command not found on system"
	exit 1
fi


if [[ -f /tmp/bw1.log ]]; then
	rm /tmp/bw1.log
fi

if [[ -f /tmp/bw2.log ]]; then
	rm /tmp/bw2.log
fi

$1 -d rocep1s0f0 -i 1 -F 10.10.10.1 -p 18515 > /tmp/bw1.log 2>&1 &
$1 -d roceP2p1s0f0 -i 1 -F 10.10.11.1 -p 18516 > /tmp/bw2.log 2>&1 &
wait
echo "INTERFACE 1"
tail -n 5 /tmp/bw1.log
echo "INTERFACE 2"
tail -n 5 /tmp/bw2.log
