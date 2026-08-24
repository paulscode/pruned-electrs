#!/usr/bin/env python3
"""Query the Electrum interface and check answers against the archival node.

Exercises the methods DISCOVERY.md classifies as needing an old raw block, at
heights below the pruned node's pruneheight — i.e. exactly the cases that fail
on stock electrs.
"""
import hashlib
import json
import socket
import subprocess
import sys

ELECTRUM = ("127.0.0.1", 19014)
ACLI = [
    f"{__import__('os').path.expanduser('~')}/bin/knots/bin/bitcoin-cli",
    f"-datadir={__import__('os').path.dirname(__import__('os').path.abspath(__file__))}/nodeA",
    f"-conf={__import__('os').path.dirname(__import__('os').path.abspath(__file__))}/nodeA/bitcoin.conf",
]


def acli(*args):
    out = subprocess.run(ACLI + list(args), capture_output=True, text=True, check=True).stdout.strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


class Electrum:
    def __init__(self, addr):
        self.sock = socket.create_connection(addr, timeout=60)
        self.n = 0

    def call(self, method, params):
        self.n += 1
        req = json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
        self.sock.sendall(req.encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError
            buf += chunk
        return json.loads(buf)


def scripthash(spk_hex):
    return hashlib.sha256(bytes.fromhex(spk_hex)).digest()[::-1].hex()


def main():
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        print(("PASS  " if ok else "FAIL  ") + name + ("  " + detail if detail else ""), flush=True)

    e = Electrum(ELECTRUM)
    prune_h = 251

    # a block well below the pruned node's pruneheight
    h = 100
    blockhash = acli("getblockhash", str(h))
    block = acli("getblock", blockhash, "2")
    txid = block["tx"][0]["txid"]
    raw_tx = acli("getrawtransaction", txid, "0", blockhash)
    spk = block["tx"][0]["vout"][0]["scriptPubKey"]["hex"]

    print(f"testing against pruned height {h} (node B pruneheight={prune_h})\n")

    r = e.call("blockchain.block.header", [h])
    check("blockchain.block.header @%d" % h,
          r.get("result") == acli("getblockheader", blockhash, "false"))

    r = e.call("blockchain.transaction.get", [txid])
    check("blockchain.transaction.get (pruned block)",
          r.get("result") == raw_tx,
          "" if r.get("result") == raw_tx else f"got {str(r)[:120]}")

    r = e.call("blockchain.transaction.get_merkle", [txid, h])
    ok = r.get("result", {}).get("block_height") == h and r.get("result", {}).get("pos") == 0
    check("blockchain.transaction.get_merkle (pruned block)", ok, str(r.get("result") or r)[:120])

    r = e.call("blockchain.transaction.id_from_pos", [h, 0, False])
    check("blockchain.transaction.id_from_pos (pruned block)",
          r.get("result", {}).get("tx_hash") == txid, str(r.get("result") or r)[:120])

    sh = scripthash(spk)
    r = e.call("blockchain.scripthash.get_history", [sh])
    hist = r.get("result") or []
    heights = {entry["height"] for entry in hist}
    check("blockchain.scripthash.get_history includes pruned height %d" % h,
          h in heights, "%d entries, min height %s" % (len(hist), min(heights) if heights else "-"))

    r = e.call("blockchain.scripthash.get_balance", [sh])
    check("blockchain.scripthash.get_balance", r.get("result") is not None, str(r.get("result"))[:120])

    r = e.call("blockchain.transaction.get", [txid, True])
    res = r.get("result")
    check("blockchain.transaction.get verbose=True (pruned block)",
          isinstance(res, dict) and res.get("txid") == txid,
          "expected-to-fail path: " + str(r.get("error") or res)[:120])

    print()
    failed = [n for n, ok, _ in results if not ok]
    print("%d/%d passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
