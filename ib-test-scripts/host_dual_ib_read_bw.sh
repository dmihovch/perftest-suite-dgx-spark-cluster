#!/usr/bin/env bash
#
#
#


if [[ -f /tmp/bw1.log ]]; then
	rm /tmp/bw1.log
fi

if [[ -f /tmp/bw2.log ]]; then
	rm /tmp/bw2.log
fi

ib_read_bw -d rocep1s0f0 -i 1 -F -p 18515 > /tmp/bw1.log 2>&1 &
ib_read_bw -d roceP2p1s0f0 -i 1 -F -p 18516 > /tmp/bw2.log 2>&1 & 
wait
echo "INTERFACE 1"
tail -n 5 /tmp/bw1.log
echo "INTERFACE 2"
tail -n 5 /tmp/bw2.log
