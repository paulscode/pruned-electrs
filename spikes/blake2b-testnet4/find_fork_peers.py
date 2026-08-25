#!/usr/bin/env python3
"""Find testnet4 peers that are actually on the BLAKE2b fork.

The BLAKE2b chain shares testnet4's genesis block, magic bytes and default port,
so its nodes sit on the same network as ordinary testnet4 nodes and gossip
addresses with them. That is a problem for a node trying to sync (the DNS seeds
hand back mostly non-fork peers, and it stalls at 149536), but it is an
opportunity here: the fork nodes are reachable through exactly the same discovery
path, they just have to be told apart afterwards.

Telling them apart is decisive rather than heuristic. Ask each peer for the
headers following block 149536, the last block both chains agree on:

  - a fork node answers with a 164-byte header v2, hashed with BLAKE2b
  - a Core or pre-fork node answers with an 80-byte header, hashed SHA256d
  - a node that has not synced that far answers with nothing

The header's own version field carries which it is, in the first four bytes, so
no guessing is involved. The user agent is reported too but is not the test: a
node can run the fork build and still be following the other chain.

  ./find_fork_peers.py [--seeds] [--limit N] [--workers N] [--out peers.txt]

With no arguments it reads candidate addresses from the DNS seeds, crawls one
round of `getaddr` gossip, and probes everything it finds.
"""
import argparse
import hashlib
import json
import random
import socket
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor

# testnet4. Same for the fork, which is the whole difficulty.
MAGIC = bytes.fromhex("1c163f28")
DEFAULT_PORT = 48333
PROTOCOL_VERSION = 70016

# Height 149537 is the first BLAKE2b block, so 149536 is the last block both
# chains have. The locator has to be the *common* one: a Core node has never
# heard of 149537 and would answer from genesis instead of from the fork point.
# Derived from block 149537's own prev_blockhash field, not looked up.
FORK_HEIGHT = 149537
LAST_COMMON_HEIGHT = 149536
LAST_COMMON_HASH = "0000000000601b1b360b505bd6d999c450fd5bc1ec48cfbcefea599b25dc1951"
FORK_BLOCK_HASH = "000000000068f60429c933dc0c8befbcc7edadb1cf8f8d0d7804c608fd736d82"

VERSION_HEADER_V2_FLAG = 0x8000_0000

DNS_SEEDS = [
    "seed.testnet4.bitcoin.sprovoost.nl",
    "seed.testnet4.wiz.biz",
]


