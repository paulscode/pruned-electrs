# Pruning and BLAKE2b header v2, together

B4 from `internal_docs/TESTNET-PLAN.md`: confirm that the pruned-block routing
(patches 0001 and 0002) and the header-v2 support (patches 0003 and 0004)
compose, rather than each working only when the other is absent.

**They do.** electrs indexed all 800 blocks of a pruned BLAKE2b regtest chain and
answered correctly at every combination of the two frontiers.

## The setup

Two Bitcoin Knots `v29.4.1.knots20260508rc2` regtest nodes, BLAKE2b activating at
height 20, so heights 1..19 are 80-byte v1 headers and 20..800 are 164-byte v2
headers with BLAKE2b hashes.

| | |
|---|---|
| node A | archival, mines the chain |
| node B | pruned (`prune=1 fastprune=1`), pruned to height 384, `-whitelist=noban,download` |
| `fallback_proxy.py` | stands in for btc-rpc-proxy: forwards RPC to B, retries `getblock` at verbosity 0 or 1 against A when B refuses |
| electrs | RPC to the proxy, p2p to B |

That puts two independent frontiers on one chain, which is the point:

```
height     1 .............. 19 | 20 ................ 383 | 384 .......... 800
header        v1 / SHA256d     |      v2 / BLAKE2b       |    v2 / BLAKE2b
source        pruned, RPC      |      pruned, RPC        |    local, p2p
```

## Result

electrs reached the node's tip and, checked over the Electrum protocol:

- `blockchain.block.header` returns 80 bytes below height 20 and 164 above, on
  both sides of the prune frontier
- `transaction.id_from_pos` and `transaction.get_merkle` agree at heights 5, 25,
  150, 383, 384, 700 and 800, with every merkle proof recomputing to that
  block's own merkle root: 7/7
- `scripthash.get_history` for the coinbase address returns 400 entries, of which
  19 are v1, **364 are v2 blocks below the prune height** (so they were fetched
  over RPC, not p2p), and 17 are v2 blocks held locally
- `transaction.get` works for a transaction in a pruned v2 block

## What this establishes, and what it does not

**The routing needed no change for v2.** `Daemon::for_blocks_with` splits the
batch on `getblockchaininfo.pruneheight` and never looks at header format, and
`getblock <hash> 0` returns the whole block including its 164-byte header. Two
things could have broken and did not: `block_heights()` deserializes
`getblockheader`'s verbose JSON into `bitcoincore-rpc`'s typed
`GetBlockHeaderResult`, and Knots' v2 form of that JSON turns out to be a strict
superset of the v1 form (it adds `header_version`, `nonce2`, `nonce3`,
`extranonce`, `time_offset`, `header_flags`, `xor_key_mask_clear_bits`,
`xor_key`, `mm_rhs`), so serde ignores the additions.

**But btc-rpc-proxy itself cannot serve a v2 block.** This test used a shim
instead, and that substitution is the finding, not a convenience.
`fetch_block_from_peer` in `Start9Labs/btc-rpc-proxy` decodes the peer's `block`
message into `rust-bitcoin`'s `Block` and then checks `b.block_hash()` against
the hash it asked for, plus `check_merkle_root()`. All three steps assume an
80-byte header and SHA256d. `rust-bitcoin` refuses a v2 block outright ("data not
consumed entirely", having read a transaction count of zero from offset 80; see
the `rust_bitcoin_mishandles_a_v2_block` test in `headerv2.rs`).

So on StartOS, pruning plus BLAKE2b needs the proxy taught the new format too.
That is a separate piece of work in a separate repo, and it is not required for a
non-pruned BLAKE2b node.

**The p2p peer must grant `noban`.** Without it bitcoind serves only the last 288
blocks (`NODE_NETWORK_LIMITED`) and disconnects when asked for anything older,
which ends electrs, since it does not reconnect p2p. Not a new finding, and the
StartOS package already binds to bitcoind's `peer-local` host for this reason,
but it is what the first run of this test hit and it is worth stating: at height
384 with the tip at 800, the block is 416 deep and outside the window.

## Running it

```bash
# two nodes on a docker network, B pruned and whitelisted
docker run -d --name b4a --network b4net ... knots-blake2b:rc2
docker run -d --name b4p --network b4net ... -e PRUNE=1 -e FASTPRUNE=1 \
  knots-blake2b:rc2 '-whitelist=noban,download@0.0.0.0/0'
# sync B from A, then: bitcoin-cli pruneblockchain 500

./fallback_proxy.py 19120 http://127.0.0.1:19110 http://127.0.0.1:19100
electrs --conf electrs.toml   # daemon_rpc_addr = the proxy, daemon_p2p_addr = B
```

`fallback_proxy.py` is test scaffolding, not a component: no peer fetching, no
block validation, no auth of its own (it passes the caller's `Authorization`
header straight through). Use btc-rpc-proxy for anything real.
