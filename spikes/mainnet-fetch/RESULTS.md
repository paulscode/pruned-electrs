# Measured results

Raw output from the benchmarks in this directory. Reproduce with the commands shown.
Public mainnet peers, clearnet, from a Linux Mint workstation. Peer quality varies by
more than 10x between runs, which is itself one of the findings.

## Sequential fetch latency — `fetch_bench.py`

`python3 fetch_bench.py --heights 200000,400000,600000,700000,800000,900000 --repeat 5`

| height | block size | median | min | max | MB/s |
|---|---|---|---|---|---|
| 200000 | 0.25 MB | 152.6 ms | 151.6 | 172.4 | 1.62 |
| 400000 | 0.95 MB | 159.5 ms | 158.3 | 326.0 | 5.95 |
| 600000 | 0.87 MB | 158.4 ms | 155.8 | 280.4 | 5.50 |
| 700000 | 1.28 MB | 164.6 ms | 162.0 | 166.2 | 7.76 |
| 800000 | 1.63 MB | 166.3 ms | 165.5 | 176.6 | 9.83 |
| 900000 | 1.92 MB | 172.7 ms | 171.2 | 182.9 | 11.12 |

Median 162 ms/block, flat in block size => round-trip-bound. 6.2 blocks/s.

Same, with `--no-nodelay` (upstream proxy behaviour):

| height | block size | median |
|---|---|---|
| 200000 | 0.25 MB | 342.8 ms |
| 400000 | 0.95 MB | 482.6 ms |
| 600000 | 0.87 MB | 499.1 ms |
| 700000 | 1.28 MB | 589.5 ms |
| 800000 | 1.63 MB | 726.0 ms |
| 900000 | 1.92 MB | 931.7 ms |

Median 544 ms/block. Latency tracks size here, but that is not the Nagle penalty: TCP_NODELAY
changes only the proxy's own write, a fixed 61-byte getdata. This run and the patched one used
different peers at different times, so the difference between them is uncontrolled.

## Concurrency scaling — `concurrency_bench.py`

`python3 concurrency_bench.py --mode peers --levels ... --blocks ...`

Shared work queue drained by N peer workers, peers vetted with a probe fetch first.

Run A, heights 700000-700047:

| N | wall | blk/s | MB/s | speedup | dropped |
|---|---|---|---|---|---|
| 1 | 76.21s | 0.63 | 0.59 | 1.00x | 0 |
| 2 | 36.13s | 1.33 | 1.24 | 2.11x | 0 |
| 4 | 10.11s | 4.75 | 4.42 | 7.54x | 0 |
| 8 | 7.09s | 6.77 | 6.30 | 10.75x | 0 |
| 12 | 4.95s | 9.70 | 9.02 | 15.40x | 0 |

Run B, heights 650000-650035:

| N | wall | blk/s | MB/s | speedup | dropped |
|---|---|---|---|---|---|
| 1 | 27.71s | 1.30 | 1.74 | 1.00x | 0 |
| 4 | 5.10s | 7.06 | 9.43 | 5.43x | 0 |
| 12 | 2.73s | 13.21 | 17.66 | 10.16x | 0 |

Run C, heights 750000-750047 — a bad network moment, shown because it is real:

| N | wall | blk/s | MB/s | speedup | dropped |
|---|---|---|---|---|---|
| 12 | 36.19s | 1.33 | 1.73 | 1.00x | 3 |
| 16 | 34.69s | 1.38 | 1.81 | 1.04x | 3 |
| 24 | 35.45s | 1.35 | 1.77 | 1.02x | 4 |

## Pipelining on a single connection — `concurrency_bench.py --mode single`

| N in flight | wall | blk/s | MB/s | speedup |
|---|---|---|---|---|
| 1 | 6.22s | 0.16 | 0.21 | 1.00x |
| 2 | 6.00s | 0.33 | 0.29 | 2.07x |
| 4 | 6.99s | 0.57 | 0.29 | 3.56x |
| 8 | 15.74s | 0.51 | 0.24 | 3.16x |
| 16 | 28.65s | 0.56 | 0.27 | 3.47x |

MB/s is pinned near 0.27 throughout: this peer was bandwidth-capped, and pipelining
saturates the link rather than beating it. Peer-parallelism beats connection-pipelining.

## Over Tor — `fetch_bench.py --tor 127.0.0.1:9050`

Onion peers, harvested from clearnet peers' `addrv2` gossip (DNS seeds return clearnet only).
This is the network a StartOS node running `onlynet=onion` actually uses — as the reference
Start9 test server does.

| height | block size | median | min | max | MB/s |
|---|---|---|---|---|---|
| 400000 | 0.95 MB | 2118.3 ms | 1783.3 | 2578.8 | 0.45 |
| 700000 | 1.28 MB | 2029.2 ms | 1813.4 | 2290.5 | 0.63 |
| 900000 | 1.92 MB | 3667.4 ms | 2529.2 | 3988.2 | 0.52 |

Median 2118 ms/block against 162 ms on clearnet: **13x slower**. Throughput 0.45-0.63 MB/s
against 5.95-11.12: **~13-18x worse**.

The header walk shows the same penalty on pure round-trip work: 900k headers took 439 s over
Tor against 21 s on clearnet, **~21x**.

Extrapolated full chain, sequential, one peer: **529.6 hours — 22 days.**

## Concurrency over Tor — `concurrency_bench.py --mode peers --tor 127.0.0.1:9050`

Heights 200000-200023, shared queue, onion peers.

| N | wall | blk/s | speedup | dropped |
|---|---|---|---|---|
| 1 | 24.62s | 0.97 | 1.00x | 0 |
| 4 | 7.77s | 3.09 | 3.17x | 0 |
| 8 | 4.05s | 5.92 | 6.08x | 0 |

Concurrency helps *more* over Tor than on clearnet, because Tor is heavily round-trip-bound and
parallel circuits mask latency rather than contending for bandwidth. At N=8 a 500-block query
burst drops from ~17 minutes to ~84 seconds.

Full chain at 5.92 blk/s: ~42 hours, against ~530 h sequential. Still not a viable bootstrap,
but for query bursts and gap repair it is the difference between usable and not.

## Reproducing

Clearnet benchmarks need no local node. The Tor ones need a SOCKS proxy (Tor's default
`127.0.0.1:9050`) and harvest onion peers via BIP155 `addrv2` gossip, since DNS seeds return
clearnet only. Onion peer reachability is roughly 25%, so a run may need retrying.