def dsha(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def msg(command, payload):
    cmd = command.encode() + b"\x00" * (12 - len(command))
    return MAGIC + cmd + struct.pack("<I", len(payload)) + dsha(payload)[:4] + payload


def varint(n):
    if n < 0xFD:
        return struct.pack("<B", n)
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def read_varint(b, o):
    v = b[o]
    if v < 0xFD:
        return v, o + 1
    if v == 0xFD:
        return struct.unpack_from("<H", b, o + 1)[0], o + 3
    if v == 0xFE:
        return struct.unpack_from("<I", b, o + 1)[0], o + 5
    return struct.unpack_from("<Q", b, o + 1)[0], o + 9


def netaddr():
    return struct.pack("<Q", 0) + b"\x00" * 10 + b"\xff\xff" + b"\x00" * 4 + struct.pack(">H", 0)


def version_payload():
    ua = b"/blake2b-peer-probe:0.1/"
    return (
        struct.pack("<i", PROTOCOL_VERSION)
        + struct.pack("<Q", 0)
        + struct.pack("<q", int(time.time()))
        + netaddr()
        + netaddr()
        + struct.pack("<Q", 0x5EED)
        + varint(len(ua)) + ua
        + struct.pack("<i", 0)
        + b"\x00"  # relay = false; we want headers, not a mempool feed
    )


def parse_version(payload):
    o = 4 + 8 + 8 + 26 + 26 + 8
    n, o = read_varint(payload, o)
    ua = payload[o:o + n].decode("ascii", "replace")
    o += n
    start_height = struct.unpack_from("<i", payload, o)[0]
    return ua, start_height


def recv_exact(sock, n, deadline):
    buf = b""
    while len(buf) < n:
        sock.settimeout(max(0.1, deadline - time.monotonic()))
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("peer closed")
        buf += chunk
    return buf


def recv_msg(sock, deadline):
    hdr = recv_exact(sock, 24, deadline)
    if hdr[:4] != MAGIC:
        raise ValueError("bad magic %s" % hdr[:4].hex())
    command = hdr[4:16].rstrip(b"\x00").decode("ascii", "replace")
    length = struct.unpack("<I", hdr[16:20])[0]
    if length > 8_000_000:
        raise ValueError("absurd payload %d" % length)
    payload = recv_exact(sock, length, deadline) if length else b""
    return command, payload


def getheaders_payload(locator_hash_hex):
    h = bytes.fromhex(locator_hash_hex)[::-1]  # display order -> internal
    return (
        struct.pack("<I", PROTOCOL_VERSION)
        + varint(1) + h
        + b"\x00" * 32  # hash_stop: none, give us as many as you have
    )


def classify_headers(payload):
    """First header in a `headers` message: (version_flavour, count, size)."""
    count, o = read_varint(payload, 0)
    if count == 0:
        return None, 0, 0
    version = struct.unpack_from("<I", payload, o)[0]
    size = 164 if version & VERSION_HEADER_V2_FLAG else 80
    return ("v2" if size == 164 else "v1"), count, size


def probe(addr, timeout=8.0, want_addrs=False):
    host, port = addr
    deadline = time.monotonic() + timeout
    out = {"addr": f"{host}:{port}", "ok": False, "fork": None,
           "user_agent": None, "height": None, "note": None, "peers": []}
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.sendall(msg("version", version_payload()))

        got_version = False
        while time.monotonic() < deadline:
            command, payload = recv_msg(sock, deadline)
            if command == "version":
                out["user_agent"], out["height"] = parse_version(payload)
                got_version = True
                sock.sendall(msg("verack", b""))
            elif command == "verack":
                if got_version:
                    break
            elif command == "ping":
                sock.sendall(msg("pong", payload[:8]))
        if not got_version:
            out["note"] = "no version message"
            return out

        if want_addrs:
            sock.sendall(msg("getaddr", b""))

        sock.sendall(msg("getheaders", getheaders_payload(LAST_COMMON_HASH)))

        while time.monotonic() < deadline:
            command, payload = recv_msg(sock, deadline)
            if command == "ping":
                sock.sendall(msg("pong", payload[:8]))
            elif command in ("addr", "addrv2"):
                out["peers"] += parse_addrs(command, payload)
            elif command == "headers":
                flavour, count, size = classify_headers(payload)
                out["ok"] = True
                if flavour is None:
                    out["note"] = "no headers past the fork height (not synced that far)"
                    out["fork"] = False
                else:
                    out["fork"] = flavour == "v2"
                    out["note"] = f"{count} headers, first is {size} bytes ({flavour})"
                if not want_addrs:
                    return out
                # else keep reading briefly for gossip
                deadline = min(deadline, time.monotonic() + 2.0)
        return out
    except Exception as e:
        # Do not clobber a result we already have. In gossip mode the socket is
        # deliberately left reading past the point the headers arrived, so a
        # timeout here is the normal way that phase ends, not a failed probe.
        if not out["ok"]:
            out["note"] = f"{type(e).__name__}: {e}"
        return out
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


def parse_addrs(command, payload):
    """Best-effort address extraction. IPv4 only, which is all we can dial here."""
    peers = []
    try:
        count, o = read_varint(payload, 0)
        for _ in range(min(count, 1000)):
            if command == "addr":
                o += 4 + 8  # time, services
                ipbytes = payload[o:o + 16]; o += 16
                port = struct.unpack_from(">H", payload, o)[0]; o += 2
                if ipbytes[:12] == b"\x00" * 10 + b"\xff\xff":
                    peers.append((socket.inet_ntoa(ipbytes[12:]), port))
            else:  # addrv2
                o += 4  # time
                _svc, o = read_varint(payload, o)
                network = payload[o]; o += 1
                n, o = read_varint(payload, o)
                blob = payload[o:o + n]; o += n
                port = struct.unpack_from(">H", payload, o)[0]; o += 2
                if network == 1 and len(blob) == 4:
                    peers.append((socket.inet_ntoa(blob), port))
    except Exception:
        pass
    return peers


def from_dns():
    found = set()
    for seed in DNS_SEEDS:
        try:
            for res in socket.getaddrinfo(seed, DEFAULT_PORT, socket.AF_INET, socket.SOCK_STREAM):
                found.add((res[4][0], DEFAULT_PORT))
        except Exception as e:
            print(f"  seed {seed}: {e}", file=sys.stderr)
    return sorted(found)


def crawl_from_fork(seeds, args):
    """Breadth-first from known fork peers, expanding only through fork peers.

    A fork node's address manager holds both fork and non-fork peers, since the
    two share a network, so this still probes everything it hears about. What it
    does not do is follow gossip out of a *non*-fork node, because that is the
    whole testnet4 address space and the yield is negligible: the broad scan
    covers that case separately.
    """
    confirmed, probed = {}, set()
    frontier = list(seeds)

    for rnd in range(args.rounds):
        frontier = [a for a in frontier if a not in probed]
        if not frontier:
            break
        print(f"round {rnd + 1}: probing {len(frontier)} addresses")
        probed.update(frontier)
        gossip = set()
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for r in ex.map(lambda a: probe(a, args.timeout, want_addrs=True), frontier):
                if r["fork"]:
                    if r["addr"] not in confirmed:
                        print(f"  FORK  {r['addr']:<24} {r['user_agent']}  {r['note']}")
                    confirmed[r["addr"]] = r
                    gossip.update(r["peers"])
        frontier = sorted(gossip)
        print(f"  {len(confirmed)} fork peers so far; {len(frontier)} addresses gossiped by them")

    print(f"\n{len(confirmed)} peers confirmed on the BLAKE2b chain:")
    for a, r in sorted(confirmed.items()):
        print(f"  {a:<24} {r['user_agent']:<40} height {r['height']}")
    if args.out and confirmed:
        with open(args.out, "w") as f:
            for a in sorted(confirmed):
                f.write(a + "\n")
        print(f"\nwrote {len(confirmed)} addresses to {args.out}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument("--out", default=None, help="write confirmed fork peers here")
    ap.add_argument("--json", default=None, help="write the full result set here")
    ap.add_argument("addr", nargs="*", help="probe these host:port instead of crawling")
    ap.add_argument("--from-fork", action="store_true",
                    help="breadth-first crawl outward from the given fork peers, "
                         "following gossip only from peers confirmed on the fork")
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()

    if args.addr:
        candidates = []
        for a in args.addr:
            host, _, port = a.rpartition(":")
            candidates.append((host or a, int(port) if port else DEFAULT_PORT))
        if args.from_fork:
            return crawl_from_fork(candidates, args)
    else:
        print("resolving DNS seeds...")
        seeds = from_dns()
        print(f"  {len(seeds)} addresses from seeds")
        if not seeds:
            print("no seed addresses; pass addresses explicitly", file=sys.stderr)
            return 1
        print("crawling one round of gossip...")
        gossip = set(seeds)
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for r in ex.map(lambda a: probe(a, args.timeout, want_addrs=True), seeds[:40]):
                gossip.update(r["peers"])
        candidates = sorted(gossip)
        # Shuffle before the --limit slice. Sorted order is IP order, which
        # clusters whole hosting ranges together, so a truncated sorted scan
        # samples a handful of datacentres rather than the network.
        random.Random(0).shuffle(candidates)
        print(f"  {len(candidates)} candidates after gossip")

    candidates = candidates[: args.limit]
    print(f"probing {len(candidates)} peers: which chain do they serve after {LAST_COMMON_HEIGHT}?\n")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for r in ex.map(lambda a: probe(a, args.timeout), candidates):
            results.append(r)
            if r["fork"]:
                print(f"  FORK  {r['addr']:<24} {r['user_agent']}  {r['note']}")

    fork = [r for r in results if r["fork"]]
    reachable = [r for r in results if r["ok"]]
    print(f"\n{len(candidates)} probed, {len(reachable)} answered, {len(fork)} on the BLAKE2b chain")

    if reachable and not fork:
        print("\nEvery reachable peer is on the SHA256d chain. Agents seen:")
        seen = {}
        for r in reachable:
            seen[r["user_agent"]] = seen.get(r["user_agent"], 0) + 1
        for ua, n in sorted(seen.items(), key=lambda kv: -kv[1])[:12]:
            print(f"  {n:>4}  {ua}")

    if args.out and fork:
        with open(args.out, "w") as f:
            for r in fork:
                f.write(r["addr"] + "\n")
        print(f"\nwrote {len(fork)} addresses to {args.out}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
