# Electrum protocol: variable-length block headers

Draft for discussion. Version 0.1, 2026-08-24.

## Problem

The Electrum protocol assumes a block header is 80 bytes and that its hash is SHA256d over
those bytes. Both assumptions are baked into clients rather than into the wire format.

Bitcoin Knots PR #359 introduces a second header format. A v2 header is 164 bytes and its
hash is a staged BLAKE2b computation. The change is live on testnet4 today, activated at
height 149537 in `v29.4.1.knots20260508rc2`.

A client speaking the protocol as it stands cannot follow that chain. It will slice the
concatenated response from `blockchain.block.headers` on an 80-byte stride and get
garbage, and it will verify proof of work with the wrong hash function. Electrum, Sparrow
and anything else built on this protocol are affected the same way.

This document proposes the smallest change that lets a server serve such a chain and lets
a client know it needs to parse differently.

## What is not affected

Worth stating first, because it bounds the work.

`hashMerkleRoot` is unchanged. It is still SHA256d over the transaction tree, at the same
offset 36. So `blockchain.transaction.get_merkle`, `blockchain.transaction.id_from_pos` and
every merkle proof keep working with no change on either side.

Scripthash methods, transaction broadcast, fee estimation and mempool methods are all
unaffected. They do not touch header bytes.

The only methods that need anything are the three that carry headers:
`blockchain.block.header`, `blockchain.block.headers`, and `blockchain.headers.subscribe`.

## The format is self-describing

Bit 31 of the version field is the v2 marker (`VERSION_HEADER_V2_FLAG = 0x80000000`, set in
`GetCompleteVersion()`). The version field is the first four bytes on the wire, little
endian.

So the length of a header is a function of its first four bytes:

```
version = u32le(buf[0..4])
length  = 164 if (version & 0x80000000) else 80
```

This matters more than it might look. It means no activation height has to be agreed
between server and client, no chain parameter has to be distributed, and a stream mixing
v1 and v2 headers is unambiguously parseable by walking it. A client that implements the
walk works on any chain, before and after any activation, without being told when the
activation was.

## Proposed changes

### 1. Protocol version 1.5

Bump the negotiated version. A client advertising 1.5 states that it parses headers by
length prefix rather than by fixed stride.

Negotiation is unchanged: `server.version(client_name, protocol_version)` returns the
agreed version, using the existing min/max rules.

### 2. Servers on a chain with v2 headers must not negotiate below 1.5

This is the safety-critical part. If a 1.4 client connects to a server on such a chain and
the server accepts the negotiation, the client silently mis-parses every header past
activation. It does not fail loudly. It computes wrong hashes, fails to link the chain, and
depending on the client may present that as a sync problem rather than an incompatibility.

A server that knows its chain uses v2 headers should refuse to negotiate below 1.5, closing
the connection as the existing rules already require when no common version exists. Better
a clear connection failure than a wallet that quietly believes something false.

Servers on chains with no v2 headers keep negotiating 1.4 as they do now. Nothing changes
for them.

### 3. `blockchain.block.headers` is parsed by walking, not by stride

The result format does not change:

```
{ "count": 2, "hex": "<concatenated headers>", "max": 2016 }
```

`hex` remains the concatenation of the raw headers in order. What changes is how a client
splits it: read four bytes, derive the length, take that many bytes, repeat. `count` still
says how many headers are present, so a client can check its walk consumed exactly that
many and did not run off the end.

Servers may want to lower `max` on a v2 chain. At 2016 headers the response goes from about
323 kB of hex to about 661 kB.

### 4. Checkpoint proofs use the chain's own block hash

When `cp_height` is nonzero, `blockchain.block.header` and `blockchain.block.headers`
return a merkle `branch` and `root` over the header chain. The leaves are block hashes.

