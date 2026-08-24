#!/usr/bin/env python3
"""Check the header-v2 claims in docs/electrum-header-v2.md against the live chain.

Everything in that document about the wire format came from reading Knots' C++.
This decodes a real v2 header from the BLAKE2b testnet4 chain and checks the
claims that can be checked without implementing BLAKE2b:

  1. field offsets, by asserting m_height equals the block's actual height and
     m_txcount equals its actual transaction count
  2. hashMerkleRoot is still at offset 36 and still SHA256d over the tx tree
  3. the block hash is NOT SHA256d over the 164 bytes, so identity really did change
  4. genesis is shared with ordinary testnet4, so genesis_hash cannot tell them apart

Read-only against a public explorer API.
"""
import hashlib
import json
import struct
import sys
import urllib.request

API = "https://mempool.guide/testnet4/api"
ACTIVATION = 149537
# Bitcoin testnet3/testnet4 genesis differ; this is testnet4's, per Knots chainparams.
TESTNET4_GENESIS = "00000000da84f2bafbbc53dee25a72ae507ff4914b867c565be350b0da8bf043"


def get(path, raw=False):
    with urllib.request.urlopen(f"{API}/{path}", timeout=30) as r:
        b = r.read()
    return b if raw else b.decode().strip()


def dsha(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def merkle_root(txids_be_hex):
    """Standard Bitcoin merkle root over txids, returned in display (big-endian) hex."""
    layer = [bytes.fromhex(t)[::-1] for t in txids_be_hex]
    if not layer:
        return None
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [dsha(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0][::-1].hex()


def parse_v2(raw):
    """Offsets exactly as claimed in the spec's wire-format table."""
    assert len(raw) == 164, f"expected 164 bytes, got {len(raw)}"
    f = {}
    f["version"] = struct.unpack_from("<I", raw, 0)[0]
    f["hashPrevBlock"] = raw[4:36][::-1].hex()
    f["hashMerkleRoot"] = raw[36:68][::-1].hex()
    f["time_on_wire"] = struct.unpack_from("<I", raw, 68)[0]
    f["nBits"] = struct.unpack_from("<I", raw, 72)[0]
    f["nNonce"] = struct.unpack_from("<I", raw, 76)[0]
    f["m_nonce2"] = struct.unpack_from("<I", raw, 80)[0]
    f["m_nonce3"] = struct.unpack_from("<I", raw, 84)[0]
    f["m_extranonce"] = raw[88:104].hex()
    f["m_time_offset"] = struct.unpack_from("<I", raw, 104)[0]
    f["m_txcount"] = struct.unpack_from("<H", raw, 108)[0]
    f["m_flags"] = raw[110]
    f["m_xor_key_mask_clear_bits"] = raw[111]
    f["m_xor_key"] = raw[112:128].hex()
    f["m_height"] = struct.unpack_from("<I", raw, 128)[0]
    f["m_mm_rhs"] = raw[132:164].hex()
    return f


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  {detail}" if detail else ""))
    return ok


def main():
    height = int(sys.argv[1]) if len(sys.argv) > 1 else 169000
    results = []

    print(f"BLAKE2b testnet4, block {height} (activation {ACTIVATION})\n")
    bh = get(f"block-height/{height}")
    blk = json.loads(get(f"block/{bh}"))
    hdr_hex = get(f"block/{bh}/header")
    raw = bytes.fromhex(hdr_hex)

    print(f"block hash   {bh}")
    print(f"header       {len(raw)} bytes\n")

    results.append(check("header is 164 bytes", len(raw) == 164, f"got {len(raw)}"))
    results.append(check("bit 31 of version is set (v2 marker)",
                         bool(struct.unpack_from('<I', raw, 0)[0] & 0x80000000),
                         f"version=0x{struct.unpack_from('<I', raw, 0)[0]:08x}"))

    f = parse_v2(raw)

    # 1. offsets, self-checking against values the explorer reports independently
    results.append(check("m_height at offset 128 equals the block height",
                         f["m_height"] == height, f"header says {f['m_height']}"))
    results.append(check("m_txcount at offset 108 equals the tx count",
                         f["m_txcount"] == blk["tx_count"],
                         f"header {f['m_txcount']} vs block {blk['tx_count']}"))
    results.append(check("hashPrevBlock at offset 4 matches the explorer",
                         f["hashPrevBlock"] == blk["previousblockhash"]))

    # 2. merkle root unchanged: still SHA256d over the tx tree, still at offset 36
    txids = json.loads(get(f"block/{bh}/txids"))
    computed = merkle_root(txids)
    results.append(check("hashMerkleRoot at offset 36 matches the explorer",
                         f["hashMerkleRoot"] == blk["merkle_root"]))
    results.append(check("merkle root is still SHA256d over the tx tree",
                         computed == blk["merkle_root"],
                         f"computed {computed[:16]}... from {len(txids)} txids"))

    # 3. identity changed: the block hash is not SHA256d of the header bytes
    sha_of_header = dsha(raw)[::-1].hex()
    results.append(check("block hash is NOT SHA256d over the 164 bytes",
                         sha_of_header != bh, f"sha256d gives {sha_of_header[:16]}..."))
    # and not SHA256d over the first 80 either, in case anyone assumes truncation works
    results.append(check("nor SHA256d over the first 80 bytes",
                         dsha(raw[:80])[::-1].hex() != bh))

    # 4. genesis is shared with ordinary testnet4
    g = get("block-height/0")
    results.append(check("genesis matches ordinary testnet4",
                         g == TESTNET4_GENESIS, g))

    # a v1 block for contrast
    v1 = get(f"block/{get(f'block-height/{ACTIVATION - 1}')}/header")
    results.append(check(f"block {ACTIVATION-1} is still v1 (80 bytes)",
                         len(bytes.fromhex(v1)) == 80, f"{len(bytes.fromhex(v1))} bytes"))

    print("\nother header fields, for eyeballing:")
    for k in ("m_flags", "m_xor_key_mask_clear_bits", "m_time_offset", "m_nonce2",
              "m_nonce3", "m_extranonce", "m_xor_key", "m_mm_rhs"):
        print(f"  {k:26} {f[k]}")

    bad = results.count(False)
    print(f"\n{len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
