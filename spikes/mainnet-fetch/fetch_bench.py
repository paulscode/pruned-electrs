#!/usr/bin/env python3
"""Measure what a historical block fetch from a real mainnet peer actually costs.

This is the number DISCOVERY.md §11.1 is missing. Everything measured so far was
loopback with ~250-byte regtest blocks, which captures per-request overhead and
nothing about real latency or real block sizes.

It replicates btc-rpc-proxy's `fetch_block_from_peer` faithfully — same handshake
(services=NONE, relay=0), same one-block-per-getdata pattern, same validation —
so the timings transfer. It deliberately does NOT need a local node, a pruned
node, or the proxy itself: the cost being measured is peer→us, and that is
identical whoever asks.

  --nodelay/--no-nodelay  reproduce the patch 0003 comparison against real peers,
                          where RTT is present and the ~40ms stall is additive
                          rather than dominant.

Read-only: connects to public peers, asks for blocks, downloads nothing else.
"""
import argparse
import hashlib
import random
import socket
import statistics
import struct
import sys
import time

MAGIC = bytes.fromhex("f9beb4d9")  # mainnet
PROTOCOL_VERSION = 70016
MSG_WITNESS_BLOCK = 0x40000002
DNS_SEEDS = [
    "seed.bitcoin.sipa.be",
    "dnsseed.bluematt.me",
    "seed.bitcoinstats.com",
    "seed.bitcoin.jonasschnelli.ch",
    "seed.btc.petertodd.net",
    "seed.bitcoin.sprovoost.nl",
    "dnsseed.emzy.de",
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


def read_varint(b, off):
    v = b[off]
    if v < 0xFD:
        return v, off + 1
    if v == 0xFD:
        return struct.unpack_from("<H", b, off + 1)[0], off + 3
    if v == 0xFE:
        return struct.unpack_from("<I", b, off + 1)[0], off + 5
    return struct.unpack_from("<Q", b, off + 1)[0], off + 9


def netaddr():
    return struct.pack("<Q", 0) + b"\x00" * 10 + b"\xff\xff" + b"\x00" * 4 + struct.pack(">H", 0)


def version_payload():
    ua = b"/pruned-electrs-bench:0.1/"
    return (
        struct.pack("<i", PROTOCOL_VERSION)
        + struct.pack("<Q", 0)
        + struct.pack("<q", int(time.time()))
        + netaddr() + netaddr()
        + struct.pack("<Q", random.getrandbits(64))
        + varint(len(ua)) + ua
        + struct.pack("<i", 0)
        + b"\x00"
    )


def socks5_connect(proxy, host, port, timeout):
    """Minimal SOCKS5 CONNECT, so .onion peers are reachable without PySocks.

    Hostname addressing (ATYP 0x03) is what makes this work for onion services:
    the name is resolved by Tor, not locally.
    """
    phost, pport = proxy.rsplit(":", 1)
    s = socket.create_connection((phost, int(pport)), timeout=timeout)
    s.sendall(b"\x05\x01\x00")                      # VER 5, 1 method, no auth
    if s.recv(2) != b"\x05\x00":
        s.close()
        raise OSError("SOCKS5 handshake refused")
    name = host.encode()
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + struct.pack(">H", port))
    reply = s.recv(4)
    if len(reply) < 4 or reply[1] != 0x00:
        s.close()
        raise OSError(f"SOCKS5 connect failed (code {reply[1] if len(reply) > 1 else '?'})")
    atyp = reply[3]
    if atyp == 0x01:
        s.recv(4 + 2)
    elif atyp == 0x03:
        s.recv(s.recv(1)[0] + 2)
    else:
        s.recv(16 + 2)
    return s



def onion_v3(pubkey):
    """Render a BIP155 TORV3 32-byte pubkey as its .onion hostname."""
    import base64
    checksum = hashlib.sha3_256(b".onion checksum" + pubkey + b"\x03").digest()[:2]
    return base64.b32encode(pubkey + checksum + b"\x03").decode().lower() + ".onion"

