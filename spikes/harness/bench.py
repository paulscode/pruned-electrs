#!/usr/bin/env python3
"""Per-request cost of the three block-fetch paths, on loopback.

Regtest blocks are tiny, so this deliberately measures *per-request overhead*
rather than throughput — which is the dominant cost for the bootstrap question,
since a full-chain fetch is ~900k individual getblock calls. Loopback peers mean
zero network latency, so the peer-fetch number here is a floor, not a forecast.
"""
import json
import os
import statistics
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE = open(f"{HERE}/nodeB/regtest/.cookie").read().strip()
BITCOIND_B = "http://127.0.0.1:19011/"
PROXY = "http://127.0.0.1:19013/"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 100


def rpc(url, method, params):
    body = json.dumps({"jsonrpc": "1.0", "id": "b", "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "text/plain"})
    import base64
    req.add_header("Authorization", "Basic " + base64.b64encode(COOKIE.encode()).decode())
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def hashes_at(url, heights):
    return [rpc(url, "getblockhash", [h])["result"] for h in heights]


def timed(url, hs, label):
    lat = []
    total_bytes = 0
    errors = 0
    t0 = time.time()
    for h in hs:
        s = time.time()
        r = rpc(url, "getblock", [h, 0])
        lat.append((time.time() - s) * 1000)
        if r.get("error"):
            errors += 1
        else:
            total_bytes += len(r["result"]) // 2
    wall = time.time() - t0
    lat.sort()
    print(
        f"{label:<38} n={len(hs):<4} {wall:6.2f}s  "
        f"{len(hs)/wall:7.1f} blk/s  "
        f"median {statistics.median(lat):6.2f}ms  p95 {lat[int(len(lat)*0.95)-1]:7.2f}ms  "
        f"errors={errors}"
    )
    return len(hs) / wall


def main():
    info = rpc(BITCOIND_B, "getblockchaininfo", [])["result"]
    ph, tip = info["pruneheight"], info["blocks"]
    print(f"node B: tip={tip} pruneheight={ph}\n")

    pruned_heights = list(range(1, 1 + N))
    kept_heights = list(range(ph, ph + N))
    ph_hashes = hashes_at(BITCOIND_B, pruned_heights)
    kt_hashes = hashes_at(BITCOIND_B, kept_heights)

    timed(BITCOIND_B, kt_hashes, "retained, direct to bitcoind")
    timed(PROXY, kt_hashes, "retained, through proxy")
    timed(PROXY, ph_hashes, "PRUNED, through proxy (peer fetch)")


if __name__ == "__main__":
    main()
