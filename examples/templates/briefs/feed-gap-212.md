# Bug 212: the A-feed receiver reports gaps that are not on the wire

Reported by market-data operations, 2026-09-30.

The A-feed receiver (`src/feedrx/`, AF_XDP zero-copy on `ens1f0` queue 3; group 239.1.1.7, port
30107, VLAN 210) raises `GAP after seq N` alarms and requests retransmission several times an
hour, nearly all within a second of the open. Its own explanation, in the alarm log, is "upstream
gap": its `rx_drops` counter stays at zero. The B feed, on a plain UDP socket on the same host,
shows no gap at the same moments.

Evidence attached:

- `tests/fixtures/captures/feed-a-0930-open.pcapng`, taken on the switch SPAN port of the
  receiver's link (not on the host: zero-copy AF_XDP frames never reach tcpdump there), 09:29:50
  to 09:31:10. Every sequence number is on the wire:
  `tshark -r tests/fixtures/captures/feed-a-0930-open.pcapng -Y 'udp.dstport == 30107' -T fields
  -e frame.number -e frame.time_epoch -e data.data` shows seq 1048575 to 1048831 in frames 18211
  to 18467, in order, at about 2 microseconds apart: a burst of 257 datagrams.
- `ethtool -S ens1f0` before and after the open: `rx_xsk_buff_alloc_err` on queue 3 rose by 3,112;
  `rx_dropped` and `netstat -su` receive errors did not move.
- `perf record -e xdp:xdp_exception -e skb:kfree_skb` during a replay with `tcpreplay
  --topspeed` shows no XDP exception and no socket drop.

Our reading so far: the driver ran out of fill-ring buffers during the burst, so the loss is
between the NIC and the receiver, not upstream, and invisible to the receiver's own counter. That
reading is not yet proven against the receiver's refill code.

Reproduce it through the receiver's replay harness (`feedrx.sim.replay_pcap(path, ring_size=...)`,
which drives the real ring-handling code against the capture's inter-arrival times), the entry
point the operations tool uses; not with live multicast. The alarm text is the symptom. Say which
capture point and which counter each conclusion rests on.
