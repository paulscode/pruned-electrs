# Electrum protocol: variable-length block headers

Draft for discussion. Version 0.2, 2026-08-24.

Changes since 0.1: corrected against the current protocol and against what is actually
deployed. Version 1.5 was skipped, 1.6 replaced the concatenated header blob with a list,
and 1.7 is documented, so the proposal targets 1.8. But a survey of public servers shows
Fulcrum is the only implementation reaching 1.6, so the concatenated form is still what
almost everything exchanges. All wire-format claims are now verified against live chain
data rather than read from source.

## Problem

The Electrum protocol assumes a block header is 80 bytes and that its hash is SHA256d over
those bytes. Both assumptions are baked into clients rather than into the wire format.

Protocol versions referenced here: 1.5 was skipped, 1.6 changed
`blockchain.block.headers()` to return a list rather than a concatenated hex string, and
1.7 is the current documented version. So this proposal targets **1.8**.

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

## A related gap, one layer down

An Electrum server gets its data from the node over RPC, and there the format is not
self-describing.

`blockheaderToJSON()` clears bit 31 before reporting, so verbose `getblockheader` returns
identical `version` and `versionHex` for a v1 and a v2 header, and none of the ten v2
fields appear. A server that consumes the JSON cannot tell which format it is about to
receive; the first thing it sees is a raw header of unexpected length. This was
demonstrated on regtest by Kilombino on 2026-08-19 in the PR #359 thread.

Knots PR #363 (AcesHigh70, open as of 2026-08-24) adds the v2 fields to
`blockheaderToJSON` and emits `header_version` for every header, zero for v1. That closes
the RPC-side gap.

The two are independent. A server can already do everything in this document by parsing
raw header hex, which is self-describing, and does not need to wait for #363. But
implementers should know the JSON path cannot currently detect the change, because
discovering it by hitting a 164-byte header is an unpleasant way to find out.

## Proposed changes

### 1. Protocol version 1.8

Bump the negotiated version. A client advertising 1.8 states that it does not assume a
header is 80 bytes and knows which hash function identifies a block on this chain.

Negotiation is unchanged, and 1.6 already mandates that `server.version()` is the first
message sent, which makes the check cheap: a server knows the client's version before it
has to serve anything.

### 2. Servers on a chain with v2 headers must not negotiate below 1.8

This is the safety-critical part. A client below 1.8 does not fail loudly on this chain. It
computes the wrong hash for every header past activation, fails to link the chain, and
depending on the client may present that as a sync problem rather than an incompatibility.

A server that knows its chain uses v2 headers should refuse to negotiate below 1.8, closing
the connection as the existing rules already require when no common version exists. Better
a clear connection failure than a wallet that quietly believes something false.

Servers on chains with no v2 headers keep negotiating as they do now. Nothing changes for
them.

### 3. Header length must be read, not assumed

Protocol 1.6 changed `blockchain.block.headers()` from a concatenated hex string to a list,
which would remove most of this problem on its own. Each header is then its own string, so
its length is self-evident and nothing has to be sliced on a stride.

Deployment tells a different story. Probing public servers on 2026-08-24:

| server | software | `protocol_max` | `block.headers` at the negotiated version |
|---|---|---|---|
| fulcrum.sethforprivacy.com | Fulcrum 2.1.2 | 1.6 | list |
| fulcrum.kilombino.com | Fulcrum 2.1.0 | 1.6 | list |
| electrum.emzy.de | ElectrumX 1.18.0 | 1.4.3 | concatenated hex |
| bitcoin.lu.ke | ElectrumX 1.18.0 | 1.4.3 | concatenated hex |
| electrum.blockstream.info | electrs-esplora 0.4.1 | 1.4 | concatenated hex |

Fulcrum is the only implementation of the four that reaches 1.6. ElectrumX caps at 1.4.3,
and both electrs lines cap at 1.4.

The client side, read from source rather than from connection dialogs:

| client | versions requested | source |
|---|---|---|
| Electrum | min `1.4`, max `1.6` | `electrum/version.py` |
| Sparrow | `{"1.3", "1.4.2"}` | `ElectrumServer.java`, `SUPPORTED_VERSIONS` |

So Electrum can reach 1.6 and will get the list form from a Fulcrum server. Sparrow caps at
1.4.2 and therefore always receives the concatenated form, whatever the server supports.

The concatenated form is what most pairings actually exchange, and for one of the two major
wallets it is the only form available. A client that wants to follow a v2 chain cannot wait
for the ecosystem to reach 1.6.

That makes the self-describing marker do real work rather than being a curiosity. A client
walks the blob: read four bytes, take 80 or 164 depending on bit 31, repeat, and check the
walk consumed exactly `count` headers and did not run off the end. The same routine handles
the 1.6 list form, where each element is simply already separated.

1.8 should state that a header is 80 or 164 bytes, that the length is determined by bit 31
of the version field, and that this holds in both the concatenated and list forms.

Servers may want to lower `max` on a v2 chain. At 2016 headers the response roughly doubles.

### 4. Checkpoint proofs use the chain's own block hash

When `cp_height` is nonzero, `blockchain.block.header` and `blockchain.block.headers`
return a merkle `branch` and `root` over the header chain.

The leaves are block hashes. Verified rather than assumed: asking ElectrumX 1.18.0 and
Fulcrum 2.1.2 for headers 0 to 5 with `cp_height=5`, hashing those headers, and building a
Bitcoin-style tree with last-node duplication reproduces both servers' `root` exactly, and
the `branch` matches deepest-pairing-first. Two independent implementations agreeing rules
out reading one server's quirk as the specification.

