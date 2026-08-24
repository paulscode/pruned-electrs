#!/usr/bin/env python3
"""Independent implementation of CBlockHeader::GetHash() for BLAKE2b header v2.

Written from src/primitives/block.cpp in Knots rather than from the lab's
reference, so that agreeing with the published vectors is a real check on the
reading rather than a restatement of it.

  ./headerv2_hash.py vectors <path/to/block_header_v2.json>
  ./headerv2_hash.py live <164-byte-header-hex> <expected-block-hash>

Stages are exposed individually because the published vectors carry every one,
which turns a wrong byte order into a one-line diff instead of a hunt.
"""
import hashlib
import json
import struct
import sys

ZERO16 = bytes(16)


def sha256(b):
    return hashlib.sha256(b).digest()


def tagged(tag, payload):
    """BIP340 tagged hash: SHA256(SHA256(tag) || SHA256(tag) || payload).

    The C++ asserts BytesWritten() == 0x40 + n, the 0x40 being the doubled tag
    digest, which is what pins this interpretation.
    """
    t = sha256(tag.encode())
    return sha256(t + t + payload)


def blake2b256(b):
    return hashlib.blake2b(b, digest_size=32).digest()


def u32(v):
    return struct.pack("<I", v)


def get_hash(f, stages=None):
    """f: dict of header fields in the same shape as the test vectors.

    Hashes named *_be are display order in the JSON; the wire and the uint256
    internals hold them reversed.
    """
    st = {} if stages is None else stages

    # uint128 like m_extranonce and m_mm_rhs: the JSON holds display order, the
    # serializer writes internal order, so reverse.
    xor_key = bytes.fromhex(f["m_xor_key"])[::-1]
    xor_key_hash = tagged("Bitcoin block hash PoW XOR key", xor_key)
    st["xor_key_hash"] = xor_key_hash.hex()

    mask = bytes(32)
    if any(xor_key):
        m = bytearray(tagged("Bitcoin block hash PoW XOR mask", xor_key))
        clear_bytes = f["m_xor_key_mask_clear_bits"] // 8
        for i in range(clear_bytes):
            m[i] = 0
        m[clear_bytes] &= 0xFF >> (f["m_xor_key_mask_clear_bits"] % 8)
        mask = bytes(m)
    st["mask"] = mask.hex()

    # hashPrevBlock is stored reversed on the wire; ReversedBytes() puts it back
    # into display order, which is exactly what the JSON field already holds.
    prevblock_sane = bytes.fromhex(f["hashPrevBlock"])
    prevblock_hidden = bytearray(
        tagged("Bitcoin prevblock header, hashed", prevblock_sane))

    complete_version = (f["nVersion"] & ~0x80000000) | 0x80000000
    # UseTimeOffset is bit 2 of m_flags
    time_on_wire = f["nTime"] - (f["m_time_offset"] if f["m_flags"] & 0x04 else 0)
    time_on_wire &= 0xFFFFFFFF

    h1_payload = (
        u32(complete_version)
        + prevblock_sane
        + u32(f["m_height"])
        + bytes.fromhex(f["hashMerkleRoot"])[::-1]   # uint256 serializes internal order
        + u32(time_on_wire)
        + b"\x00"                                    # reserved for 40-bit time
        + u32(f["nBits"])
        + u32(f["m_txcount"])
        + bytes([f["m_flags"], f["m_xor_key_mask_clear_bits"]])
        + xor_key_hash
    )
    assert len(h1_payload) == 119, f"h1 payload {len(h1_payload)}, expected 119"
    h1 = tagged("Bitcoin block header 1", h1_payload)
    st["h1"] = h1.hex()

    h2_payload = h1 + ZERO16 + ZERO16 + bytes.fromhex(f["m_mm_rhs"])[::-1]
    assert len(h2_payload) == 0x60, f"h2 payload {len(h2_payload)}, expected 96"
    h2 = tagged("Merge-mining hook", h2_payload)
    st["h2"] = h2.hex()

    ss = u32(0) + h2 + bytes.fromhex(f["m_extranonce"])[::-1]
    assert len(ss) == 52, f"first blake2b input {len(ss)}, expected 52"
    b1 = blake2b256(ss)
    st["blake2b_1"] = b1.hex()

    profile = f["m_flags"] & 3
    nonces = u32(f["nNonce"]) + u32(f["m_nonce2"])
    if profile == 0:
        prevblock_hidden[:6] = bytes(6)
        asic = bytes(prevblock_hidden) + nonces + u32(f["m_time_offset"]) + u32(f["m_nonce3"]) + b1
    elif profile == 1:
        asic = nonces + u32(f["m_nonce3"]) + u32(f["m_time_offset"]) + b1 + h2
    elif profile == 2:
        asic = ZERO16 * 3 + h2 + nonces + u32(f["m_time_offset"]) + u32(f["m_nonce3"]) + b1
    else:
        asic = ZERO16 * 2 + ZERO16 * 3 + h2 + nonces + u32(f["m_time_offset"]) + u32(f["m_nonce3"]) + b1
    st["asic_input"] = asic.hex()
    st["asic_profile"] = profile

    b2 = blake2b256(asic)
    st["blake2b_2"] = b2.hex()

    # final_hash is written backwards from end(), so internal = reverse(b2 ^ mask);
    # uint256's display hex reverses again, leaving b2 ^ mask in forward order.
    st["block_hash"] = bytes(a ^ b for a, b in zip(b2, mask)).hex()
    return st


