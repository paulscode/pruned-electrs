#!/usr/bin/env python3
"""Minimal Bitcoin P2P client used to answer one discovery question:

what does bitcoind do when a peer asks, via `getdata`, for a block it has pruned?

electrs' only block source is p2p (`src/p2p.rs::for_blocks`), and that function
sends one `getdata` for a whole batch and then blocks on `blocks_recv.recv()`
once per requested hash. Whether a pruned block yields an error, a `notfound`,
a disconnect, or simply silence decides whether a pruning-aware electrs can
detect the miss and fall back, or whether it just wedges.

Usage: p2p_probe.py <port> <blockhash> [<blockhash> ...]
"""
import hashlib
import socket
import struct
import sys
import time

MAGIC = bytes.fromhex("fabfb5da")  # regtest
PROTOCOL_VERSION = 70016
MSG_WITNESS_BLOCK = 0x40000002


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


def netaddr():
    return struct.pack("<Q", 0) + b"\x00" * 10 + b"\xff\xff" + b"\x00" * 4 + struct.pack(">H", 0)


def version_payload():
    ua = b"/probe:0.1/"
    return (
        struct.pack("<i", PROTOCOL_VERSION)
        + struct.pack("<Q", 0)               # services = NONE, as electrs sends
        + struct.pack("<q", int(time.time()))
        + netaddr()
        + netaddr()
        + struct.pack("<Q", 0x1234)
        + varint(len(ua)) + ua
        + struct.pack("<i", 0)
        + b"\x00"                            # relay = false
    )


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("peer closed connection")
        buf += chunk
    return buf


def recv_msg(sock):
    hdr = recv_exact(sock, 24)
    if hdr[:4] != MAGIC:
        raise ValueError("bad magic %s" % hdr[:4].hex())
    command = hdr[4:16].rstrip(b"\x00").decode()
    length = struct.unpack("<I", hdr[16:20])[0]
    payload = recv_exact(sock, length) if length else b""
    return command, payload


def main():
    port = int(sys.argv[1])
    hashes = sys.argv[2:]

    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.sendall(msg("version", version_payload()))

    # handshake
    while True:
        command, payload = recv_msg(sock)
        if command == "version":
            sock.sendall(msg("verack", b""))
        elif command == "verack":
            break
    print("[%d] handshake complete" % port, flush=True)

    # one getdata carrying every hash, exactly as electrs' for_blocks does
    inv = varint(len(hashes))
    for h in hashes:
        inv += struct.pack("<I", MSG_WITNESS_BLOCK) + bytes.fromhex(h)[::-1]
    sock.sendall(msg("getdata", inv))
    print("[%d] sent getdata for %d block(s)" % (port, len(hashes)), flush=True)

    sock.settimeout(15)
    start = time.time()
    received = 0
    try:
        while received < len(hashes):
            command, payload = recv_msg(sock)
            elapsed = time.time() - start
            if command == "block":
                blockhash = dsha(payload[:80])[::-1].hex()
                received += 1
                print("[%d] +%5.2fs block %s (%d bytes)" % (port, elapsed, blockhash, len(payload)), flush=True)
            elif command == "notfound":
                print("[%d] +%5.2fs NOTFOUND %s" % (port, elapsed, payload.hex()), flush=True)
                break
            elif command == "ping":
                sock.sendall(msg("pong", payload))
            else:
                print("[%d] +%5.2fs (%s)" % (port, elapsed, command), flush=True)
    except socket.timeout:
        print("[%d] +%5.2fs TIMED OUT after %d/%d blocks — no reply, no notfound"
              % (port, time.time() - start, received, len(hashes)), flush=True)
    except EOFError as e:
        print("[%d] +%5.2fs DISCONNECTED by peer after %d/%d blocks (%s)"
              % (port, time.time() - start, received, len(hashes), e), flush=True)
    sock.close()


if __name__ == "__main__":
    main()