For a v2 header, the block hash is what the chain says it is, meaning the staged BLAKE2b
value from `CBlockHeader::GetHash()`, not SHA256d over the 164 bytes. On a chain that
transitions, leaves below the activation height are SHA256d hashes and leaves at or above
it are BLAKE2b hashes. The tree itself is unchanged, and its internal pairing still uses
SHA256d.

This needs stating explicitly because "the header hash" is ambiguous once there are two
hash functions in one chain.

The practical consequence for a client is concrete: verifying a checkpoint proof that spans
the activation height means computing SHA256d leaves below it and BLAKE2b leaves at or
above it, in one tree. The pairing function above the leaves does not change.

### 5. Chain identity needs a field, and `genesis_hash` will not do

Note the timing here. Protocol 1.7 **removed** `hash_function` from `server.features()`,
which was the one field that spoke to how a chain hashes. It reported `"sha256"` and was
presumably dropped as a constant nobody varied. A chain that varies it arrived shortly
afterwards.

I am not proposing to restore it. `hash_function` described the scripthash function rather
than the header hash, so restoring it would answer a different question. But it is worth
noting that the field which looks like it should carry this does not exist any more, and
that whatever replaces it should be about the header.


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
chain linkage, and to build checkpoint leaves past activation. This needs the staged
BLAKE2b implementation.

It is less work than it looks. A from-scratch implementation written against the published
vectors, in Python, is about 120 lines including the four ASIC profile layouts, and it
reproduces live block hashes. See below.

A client that does the first and clearly says it is not verifying proof of work on this
chain is more useful than a client that does neither, and it is a small change.

## Verified against the live chain

The claims above are not read from source alone. Decoded on 2026-08-24 from a real v2
header on BLAKE2b testnet4, block 169000, using the offsets in the table below. Eleven
checks, all passing:

```
header is 164 bytes                                    yes
bit 31 of version set                                  version = 0xa0000000
m_height at offset 128 equals the block height         169000
m_txcount at offset 108 equals the tx count            1
hashPrevBlock at offset 4 matches the explorer         yes
hashMerkleRoot at offset 36 matches the explorer       yes
merkle root is still SHA256d over the tx tree          recomputed from txids, matches
block hash is NOT SHA256d over the 164 bytes           sha256d gives 533a54b9fb34669d...
nor SHA256d over the first 80 bytes                    no match either
genesis matches ordinary testnet4                      00000000da84f2ba...bf043
block 149536 is still v1                               80 bytes
```

`m_height` and `m_txcount` are the useful ones: both are values the explorer reports
independently, so agreeing with them at the claimed offsets is a real check on the layout
rather than a restatement of it.

The last two matter for specific points above. Genesis being shared with ordinary testnet4
is why `genesis_hash` cannot identify this chain. And the block hash matching neither
SHA256d over 164 bytes nor over the first 80 is the concrete demonstration that block
identity, not just header length, has changed.

The script is `spikes/electrum-probe/verify_header_v2.py` in this repo. It is read-only
against a public explorer API and takes a height as its argument.

### The hash itself

`GetHash()` was also implemented from the C++ and checked, since a proposal that says "the
hash is different" should be able to demonstrate it.

Against the published vectors in `src/test/data/block_header_v2.json`: **40 of 40 stage
comparisons match**, covering all four ASIC profiles, both time-offset settings, and both
null and non-null XOR keys. The vectors carry every intermediate stage, which is what made
this quick: an initial byte-order error on `m_xor_key` showed up as a mismatch on
`xor_key_hash`, the very first stage, rather than as a wrong final hash with no clue where
it went wrong.

Against the live chain, the same implementation reproduces the block hash exactly:

```
height 149537 (activation)  000000000068f60429c933dc0c8befbcc7edadb1cf8f8d0d7804c608fd736d82
height 160500               0000000000143a4b1c5889b8ee9fe766dd5a1cc4c1fb142a60fffe35e79bc294
height 169000               000000000000324e94407a3c8335ead2213d881a105b1cc6c0797e0713b26d86
```

So the path from raw header bytes, through the field layout in the table above, through the
staged computation, to the identifier the chain actually uses, is verified end to end.
`spikes/electrum-probe/headerv2_hash.py`, about 120 lines, no dependencies beyond hashlib.

All three blocks sampled use ASIC profile 0 with a null XOR key, which is what solo mining
produces. The other three profiles are exercised by the vectors but have not been seen in
the wild here.

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

1. Is a protocol bump the right mechanism, or should this be a capability flag? A bump is
   heavier, but it is what already exists and what clients already handle, and 1.6 made
   `server.version()` the first message so the answer is known before anything is served.
2. For chain identity, fork point or opaque identifier?
3. Should `max` be reduced on a v2 chain, or left to server operators?
4. Does anything else in the protocol assume 80 bytes? I have looked at the header-carrying
   methods and believe not, but a second pair of eyes on that is worth having.

## Status

Nothing here is implemented yet. I am working on the indexer side in electrs and will
report what the parsing work actually costs once it is done. If someone is already doing
this in Fulcrum or ElectrumX, I would rather join that than duplicate it.

There is precedent for specifying a layer that sits outside the node. luke-jr has said a
BIP is to be written for the `getblocktemplate` BLAKE2b extensions, which is the same shape
of problem in the mining direction: parameters the node does not determine, carried over a
protocol the node does not own. This document is the equivalent for the wallet direction.
