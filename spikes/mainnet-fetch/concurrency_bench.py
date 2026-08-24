#!/usr/bin/env python3
"""Does fetching historical blocks in parallel actually scale?

§7a showed per-block latency is round-trip-bound and flat in block size, which
predicts near-linear speedup from concurrency — and that prediction is what
decides whether the RPC path can ever carry a cold bootstrap (40.5h sequential)
or is only ever a repair/query path.

Two shapes are measured, because they answer different questions:

  --mode peers    N blocks in flight across N *different* peers, one connection
                  each. This is the ceiling: what the proxy could reach if it
                  parallelised across its peer set.
  --mode single   N blocks in flight down ONE connection, pipelined. This is
                  what a single peer will tolerate, and it is the cheaper thing
                  to implement.

Read-only: connects to public peers, requests blocks, nothing else.
"""
import argparse
import statistics
import sys
import threading
import time

from fetch_bench import (
    Peer, build_index, connect_one, discover, discover_onion, varint,
    MSG_WITNESS_BLOCK,
)
import struct


def fetch_serial(peer, hashes):
    t0 = time.time()
    got = 0
    for h in hashes:
        ms, body = peer.get_block(h)
        if body:
            got += len(body)
    return time.time() - t0, got


def fetch_pipelined(peer, hashes):
    """All getdata out first, then drain. One connection, N in flight."""
    t0 = time.time()
    for h in hashes:
        inv = varint(1) + struct.pack("<I", MSG_WITNESS_BLOCK) + h
        peer.send("getdata", inv)
    got = 0
    remaining = len(hashes)
    while remaining:
        command, body = peer.recv()
        if command == "block":
            got += len(body)
            remaining -= 1
        elif command == "ping":
            peer.send("pong", body)
        elif command == "notfound":
            remaining -= 1
    return time.time() - t0, got


def fetch_across_peers(peers, hashes):
    """A shared work queue drained by one worker per peer.

    Not one-block-per-peer: peer quality varies by more than an order of
    magnitude, so a static assignment makes total time the *slowest* peer's
    time and measures nothing useful. A queue lets fast peers do more work and
    is what any real implementation would do anyway.
    """
    import queue

    q = queue.Queue()
    for h in hashes:
        q.put(h)
    got = [0]
    errors = []
    lock = threading.Lock()

    def worker(peer):
        while True:
            try:
                h = q.get_nowait()
            except queue.Empty:
                return
            try:
                _, body = peer.get_block(h)
                with lock:
                    got[0] += len(body) if body else 0
            except Exception as e:  # noqa: BLE001 - a dead peer is a data point
                with lock:
                    errors.append(str(e))
                q.put(h)  # let a healthy worker retry it
                return     # and retire this connection

    t0 = time.time()
    threads = [threading.Thread(target=worker, args=(p,)) for p in peers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return time.time() - t0, got[0], errors


def warm_pool(addrs, want, probe_hash, existing=None, proxy=None, timeout=20):
    """Build a pool of peers that have each proved they can serve a block.

    Dropping non-responders up front separates 'does concurrency scale' from
    'how many public peers are junk', which are different questions.
    """
    pool = list(existing or [])
    attempts = 0
    while len(pool) < want and attempts < want * 6:
        attempts += 1
        p = connect_one(addrs, nodelay=True, proxy=proxy, timeout=timeout)
        if p is None:
            break
        try:
            p.sock.settimeout(timeout)
            _, body = p.get_block(probe_hash)
            if body:
                pool.append(p)
            else:
                p.close()
        except Exception:  # noqa: BLE001
            p.close()
    return pool


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["single", "peers"], default="single")
    ap.add_argument("--levels", default="1,2,4,8,16")
    ap.add_argument("--blocks", type=int, default=16, help="blocks per level")
    ap.add_argument("--start-height", type=int, default=700000)
    ap.add_argument("--tor", metavar="HOST:PORT", default=None,
                    help="route through this SOCKS5 proxy and use onion peers")
    args = ap.parse_args()
    timeout = 30 if args.tor else 20

    levels = [int(x) for x in args.levels.split(",")]

    print("discovering peers…", flush=True)
    addrs = discover_onion() if args.tor else discover()
    primary = connect_one(addrs, nodelay=True, proxy=args.tor, timeout=timeout)
    if primary is None:
        print("no NODE_NETWORK peer reachable", file=sys.stderr)
        return 1
    print(f"  primary {primary.addr}", flush=True)

    top = args.start_height + args.blocks + 1
    print(f"walking headers to {top}…", flush=True)
    hashes = build_index(primary, top, log=False)
    window = hashes[args.start_height:args.start_height + args.blocks]
    print(f"  using heights {args.start_height}..{args.start_height + args.blocks - 1}\n", flush=True)

    if args.mode == "single":
        print("one connection, N blocks in flight (pipelined)")
        print(f"{'N':>4} {'wall':>8} {'blk/s':>8} {'MB/s':>7}  speedup")
        base = None
        for n in levels:
            chunk = window[:n]
            wall, got = fetch_serial(primary, chunk) if n == 1 else fetch_pipelined(primary, chunk)
            rate = n / wall
            if base is None:
                base = rate
            print(f"{n:>4} {wall:>7.2f}s {rate:>8.2f} {got/1e6/wall:>7.2f}  {rate/base:>5.2f}x", flush=True)
    else:
        print(f"shared queue of {args.blocks} blocks, drained by N peer workers")
        print(f"{'N':>4} {'peers':>6} {'wall':>8} {'blk/s':>8} {'MB/s':>7}  speedup  dropped")
        base = None
        pool = [primary]
        probe = hashes[args.start_height - 1]
        for n in levels:
            pool = warm_pool(addrs, n, probe, existing=[p for p in pool],
                             proxy=args.tor, timeout=timeout)
            if len(pool) < n:
                print(f"{n:>4} {len(pool):>6}   — only {len(pool)} healthy peers found, stopping",
                      flush=True)
                break
            wall, got, errs = fetch_across_peers(pool[:n], list(window))
            rate = args.blocks / wall
            if base is None:
                base = rate
            print(f"{n:>4} {n:>6} {wall:>7.2f}s {rate:>8.2f} {got/1e6/wall:>7.2f}  "
                  f"{rate/base:>5.2f}x  {len(errs)}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