def parse_live(raw):
    """Decode a 164-byte v2 header into the vector field shape."""
    assert len(raw) == 164
    flags = raw[110]
    time_on_wire = struct.unpack_from("<I", raw, 68)[0]
    toff = struct.unpack_from("<I", raw, 104)[0]
    return {
        "nVersion": struct.unpack_from("<I", raw, 0)[0] & ~0x80000000,
        "hashPrevBlock": raw[4:36][::-1].hex(),
        "hashMerkleRoot": raw[36:68][::-1].hex(),
        "nTime": (time_on_wire + (toff if flags & 0x04 else 0)) & 0xFFFFFFFF,
        "nBits": struct.unpack_from("<I", raw, 72)[0],
        "nNonce": struct.unpack_from("<I", raw, 76)[0],
        "m_nonce2": struct.unpack_from("<I", raw, 80)[0],
        "m_nonce3": struct.unpack_from("<I", raw, 84)[0],
        "m_extranonce": raw[88:104][::-1].hex(),
        "m_time_offset": toff,
        "m_txcount": struct.unpack_from("<H", raw, 108)[0],
        "m_flags": flags,
        "m_xor_key_mask_clear_bits": raw[111],
        "m_xor_key": raw[112:128][::-1].hex(),
        "m_height": struct.unpack_from("<I", raw, 128)[0],
        "m_mm_rhs": raw[132:164][::-1].hex(),
    }


def main():
    mode = sys.argv[1]
    if mode == "vectors":
        data = json.load(open(sys.argv[2]))["headers"]
        total = bad = 0
        for v in data:
            got = get_hash(v["fields"])
            print(f"--- {v['name']}  (profile {v.get('asic_profile')})")
            for k in ("xor_key_hash", "h1", "h2", "blake2b_1", "asic_input",
                      "blake2b_2", "mask", "block_hash"):
                if k not in v:
                    continue
                total += 1
                ok = str(got.get(k)).lower() == str(v[k]).lower()
                bad += not ok
                print(f"   {'ok  ' if ok else 'FAIL'} {k}")
                if not ok:
                    print(f"        want {v[k]}")
                    print(f"        got  {got.get(k)}")
        print(f"\n{total - bad}/{total} stage comparisons matched")
        return 1 if bad else 0

    if mode == "live":
        raw = bytes.fromhex(sys.argv[2])
        expected = sys.argv[3].lower()
        f = parse_live(raw)
        got = get_hash(f)
        print(f"profile      {got['asic_profile']}")
        print(f"h1           {got['h1']}")
        print(f"h2           {got['h2']}")
        print(f"blake2b_1    {got['blake2b_1']}")
        print(f"blake2b_2    {got['blake2b_2']}")
        print(f"computed     {got['block_hash']}")
        print(f"expected     {expected}")
        ok = got["block_hash"] == expected
        print("MATCH" if ok else "MISMATCH")
        return 0 if ok else 1

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
