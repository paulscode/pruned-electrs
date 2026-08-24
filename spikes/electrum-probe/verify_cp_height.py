#!/usr/bin/env python3
"""Check what the Electrum protocol's cp_height merkle proof is computed over.

The proposal claims the leaves are block hashes, which matters because on a
BLAKE2b chain "the block hash" stops being SHA256d over the header. That claim
came from the ElectrumX documentation, not from data.

This is self-contained: ask a live server for headers 0..cp_height plus the
checkpoint root, hash the headers ourselves, build the tree, and see whether the
root matches. If it does, the leaves are block hashes and nothing else.

  ./verify_cp_height.py <host> <port> [cp_height]
"""
import hashlib
import json
import socket
import ssl
import sys


def dsha(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


class Client:
    def __init__(self, host, port):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.s = ctx.wrap_socket(socket.create_connection((host, port), timeout=25),
                                 server_hostname=host)
        self.n = 0

    def call(self, method, params):
        self.n += 1
        self.s.sendall((json.dumps({"jsonrpc": "2.0", "id": self.n,
                                    "method": method, "params": params}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            c = self.s.recv(1 << 20)
            if not c:
                raise EOFError
            buf += c
        return json.loads(buf)


def merkle_root_and_branch(leaves, index):
    """Bitcoin-style tree, duplicating the last node on an odd layer.

    Returns (root, branch) with the branch deepest-pairing-first, matching the
    protocol's description.
    """
    branch = []
    layer = list(leaves)
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        sib = index ^ 1
        branch.append(layer[sib])
        layer = [dsha(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
        index //= 2
    return layer[0], branch


def main():
    host, port = sys.argv[1], int(sys.argv[2])
    cp = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    c = Client(host, port)
    print("server:", c.call("server.version", ["probe", "1.4"]).get("result"))

    r = c.call("blockchain.block.headers", [0, cp + 1, cp])
    res = r.get("result")
    if not res or "root" not in res:
        print("no checkpoint proof returned:", json.dumps(r)[:300])
        return 2

    if isinstance(res.get("headers"), list):
        hdrs = [bytes.fromhex(h) for h in res["headers"]]
    else:
        blob = bytes.fromhex(res["hex"])
        hdrs = [blob[i:i + 80] for i in range(0, len(blob), 80)]
    print(f"got {len(hdrs)} headers, count={res.get('count')}, "
          f"branch len={len(res.get('branch', []))}")

    leaves = [dsha(h) for h in hdrs]
    root, branch = merkle_root_and_branch(leaves, len(leaves) - 1)

    server_root = bytes.fromhex(res["root"])[::-1]
    ok_root = root == server_root
    print(f"  computed root {root[::-1].hex()}")
    print(f"  server root   {res['root']}")
    print("  ROOT MATCHES: leaves are block hashes" if ok_root else "  ROOT DIFFERS")

    server_branch = [bytes.fromhex(x)[::-1] for x in res.get("branch", [])]
    ok_branch = server_branch == branch
    print("  BRANCH MATCHES (deepest pairing first)" if ok_branch
          else f"  branch differs\n    ours   {[b[::-1].hex()[:16] for b in branch]}"
               f"\n    theirs {[b[::-1].hex()[:16] for b in server_branch]}")

    return 0 if (ok_root and ok_branch) else 1


if __name__ == "__main__":
    sys.exit(main())
