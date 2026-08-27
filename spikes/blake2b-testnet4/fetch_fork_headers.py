#!/usr/bin/env python3
"""Pull a run of real headers off the BLAKE2b testnet4 chain, over p2p.

Written to feed a client's verification with live data rather than constructed
data. The activation is at 149537, so a locator below it and a `getheaders` gets
a run that spans it: v1 headers, then v2 ones, with the real difficulty targets
and the real timestamps.

`getheaders` needs a locator hash rather than a height, so the run is anchored on
a hash taken from the caller. The peers are the ones find_fork_peers.py
confirmed; a non-fork testnet4 peer answers the same request with the other
chain, which is exactly the thing that makes this worth doing against fork peers
specifically.

  ./fetch_fork_headers.py <locator-hash> [--out headers.json] [--peers fork-peers.txt]

Headers come back with a trailing varint transaction count of zero per header,
and are 80 or 164 bytes depending on bit 31 of their version field, so the run is
walked rather than divided.
"""
import argparse
import hashlib
import json
import socket
import struct
import sys
import time

MAGIC = bytes.fromhex("1c163f28")
PROTOCOL_VERSION = 70016
DEFAULT_PORT = 48333
VERSION_HEADER_V2_FLAG = 0x80000000


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
    ua = b"/blake2b-header-fetch:0.1/"
    return (
        struct.pack("<i", PROTOCOL_VERSION)
        + struct.pack("<Q", 0)
        + struct.pack("<q", int(time.time()))
        + netaddr()
        + netaddr()
        + struct.pack("<Q", 0x5EED)
        + varint(len(ua)) + ua
        + struct.pack("<i", 0)
        + b"\x00"
    )


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
        raise ValueError("wrong network magic: " + hdr[:4].hex())
    command = hdr[4:16].rstrip(b"\x00").decode("ascii", "replace")
    length = struct.unpack_from("<I", hdr, 16)[0]
    if length > 32 * 1024 * 1024:
        raise ValueError(f"{command} payload of {length} bytes is implausible")
    payload = recv_exact(sock, length, deadline) if length else b""
    if dsha(payload)[:4] != hdr[20:24]:
        raise ValueError(f"{command} checksum mismatch")
    return command, payload


def header_length(blob, offset):
    """80 or 164, from the header's own version field. The whole point of the format."""
    version = struct.unpack_from("<I", blob, offset)[0]
    return 164 if version & VERSION_HEADER_V2_FLAG else 80


def split_headers(payload):
    """The `headers` message: a count, then each header followed by a zero tx count."""
    count, o = read_varint(payload, 0)
    out = []
    for i in range(count):
        if o + 4 > len(payload):
            raise ValueError(f"ran out at header {i}")
        length = header_length(payload, o)
        if o + length > len(payload):
            raise ValueError(f"header {i} claims {length} bytes, {len(payload) - o} remain")
        out.append(payload[o:o + length])
        o += length
        txcount, o = read_varint(payload, o)
        if txcount != 0:
            raise ValueError(f"header {i} carries a transaction count of {txcount}, expected 0")
    if o != len(payload):
        raise ValueError(f"{len(payload) - o} trailing bytes after {count} headers")
    return out


def fetch(host, port, locator, timeout=30):
    deadline = time.monotonic() + timeout
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(msg("version", version_payload()))
        ua = None
        while ua is None:
            command, payload = recv_msg(sock, deadline)
            if command == "version":
                o = 4 + 8 + 8 + 26 + 26 + 8
                n, o = read_varint(payload, o)
                ua = payload[o:o + n].decode("ascii", "replace")
                sock.sendall(msg("verack", b""))
        # getheaders: version, locator count, locator hashes, stop hash
        payload = (struct.pack("<I", PROTOCOL_VERSION) + varint(1)
                   + bytes.fromhex(locator)[::-1] + b"\x00" * 32)
        sock.sendall(msg("getheaders", payload))
        while True:
            command, payload = recv_msg(sock, deadline)
            if command == "headers":
                return ua, split_headers(payload)
            if command == "ping":
                sock.sendall(msg("pong", payload))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("locator", help="block hash to fetch the headers following")
    ap.add_argument("--peers", default="fork-peers.txt")
    ap.add_argument("--out", default="fork-headers.json")
    ap.add_argument("--limit", type=int, default=0, help="keep only this many headers")
    args = ap.parse_args()

    peers = []
    for line in open(args.peers):
        line = line.strip()
        if line and not line.startswith("#"):
            host, _, port = line.rpartition(":")
            peers.append((host or line, int(port) if host else DEFAULT_PORT))

    for host, port in peers:
        try:
            ua, headers = fetch(host, port, args.locator)
        except Exception as e:
            print(f"  {host}:{port}  {type(e).__name__}: {e}", file=sys.stderr)
            continue
        if args.limit:
            headers = headers[:args.limit]
        v2 = sum(1 for h in headers if len(h) == 164)
        print(f"{host}:{port} ({ua}) returned {len(headers)} headers, {len(headers) - v2} v1 and {v2} v2")
        json.dump({"peer": f"{host}:{port}", "user_agent": ua, "locator": args.locator,
                   "headers": [h.hex() for h in headers]}, open(args.out, "w"), indent=1)
        print(f"wrote {args.out}")
        return 0
    print("no peer answered", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