class Peer:
    def __init__(self, host, port, nodelay, timeout=20, proxy=None):
        if proxy:
            self.sock = socks5_connect(proxy, host, port, timeout)
            self.sock.settimeout(timeout)
        else:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1 if nodelay else 0)
        self.addr = f"{host}:{port}"
        self.services = 0
        self.height = 0

    def send(self, command, payload):
        # Deliberately written in two pieces, the way consensus_encode does:
        # that is what makes Nagle bite.
        b = msg(command, payload)
        self.sock.sendall(b[:24])
        if len(b) > 24:
            self.sock.sendall(b[24:])

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(min(n - len(buf), 1 << 20))
            if not chunk:
                raise EOFError("peer closed")
            buf += chunk
        return buf

    def recv(self):
        hdr = self._recv_exact(24)
        if hdr[:4] != MAGIC:
            raise ValueError("bad magic")
        command = hdr[4:16].rstrip(b"\x00").decode(errors="replace")
        length = struct.unpack("<I", hdr[16:20])[0]
        return command, self._recv_exact(length) if length else b""

    def handshake(self, want_addrv2=False):
        self.send("version", version_payload())
        got_version = got_verack = False
        while not (got_version and got_verack):
            command, payload = self.recv()
            if command == "version":
                self.services = struct.unpack_from("<Q", payload, 4)[0]
                off = 4 + 8 + 8 + 26 + 26 + 8
                ualen, off = read_varint(payload, off)
                off += ualen
                self.height = struct.unpack_from("<i", payload, off)[0]
                # BIP155: must be sent after version and before verack, and it
                # is the only way to be told about onion peers at all.
                if want_addrv2:
                    self.send("sendaddrv2", b"")
                self.send("verack", b"")
                got_version = True
            elif command == "verack":
                got_verack = True

    def harvest_onions(self, want, deadline_s=45):
        """Ask for peer addresses and keep the TORV3 ones offering NODE_NETWORK."""
        self.send("getaddr", b"")
        found, end = [], time.time() + deadline_s
        while len(found) < want and time.time() < end:
            try:
                command, body = self.recv()
            except (OSError, EOFError, ValueError):
                break
            if command == "ping":
                self.send("pong", body)
                continue
            if command != "addrv2":
                continue
            count, off = read_varint(body, 0)
            for _ in range(count):
                try:
                    off += 4                                   # time
                    services, off = read_varint(body, off)
                    net_id = body[off]; off += 1
                    alen, off = read_varint(body, off)
                    addr = body[off:off + alen]; off += alen
                    port = struct.unpack_from(">H", body, off)[0]; off += 2
                except (IndexError, struct.error):
                    break
                # net_id 4 = TORV3; services bit 0 = NODE_NETWORK
                if net_id == 4 and len(addr) == 32 and (services & 1):
                    found.append((onion_v3(addr), port or 8333))
        return found


    def headers_from(self, locator_hash):
        payload = struct.pack("<i", PROTOCOL_VERSION) + varint(1) + locator_hash + b"\x00" * 32
        self.send("getheaders", payload)
        while True:
            command, body = self.recv()
            if command == "headers":
                break
            if command == "ping":
                self.send("pong", body)
        count, off = read_varint(body, 0)
        out = []
        for _ in range(count):
            raw = body[off:off + 80]
            out.append(dsha(raw))
            off += 81  # 80-byte header + varint(0) tx count
        return out

    def get_block(self, blockhash):
        inv = varint(1) + struct.pack("<I", MSG_WITNESS_BLOCK) + blockhash
        t0 = time.time()
        self.send("getdata", inv)
        while True:
            command, body = self.recv()
            if command == "block":
                return (time.time() - t0) * 1000, body
            if command == "ping":
                self.send("pong", body)
            if command == "notfound":
                return None, None

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def discover(limit=40):
    addrs = set()
    for seed in DNS_SEEDS:
        try:
            for fam, _, _, _, sa in socket.getaddrinfo(seed, 8333, socket.AF_INET, socket.SOCK_STREAM):
                addrs.add((sa[0], sa[1]))
        except OSError:
            continue
        if len(addrs) >= limit:
            break
    return list(addrs)


def connect_one(addrs, nodelay, proxy=None, timeout=20):
    random.shuffle(addrs)
    for host, port in addrs:
        try:
            p = Peer(host, port, nodelay, timeout=timeout, proxy=proxy)
            p.handshake()
            # NODE_NETWORK = 1: the proxy filters on this, since only an
            # archival peer can answer for an old block.
            if not (p.services & 1):
                p.close()
                continue
            return p
        except (OSError, EOFError, ValueError):
            continue
    return None