For a v2 header, the block hash is what the chain says it is, meaning the staged BLAKE2b
value from `CBlockHeader::GetHash()`, not SHA256d over the 164 bytes. On a chain that
transitions, leaves below the activation height are SHA256d hashes and leaves at or above
it are BLAKE2b hashes. The tree itself is unchanged, and its internal pairing still uses
SHA256d.

This needs stating explicitly because "the header hash" is ambiguous once there are two
hash functions in one chain.

### 5. Chain identity needs a field, and `genesis_hash` will not do

`server.features` reports `genesis_hash`, which clients use to confirm they are talking to
a server on the chain they expect.

That does not work here. The BLAKE2b chain on testnet4 shares its genesis block with
ordinary testnet4. Two servers can report identical `genesis_hash` and serve chains that
diverge at height 149537. A wallet has no way to tell them apart from `server.features`
alone.

The same will be true of any future proof-of-work fork that keeps its history.

Some field is needed. Two options, and I do not have a strong preference:

- A fork point: the height at which the header format changed, plus the block hash at that
  height. Precise, and directly useful to a client deciding how to verify.
- An opaque chain identifier agreed per chain. Simpler, but needs a registry and someone to
  run it.

I lean towards the fork point because it needs no coordination and a client can verify it
against what the server serves.

## Client adoption can be staged

Two levels of support, and the first is much cheaper than the second.

**Parse only.** Split headers by length, read `hashMerkleRoot` at offset 36, verify merkle
proofs as before. This is enough for a wallet that trusts its own server, which is the
common case for self-hosted setups. It needs no new hash function and no consensus code.

**Full verification.** Additionally compute the v2 block hash to check proof of work and
chain linkage. This needs the staged BLAKE2b implementation, which is more work and needs
test vectors to get right.

A client that does the first and clearly says it is not verifying proof of work on this
chain is more useful than a client that does neither, and it is a small change.

## Reference material

Knots ships test vectors at `src/test/data/block_header_v2.json`: five headers covering all
four ASIC profiles. Each vector carries every intermediate stage of the hash
(`xor_key_hash`, `h1`, `h2`, `blake2b_1`, `blake2b_2`, `mask`, `asic_input`, `serialized`,
`block_hash`), so an implementation can be checked stage by stage rather than pass or fail
at the end. That turns the hardest part of the work into a lookup.

Header layout is in `src/primitives/block.h`, and `GetHash()` is in
`src/primitives/block.cpp`.

## Wire format, for reference

```
 offset  size  field                        v1   v2
      0     4  version (bit 31 = v2 flag)    y    y
      4    32  hashPrevBlock                 y    y
     36    32  hashMerkleRoot                y    y
     68     4  time_on_wire                  y    y
     72     4  nBits                         y    y
     76     4  nNonce                        y    y
   --------------------------------------------- 80 bytes, v1 ends
     80     4  m_nonce2                           y
     84     4  m_nonce3                           y
     88    16  m_extranonce                       y
    104     4  m_time_offset                      y
    108     2  m_txcount                          y
    110     1  m_flags                            y
    111     1  m_xor_key_mask_clear_bits          y
    112    16  m_xor_key                          y
    128     4  m_height                           y
    132    32  m_mm_rhs                           y
   --------------------------------------------- 164 bytes, v2
```

`nTime` is derived rather than read directly: `time_on_wire + m_time_offset` when
`m_flags & UseTimeOffset` is set.

## Open questions

1. Is 1.5 the right version number, or should this be negotiated some other way? A protocol
   bump is heavier than a capability flag, but it is the mechanism that already exists and
   the one clients already handle.
2. For chain identity, fork point or opaque identifier?
3. Should `max` be reduced on a v2 chain, or left to server operators?
4. Does anything else in the protocol assume 80 bytes? I have looked at the header-carrying
   methods and believe not, but a second pair of eyes on that is worth having.

## Status

Nothing here is implemented yet. I am working on the indexer side in electrs and will
report what the parsing work actually costs once it is done. If someone is already doing
this in Fulcrum or ElectrumX, I would rather join that than duplicate it.