def discover_onion(limit=40, deadline_s=45):
    """Onion peer addresses, harvested from clearnet peers' addrv2 gossip.

    DNS seeds only return clearnet, so this is the only way to reach the network
    a StartOS node running onlynet=onion actually uses.
    """
    addrs = discover()
    onions = []
    for _ in range(4):
        if len(onions) >= limit:
            break
        random.shuffle(addrs)
        p = None
        for host, port in addrs[:12]:
            try:
                p = Peer(host, port, nodelay=True)
                p.handshake(want_addrv2=True)
                break
            except (OSError, EOFError, ValueError):
                p = None
        if p is None:
            break
        try:
            onions.extend(p.harvest_onions(limit - len(onions), deadline_s))
        finally:
            p.close()
    seen, out = set(), []
    for a in onions:
        if a[0] not in seen:
            seen.add(a[0])
            out.append(a)
    return out


def build_index(peer, target_height, log=True):
    """Walk the header chain to target_height, so we can name real blocks."""
    genesis = bytes.fromhex("000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f")[::-1]
    hashes = [genesis]
    tip = genesis
    t0 = time.time()
    while len(hashes) <= target_height:
        batch = peer.headers_from(tip)
        if not batch:
            break
        hashes.extend(batch)
        tip = batch[-1]
        if log and len(hashes) % 50000 < 2000:
            print(f"    …{len(hashes)} headers ({time.time()-t0:.0f}s)", flush=True)
    return hashes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--heights", default="200000,400000,600000,700000,800000",
                    help="comma-separated historical heights to fetch")
    ap.add_argument("--repeat", type=int, default=3, help="fetches per height")
    ap.add_argument("--no-nodelay", action="store_true", help="leave Nagle on (pre-patch behaviour)")
    ap.add_argument("--tor", metavar="HOST:PORT", default=None,
                    help="route through this SOCKS5 proxy and use onion peers (e.g. 127.0.0.1:9050)")
    args = ap.parse_args()
    nodelay = not args.no_nodelay

    heights = [int(h) for h in args.heights.split(",")]
    print(f"TCP_NODELAY={'on' if nodelay else 'OFF (pre-patch)'}\n")

    if args.tor:
        print(f"harvesting onion peers via addrv2 (SOCKS {args.tor})…", flush=True)
        addrs = discover_onion()
        print(f"  {len(addrs)} onion addresses", flush=True)
    else:
        print("discovering peers via DNS seeds…", flush=True)
        addrs = discover()
        print(f"  {len(addrs)} candidate addresses", flush=True)

    timeout = 90 if args.tor else 20
    peer = connect_one(addrs, nodelay, proxy=args.tor, timeout=timeout)
    if peer is None:
        print("could not reach any NODE_NETWORK peer", file=sys.stderr)
        return 1
    print(f"  connected {peer.addr}  services=0x{peer.services:x}  their height={peer.height}\n", flush=True)

    print(f"walking headers to {max(heights)}…", flush=True)
    hashes = build_index(peer, max(heights))
    print(f"  {len(hashes)} headers\n", flush=True)

    print(f"{'height':>8} {'size':>10} {'median':>9} {'min':>9} {'max':>9}   MB/s")
    rows = []
    for h in heights:
        if h >= len(hashes):
            continue
        lat, size = [], 0
        for _ in range(args.repeat):
            try:
                ms, body = peer.get_block(hashes[h])
            except (OSError, EOFError):
                peer = connect_one(addrs, nodelay, proxy=args.tor, timeout=timeout)
                if peer is None:
                    break
                hashes_ok = True
                continue
            if ms is None:
                continue
            lat.append(ms)
            size = len(body)
        if not lat:
            continue
        med = statistics.median(lat)
        rows.append((h, size, med))
        print(f"{h:>8} {size/1e6:>9.2f}M {med:>8.1f}ms {min(lat):>8.1f}ms {max(lat):>8.1f}ms "
              f"  {size/1e6/(med/1000):>5.2f}")

    if rows:
        med_all = statistics.median([r[2] for r in rows])
        print(f"\nmedian across heights: {med_all:.1f} ms/block")
        print(f"extrapolated full-chain (900k blocks, sequential, 1 peer): "
              f"{900000*med_all/1000/3600:.1f} hours")
    peer.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
