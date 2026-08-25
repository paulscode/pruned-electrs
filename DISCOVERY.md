# DISCOVERY

Findings for running `electrs` against a pruned Bitcoin Core / Bitcoin Knots
node on StartOS 0.4.0.x.

Everything below was read from pinned source or measured on a local regtest node. Where this
document contradicts the project brief or upstream documentation, the source and the measurement
win; those disagreements are called out explicitly.

Passes: 2026-08-22 discovery · 2026-08-23 proof of concept, mainnet measurements, failure modes ·
2026-08-24 retry fix, concurrency and Tor measurements, packaging.

---

## 1. Pinned versions

Reference checkouts live under [vendor/](vendor/), cloned at these commits.

| Component | Repo | Commit | Tag / branch | Notes |
|---|---|---|---|---|
| electrs (upstream) | `romanz/electrs` | `1222d361ea444e97aadf4c029658b375bcd19060` | `master` | 25 commits ahead of `v0.11.1`; no tag newer than `v0.11.1` exists |
| **electrs (the version StartOS ships)** | `romanz/electrs` | **`35216c6d30148be8e6763d913d437330f431fc03`** | **`v0.11.1`** | The submodule pin in `electrs-startos`. All source analysis below is against this commit. |
| electrs StartOS package | `Start9-Community/electrs-startos` | `c974d63212064616580e8f1dc5f9598907e545f1` | `master`, latest tag `v0.11.1_20` | |
| Bitcoin Core StartOS package | `Start9Labs/bitcoin-core-startos` | `51db7e317f48151a75b270dff49039b397048c80` | `31.x` (default), latest tag `v31.1_10` | Maintained branches: `28.x`, `29.x`, `30.x`, `31.x` |
| Bitcoin Knots StartOS package | `Start9Labs/bitcoin-knots-startos` | `83db50ebc63355fa558dce4534ebe25722c627a9` | `29.x`, latest tag `v29.4_5-knots` | |
| btc-rpc-proxy | `Start9Labs/btc-rpc-proxy` | `1e9a625a54c3737d1f47b63a4063cb03d8604068` | `master`, version `0.5.1` | |
| StartOS SDK | `@start9labs/start-sdk` | — | **`2.0.9`** | Same version in all three packages above. This is the 0.4.0.x SDK. |

electrs `v0.11.1` dependency versions that matter here: `bitcoin 0.32.8`, `bitcoincore-rpc 0.19.0`,
`bitcoin_slices 0.10.0`, `rust-rocksdb 0.36`.

### Build status

- **Upstream electrs `v0.11.1` builds unmodified.** `cargo build --release`, ~2 min on this host.
  Needs `LIBCLANG_PATH` pointing at a directory containing a `libclang.so`; Mint ships
  `/usr/lib/llvm-18/lib/libclang-18.so.1`, which `bindgen`'s glob (`libclang.so`, `libclang-*.so`)
  does not match, so a symlink is required. The StartOS Dockerfile avoids this by installing
  `libclang-dev`.
- The StartOS `.s9pk` build was not attempted this pass — it needs the `start-cli` toolchain and
  contributes nothing to the Phase 1/2 questions. Deferred to Phase 8.

### Carried patch in the StartOS package

`electrs-startos` applies exactly one patch to upstream at build time, with `patch -p1 --fuzz=0`:
`0001-bound-client-write-so-a-wedged-peer-cannot-stall-the-server.patch`, which sets a 60s
`SO_SNDTIMEO` on accepted client sockets. It is unrelated to pruning but it is prior art worth
noting: **the package already has an established mechanism for carrying upstream deltas**, with a
documented retirement condition per patch. A pruning fork should use the same discipline.

---

## 2. Where electrs rejects a pruned node

Confirmed, unchanged from the brief's description — [`src/daemon.rs:137-140`](vendor/electrs/src/daemon.rs#L137-L140):

```rust
let info = rpc.get_blockchain_info()?;
if info.pruned {
    bail!("electrs requires non-pruned bitcoind node");
}
```

This runs once in `Daemon::connect`, after the RPC warmup/IBD poll and after two other guards
(`bitcoind >= 0.21`, `networkactive == true`), and **before** the P2P connection is opened.

Verified end to end against a pruned regtest node:

```
[INFO  electrs::db] "…/regtest": 0 SST files, 0 GB, 0 Grows
Error: electrs failed
Caused by:
    electrs requires non-pruned bitcoind node
```

### The guard is not the real barrier — it is covering a silent hang

This is the single most important finding of this pass, and it changes the shape of the project.

A two-line diagnostic patch (`spikes/p2p-pruned/0001-diagnostic-drop-prune-guard.patch`, applied,
measured, then reverted — `vendor/electrs` is back at a clean `35216c6`) replaced the `bail!` with a
warning. Against the same pruned regtest node, patched electrs:

```
[WARN  electrs::daemon] SPIKE: pruned node accepted; pruneheight=Some(251)
[INFO  electrs::index] indexing 800 blocks: [1..800]
```

…and then **hung forever**. No error, no progress, no timeout. Killed at 60s.

So removing the check does not produce a useful failure — it produces a wedged process. Section 4
explains why, and it is not fixable by a config flag.

One useful incidental: `pruneheight` is already exposed on `bitcoincore-rpc`'s
`GetBlockchainInfoResult` as `prune_height: Option<u64>`, so the routing predicate proposed in
section 8 needs no new RPC plumbing.

---

## 3. How electrs treats `initialblockdownload`

**An assumption in the brief is wrong here, in our favour.** The brief asks whether electrs "can run
while `initialblockdownload=true` with a relatively small upstream change". No change is needed —
**upstream already has a config flag for exactly this.**

[`src/daemon.rs:30-62`](vendor/electrs/src/daemon.rs#L30-L62), `rpc_poll`:

```rust
if skip_block_download_wait {
    // bitcoind RPC is available, don't wait for block download to finish
    return PollResult::Done(Ok(()));
}
let left_blocks = info.headers - info.blocks;
if info.initial_block_download || left_blocks > 0 { … PollResult::Retry }
```

Default behaviour: electrs polls `getblockchaininfo` once a second and refuses to proceed while
`initial_block_download` is true *or* `headers > blocks`. With `skip_block_download_wait = true`
(`config.rs:145`, surfaced as `--skip-block-download-wait`), it proceeds as soon as RPC answers.

Consequence for Architecture B: the "run electrs during Bitcoin's IBD" idea needs **zero upstream
change** to get past the IBD gate. That removes one of the two obstacles the brief anticipated. The
remaining obstacle (keeping ahead of the prune frontier) is unaffected.

The StartOS package does not currently set this flag — `electrs.toml`'s schema
([`fileModels/electrs.toml.ts`](vendor/electrs-startos/startos/fileModels/electrs.toml.ts)) has no
field for it, so it defaults to `false`.

---

## 4. Block and transaction data flow

### 4.1 Everything block-shaped comes over P2P. Nothing comes over RPC.

`Daemon` ([`src/daemon.rs:102-306`](vendor/electrs/src/daemon.rs#L102-L306)) holds two handles: a
`Connection` (P2P) and a `Client` (JSON-RPC). The split is absolute:

| Data | Transport | Function |
|---|---|---|
| Block headers (incl. reorg detection) | **P2P** `getheaders` | `Daemon::get_new_headers` → `p2p::Connection::get_new_headers` |
| Whole blocks — indexing *and* queries | **P2P** `getdata`/`block` | `Daemon::for_blocks` → `p2p::Connection::for_blocks` |
| New-block notification | **P2P** `inv` | `Daemon::new_block_notification` |
| Block txid list | RPC `getblock <hash> 1` | `Daemon::get_block_txids` → `get_block_info` |
| Raw/verbose transaction | RPC `getrawtransaction` | `Daemon::get_transaction{,_hex,_info}` |
| Mempool | RPC (batched) | `get_mempool_{info,txids,entries,transactions}` |
| Fees, relay fee, broadcast | RPC | `estimate_fee`, `get_relay_fee`, `broadcast`, `submitpackage` |

There is **no REST client and no block-file reader** anywhere in the tree. The method does **not**
differ between initial sync and steady state — `Index::sync` runs the same path for a 800 000-block
first run as for a one-block tip update.

Confirmed that `bitcoincore-rpc 0.19.0`'s `get_block_info` issues `getblock <hash> 1`
(`client.rs:344-346`), which matters because that is a verbosity the Start9 proxy intercepts.

### 4.2 Call graph

```
Rpc::sync                                    (electrum.rs)
 └─ Tracker::sync                            (tracker.rs)
     └─ Index::sync                          (index.rs:169)
         ├─ Daemon::get_new_headers          ── P2P getheaders
         │   └─ Chain::update / locator      (chain.rs)
         └─ for chunk in new_headers.chunks(batch_size):   default batch_size = 10
             └─ Index::index_blocks          (index.rs:239)
                 └─ Daemon::for_blocks       ── P2P getdata  ◀── the pruning failure point
                     └─ index_single_block   (bsl visitor → WriteBatch)
                         └─ DBStore::write   (rocksdb)
```

Reader and writer run as two scoped threads joined by a `bounded(1)` channel, so one batch indexes
while the previous batch writes.

### 4.3 Batching, and why it breaks on a pruned node

[`p2p.rs:96-131`](vendor/electrs/src/p2p.rs#L96-L131), `for_blocks`, is the crux:

```rust
self.req_send.send(Request::get_blocks(&blockhashes))?;   // ONE getdata, all hashes
for hash in blockhashes {
    let block = self.blocks_recv.recv()?;                 // blocking, NO timeout
    ensure!(&header.block_hash_sha2()[..] == hash.as_byte_array(), "got unexpected block");
    func(hash, block);
}
```

One `getdata` carries the whole batch; replies are consumed **positionally**, each checked against
the hash expected at that position. The assumption is total: *every requested block will come back,
in order*.

**Measured** what bitcoind actually does when that assumption fails
(`spikes/p2p-pruned/p2p_probe.py`, a dependency-free P2P client that mimics electrs' handshake —
`services = NONE`, `relay = false` — and sends one batched `getdata`).

Setup: Knots v29.3 regtest, `-fastprune`, 800 blocks mined, `pruneblockchain 500` → `pruneheight=251`.

| # | Peer connection | Request | Result |
|---|---|---|---|
| A | `whitebind` (noban+download) | retained block h799 | block returned, <10 ms |
| B | `whitebind` | pruned block h100 | **silence.** No `block`, no `notfound`, no disconnect. Timed out at 15 s. |
| C | plain `bind` (no permissions) | pruned block h100 | **immediate disconnect** |
| D | `whitebind` | batch `[h100 (pruned), h798, h799]` | h798 and h799 returned **in order, pruned one silently skipped** |

So electrs against a pruned node has two failure modes, neither of them a usable signal:

- **Batch entirely below the prune height** → `blocks_recv.recv()` blocks forever. This is the
  observed hang in section 2: the first batch is `[1..10]`, all pruned, so it wedges on block 1.
- **Batch straddling the prune height** (case D) → the first reply is the *wrong* block for the
  position, `ensure!` fires `got unexpected block`, and indexing dies with a misleading error.

**This kills "detect a pruned-data error and fall back transparently" as posed in the brief's Phase
5.** Over P2P there is no error to detect. Any fallback would have to be a timeout heuristic, which
is both slow and unreliable (a real block on a busy node can take longer than any threshold short
enough to be useful).

Case C is also worth flagging for packaging: it is a second, independent reason the StartOS package
must keep using the `peer-local` (whitebind) host rather than `peer`. On a plain binding a pruned
node **disconnects** the requester, and electrs does not reconnect P2P — `p2p_loop` exiting drops
`new_block_send` and takes the process down by design.

### 4.4 The boundary is exact and `pruneheight` is a reliable predicate

Measured at the boundary, tip = 800, `pruneheight` = 251:

| Height | Depth | `getblock <hash> 0` | P2P `getdata` (whitebind) |
|---|---|---|---|
| 100 | 700 | `Block not available (pruned data)` | silence |
| 250 | 550 | `Block not available (pruned data)` | silence |
| **251** | 549 | served | **served** |
| 300 | 500 | served | served |
| 799 | 1 | served | served |

Two things follow, and together they are the basis for the recommended architecture:

1. **`getblockchaininfo.pruneheight` is an exact routing predicate.** Blocks at height
   `>= pruneheight` are locally available; below it they are not. No probing, no timeouts, no
   heuristics needed — electrs can know before it asks.
2. **A `noban` peer is served retained blocks at any depth.** h251 at depth 549 came back fine,
   far beyond `NODE_NETWORK_LIMITED_MIN_BLOCKS` (288). The whitebind permission bypasses the
   limited-peer threshold, so the fast P2P path stays fully usable for everything bitcoind still
   holds.

### 4.5 RPC prune errors are exactly what the proxy keys on

Measured on the same node:

```
$ bitcoin-cli getblock <pruned-hash> 0   →  error code: -1, "Block not available (pruned data)"
$ bitcoin-cli getblock <pruned-hash> 1   →  error code: -1, "Block not available (pruned data)"
$ bitcoin-cli getblock <pruned-hash> 2   →  error code: -1, "Block not available (pruned data)"
$ bitcoin-cli getblockheader <pruned-hash>  →  succeeds (headers are never pruned)
```

Code `-1` and that exact string are what btc-rpc-proxy matches on
(`client.rs:22,26`: `MISC_ERROR_CODE = -1`, `PRUNE_ERROR_MESSAGE = "Block not available (pruned
data)"`) to decide a block needs fetching from peers. The contract holds on Knots 29.3.

`getblockheader` surviving pruning is what makes the proxy's verbosity-1 path work at all (§6.2).

---

## 5. What electrs actually stores — and why this is the hard part

Five RocksDB column families (`db.rs:39`): `config`, `headers`, `txid`, `funding`, `spending`.

- `headers` — full 80-byte block headers, plus a tip key. This is the whole `Chain`, rebuilt into
  memory at startup (`chain.rs:60`).
- `txid`, `funding`, `spending` — `HashPrefixRow`, which is **12 bytes: an 8-byte hash prefix plus a
  4-byte height** (`types.rs:39-56`). Nothing else.

**electrs stores no transactions, no txids, no transaction positions, and no merkle data.** The
index is a lossy map from a truncated hash to a height. That design is deliberate — it is why the
index is ~60 GB rather than the size of the chain — but it means:

> Every scripthash and txid query resolves candidate *heights*, then must **re-download and re-scan
> the whole block** to recover the actual data and to discard prefix collisions.

The re-scan is not a bootstrap-only cost. It is the steady-state query path, forever. Confirmed in
`electrs-startos`'s own `AGENTS.md`, and in the source:

- [`status.rs:315-326`](vendor/electrs/src/status.rs#L315-L326) — `for_new_blocks` → `daemon.for_blocks`,
  filtered only by blocks this *subscription* has already seen.
- [`tracker.rs:105-124`](vendor/electrs/src/tracker.rs#L105-L124) — `lookup_transaction` → `daemon.for_blocks`,
  then a `FindTransaction` visitor scans for the txid.

The `Cache` (`cache.rs`) is an unbounded in-memory `HashMap<Txid, Box<[u8]>>` populated during
scripthash sync. It is not persisted and has no eviction, so it helps a subscribed wallet on repeat
queries and does nothing for a cold start or an unsubscribed query.

**Implication for Phase 6:** there is no configuration of upstream electrs in which a pruned backend
serves historical queries. The data simply is not in the index. Either historical blocks stay
retrievable on demand (proxy), or electrs must persist more than it does today. This is a real
architectural fork in the road, and it is independent of how bootstrap is solved.

---

## 6. Electrum method dependency table

Classification per the brief. "Old raw block" means a block that may sit below `pruneheight`.

| Electrum method | Path | Dependency class | Pruned-node behaviour |
|---|---|---|---|
| `blockchain.block.header` | `Chain` (in-memory, from `headers` CF) | **electrs DB only** | fine |
| `blockchain.block.headers` | `Chain` | **electrs DB only** | fine |
| `blockchain.headers.subscribe` | `Chain` | **electrs DB only** | fine |
| `server.version`, `server.banner`, `server.ping`, `server.features`, `server.donation_address`, `server.peers.subscribe` | local | **electrs DB only** | fine |
| `blockchain.transaction.broadcast` | RPC `sendrawtransaction` | **RPC, no historical data** | fine |
| `blockchain.transaction.broadcast_package` | RPC `submitpackage` | **RPC, no historical data** | fine |
| `blockchain.estimatefee` | RPC `estimatesmartfee` | **RPC, no historical data** | fine |
| `blockchain.relayfee` | RPC `getnetworkinfo` | **RPC, no historical data** | fine |
| `mempool.get_fee_histogram` | mempool (RPC-fed) | **RPC, no historical data** | fine |
| `blockchain.scripthash.get_history` | index → **`for_blocks` (P2P)** | **requires old raw block** | hang / `got unexpected block` |
| `blockchain.scripthash.subscribe` | same | **requires old raw block** | same |
| `blockchain.scripthash.get_balance` | same (`Unspent::build` over synced status) | **requires old raw block** | same |
| `blockchain.scripthash.listunspent` | same | **requires old raw block** | same |
| `blockchain.transaction.get` (verbose=false) | `Cache` → `lookup_transaction` (**P2P**) → RPC `getrawtransaction` | **requires old raw block** | cache miss ⇒ hang |
| `blockchain.transaction.get` (verbose=true) | `lookup_transaction` (**P2P**) for blockhash, then RPC `getrawtransaction txid true blockhash` | **requires old raw block** *and* **old raw tx via RPC** | hang; and the RPC leg is **not** proxy-intercepted (§6.2) |
| `blockchain.transaction.get_merkle` | `Chain` → RPC **`getblock <hash> 1`** | **requires old raw block, via RPC** | **proxy-servable** (§6.2) |
| `blockchain.transaction.id_from_pos` | `Chain` → RPC **`getblock <hash> 1`** | **requires old raw block, via RPC** | **proxy-servable** (§6.2) |
| *(indexing, not a client method)* | `Index::sync` → `for_blocks` (**P2P**) | **requires every historical block** | hang |

**Undo data: not required anywhere.** Nothing in electrs reads `rev*.dat` or any undo/spent-outputs
source. Reorg handling (§9) is header-driven and re-indexes forward rather than unwinding. That
removes one of the brief's worries entirely.

### 6.1 Minimum permanent storage vs. on-demand retrieval

- **Must be permanent:** the header chain (already stored), and the prefix→height rows (already
  stored). Nothing else is strictly required *if* historical blocks stay retrievable.
- **Retrievable on demand:** every raw historical block, for both scripthash queries and
  `transaction.get`.
- **The catch:** "on demand" here is not rare. A fresh wallet connecting and subscribing to a few
  hundred scripthashes with old history can trigger hundreds of whole-block fetches in one burst.
  Sizing that against proxy latency is the top open question (§10).

### 6.2 Exactly two of the affected methods are already proxy-servable

`transaction.get_merkle` and `id_from_pos` reach for `getblock <hash> 1`, which the proxy
intercepts. Both would work through a pruned StartOS backend **today, unmodified** — if electrs ever
got far enough to serve them.

`transaction.get` with `verbose=true` will **not**: its second leg is `getrawtransaction`, which the
proxy does not intercept, and which on a pruned node fails even with `txindex` for blocks whose data
is gone. Serving it correctly requires reconstructing the verbose form from the raw block, or
accepting a documented gap.

---

## 7. btc-rpc-proxy capabilities and limits

Read from source at `1e9a625` (v0.5.1). Not yet measured — Phase 3 will measure.

### What it intercepts

[`users.rs:215-330`](vendor/btc-rpc-proxy/src/users.rs#L215-L330). Only `getblock`, and only when
the authenticated user has `fetch_blocks`:

- **verbosity 0** → `fetch_block_raw`, returns hex block. 
- **verbosity 1** → local `getblockheader <hash> true` **joined with** a peer-fetched block, assembled
  into a `GetBlockResult` (header fields + `size`/`strippedsize`/`weight`/`tx[]`). 
- **verbosity absent** → treated as 1 (`params.get(1).unwrap_or(&1_u64.into())`). 
- **verbosity 2** → falls through to `Ok(None)`, i.e. **forwarded to bitcoind**, which fails with
  `Block not available (pruned data)`. The brief's expectation here is correct.
- **Everything else** (`getrawtransaction`, `getblockchaininfo`, …) → forwarded untouched.

### How it fetches

[`fetch_blocks.rs:397-424`](vendor/btc-rpc-proxy/src/fetch_blocks.rs#L397-L424):

1. `fetch_block_from_self` — try local `getblock <hash> 0` first. **So retained blocks cost one extra
   local RPC hop and never touch the network.** Only on the exact `-1 / "Block not available (pruned
   data)"` pair does it fall through.
2. `get_peers` — `getpeerinfo`, filtered to `!inbound` **and** `servicesnames` contains `NETWORK`.
   Pruned peers advertising only `NETWORK_LIMITED` are excluded. Peer list cached for `max_peer_age`
   (default 300 s).
3. `fetch_block_from_peers` — fans out to peers `for_each_concurrent(max_peer_concurrency)`, keeps
   the **first** valid answer, discards the rest.

### Validation — stronger than electrs' own

Per peer reply (`fetch_blocks.rs:309-326`): `block_hash() == requested hash`, `check_merkle_root()`,
`check_witness_commitment()`. All three must pass.

This is sound and it is **stricter than what electrs does for its own P2P blocks** — `for_blocks`
checks only the block hash, because it trusts bitcoind. Since the requested hash comes from the
local node's validated header chain, and the header commits to the merkle root, a peer cannot
substitute content. Routing a fetch through the proxy therefore does not weaken the trust model; it
tightens it.

### Three defects found, all fixed and measured

Patches in [spikes/proxy-regtest/](spikes/proxy-regtest/), against `1e9a625`. All three are
upstream bugs in Start9's proxy, not artefacts of this project; 2 and 3 affect every pruned StartOS
node today.

**0001 — mainnet magic is hardcoded.** `Network::Bitcoin.magic()` at four sites in
`fetch_blocks.rs`. The peer-fetch path **cannot work on regtest, signet or testnet** — a peer on
another network drops the connection rather than answering. Adds a `network` config param
(default `bitcoin`, so deployments are unaffected). This is a test-harness enabler, not a
production bug.

**0002 — peer-fetched blocks are witness-stripped.** The fetcher asks for `Inventory::Block`
(`MSG_BLOCK`), which makes a segwit-aware peer serve the *stripped* serialization. The proxy
re-encodes that and returns it as the answer to `getblock`. Neither validation catches it: the
merkle root commits to txids, which stripping does not change, and `check_witness_commitment()`
returns `true` vacuously once no transaction carries a witness. So the loss is silent.

Measured on regtest against an archival peer, block at height 100:

```
archival node : 504 hex chars
proxy, before : 432 hex chars   ← missing marker/flag + coinbase witness
proxy, after  : 504 hex chars   ← byte-identical
```

electrs already asks its own peer for `WitnessBlock`, so without this fix a pruning-aware electrs
would get a *different serialization* above and below the prune height — and would return
witness-stripped transactions to wallets. Fixed by requesting `Inventory::WitnessBlock`.

**0003 — a Nagle stall on every peer fetch.** `consensus_encode` writes a p2p message to the socket
in several small pieces; with Nagle on, the kernel holds each piece after the first until the peer
ACKs, which the peer's delayed-ACK timer defers by ~40 ms. Nothing in the tree sets `TCP_NODELAY`.

Measured over loopback, 100 sequential pruned-block fetches:

| | blocks/s | median | p95 |
|---|---|---|---|
| before | 23.7 | 42.03 ms | 43.08 ms |
| after | **1692.0** | **0.58 ms** | 0.68 ms |

**Correction, twice over.** The first pass of this document called the saving "a flat ~40 ms per
block", and the second called it size-dependent. Both were wrong, and the second was wrong in a way
worth recording.

`TCP_NODELAY` changes only what the *proxy writes*, and the proxy writes one `getdata` per fetch:
24-byte header, a varint, and a 36-byte inventory entry, so 61 bytes, whatever the block turns out
to weigh. The block travels the other way. So no measurement showing latency scaling with block size
can be attributed to this option, and the mainnet table below cannot mean what it was read to mean.
Raised by the maintainer on the upstream PR and correct.

What stands is the loopback measurement, where the option was the only variable, the peer and blocks
were identical, and the effect is unambiguous. The mainnet A/B was run against different peers at
different times and controls for neither, so it is reported here as an uncontrolled observation:

| height | block size | with `TCP_NODELAY` | without |
|---|---|---|---|
| 200000 | 0.25 MB | 152.6 ms | 342.8 ms |
| 900000 | 1.92 MB | 172.7 ms | **931.7 ms** |

The gap is real and the direction is right. The size scaling in the right-hand column is peer
variance, not Nagle.

### Other limits (not fixed)

- **No retry on a stale pooled connection.** Observed: a connection recycled from an earlier fetch
  came back `Broken pipe`, and because there is no retry the whole `getblock` failed with
  `Block not available (pruned data)` — which took electrs' indexing down with it. With one eligible
  peer this is fatal; with several, `max_peer_concurrency` masks it. **A pruning-aware electrs must
  not treat a single failed fetch as terminal**, and the proxy should retry a dead pooled
  connection once. Not yet patched.
- **No cache.** Every miss re-fetches from the network. Nothing is written back to bitcoind's block
  store; the block exists only in the response. Sequential historical scanning pays full network
  cost every time. Confirmed by measurement: repeated fetches of the same pruned block cost the
  same each time.
- **No batching, no pipelining.** One `getblock` = one JSON-RPC request = up to `max_peer_concurrency`
  parallel peer fetches for that single block. electrs' P2P path pipelines a whole batch in one
  `getdata`; the proxy path cannot.
- **Amplification.** StartOS sets `max_peer_concurrency: 3`, so each pruned block is requested from
  3 peers and 2 copies are thrown away — 3× network cost per block.
- **Connection reuse is one-deep per peer.** `Peer` holds an `mpmc::bounded(1)` slot
  (`fetch_blocks.rs:200-212`); concurrent requests beyond that open fresh connections and redo the
  version/verack handshake.
- **`peer_timeout` default 30 s** applies both to connecting and to each block fetch.
- **No retry loop.** If no peer answers, `fetch_block_from_peers` returns `None` → the RPC returns
  `Block not available (pruned data)`. The caller must handle it.
- Tor / I2P: `.onion` peers go via `tor_proxy`; `.i2p` peers need a separate `i2p_proxy` (Tor cannot
  resolve them); `tor_only` forces everything through Tor.

---

## 7a. What a historical block actually costs, against real peers

The number §11.1 was missing. Measured with
[spikes/mainnet-fetch/fetch_bench.py](spikes/mainnet-fetch/fetch_bench.py), which replicates the
proxy's `fetch_block_from_peer` exactly — same handshake (`services=NONE`, `relay=0`), same
one-block-per-`getdata`, same validation — against real mainnet peers found via DNS seeds. It needs
no local node and no pruned node, because the cost being measured is peer→us and is identical
whoever asks. 5 fetches per height, clearnet, this workstation.

| height | block size | median | min | max | MB/s |
|---|---|---|---|---|---|
| 200000 | 0.25 MB | 152.6 ms | 151.6 | 172.4 | 1.62 |
| 400000 | 0.95 MB | 159.5 ms | 158.3 | 326.0 | 5.95 |
| 600000 | 0.87 MB | 158.4 ms | 155.8 | 280.4 | 5.50 |
| 700000 | 1.28 MB | 164.6 ms | 162.0 | 166.2 | 7.76 |
| 800000 | 1.63 MB | 166.3 ms | 165.5 | 176.6 | 9.83 |
| 900000 | 1.92 MB | 172.7 ms | 171.2 | 182.9 | 11.12 |

**Median 162 ms per block, and essentially flat in block size** — 0.25 MB and 1.92 MB cost the same
within 13%. The fetch is round-trip-bound, not bandwidth-bound. That single fact drives three
conclusions:

1. **Sequential full-chain bootstrap over the proxy is not viable.** 900k blocks × 162 ms ≈
   **40.5 hours** from one peer, and that is the *patched* figure; unpatched it is 136 hours. Even
   with a fast link. This settles the open question from the first pass: **Architecture A cannot be
   the bootstrap path.**
2. **Parallelism helps, but far less than the flat-latency result suggests** — now measured, see
   §7c. The prediction here was "nearly linear, 40.5 h → ~2.5 h". That was wrong: aggregate
   throughput becomes bandwidth-bound once several fetches are in flight, so the real gain over a
   *good* single peer is about **2×**, not 16×.
3. **Query-time bursts are fine.** A fresh wallet whose scripthashes touch 500 historical blocks
   costs 500 × 162 ms ≈ 81 s sequentially, and a few seconds with modest concurrency. That is the
   workload the proxy path actually has to serve, and it serves it comfortably.

Caveats: one peer at a time, one network location, clearnet. The Tor case is measured separately in
§7d, and it is much worse.

---

## 7c. Does concurrency rescue the RPC path? Partly.

[spikes/mainnet-fetch/concurrency_bench.py](spikes/mainnet-fetch/concurrency_bench.py). A shared
work queue of blocks drained by N peer workers — *not* one block per peer, which measures nothing
useful because peer quality varies by more than 10× and the slowest peer then sets the wall time.
Peers are vetted with a probe fetch first, so this separates "does concurrency scale" from "how many
public peers are junk". Full numbers in [spikes/mainnet-fetch/RESULTS.md](spikes/mainnet-fetch/RESULTS.md).

| N peers | run A | run B |
|---|---|---|
| 1 | 0.63 blk/s | 1.30 blk/s |
| 4 | 4.75 (7.5×) | 7.06 (5.4×) |
| 12 | **9.70 (15.4×)** | **13.21 (10.2×)** |

Most of the gain lands by N=4–8. A third run during a bad network moment showed no scaling at all
beyond N=12 and started dropping connections — peer quality and available bandwidth dominate, and
they vary by ~7× between runs minutes apart.

**Correction to the prediction in §7a.** Flat-in-size latency suggested near-linear scaling and a
40.5 h → 2.5 h bootstrap. That was wrong. The 10–15× figures above are relative to a *slow* baseline
peer; against the *good* single peer that produced §7a's 6.2 blocks/s, 12 concurrent peers reach
13.2 blocks/s — about **2×**. Once several fetches are in flight the aggregate is bandwidth-bound,
not round-trip-bound, and the per-request flatness stops mattering.

Also measured: **pipelining N requests down one connection is the wrong shape.** Throughput pinned
at ~0.27 MB/s across N=1..16 on a bandwidth-capped peer — it saturates the link rather than beating
it. Peer-parallelism beats connection-pipelining.

So:

- **Cold bootstrap: still no.** ~19 h at the best observed rate, against ~40 h sequential. Better,
  nowhere near enough. **Architecture B remains required.**
- **Query bursts: comfortably yes, and ~2× better than sequential.** 500 historical blocks in ~38 s
  rather than ~81 s.
- **The proxy cannot deliver this as built.** `max_peer_concurrency: 3` is a *per-block* fan-out —
  three peers asked for the *same* block, two answers discarded — not a work queue. Getting the
  scaling above would need electrs to issue concurrent `getblock` calls *and* the proxy to spend its
  peer concurrency across different blocks instead of racing them on one.

---

## 7d. Over Tor — 13× worse, and it changes the advice

`fetch_bench.py --tor 127.0.0.1:9050`. Onion peers harvested from clearnet peers' `addrv2` gossip
(DNS seeds return clearnet only, so BIP155 is the only way to reach them). This is the network a
StartOS node running `onlynet=onion` actually uses — as the reference Start9 test server does.

| height | block size | median | MB/s |
|---|---|---|---|
| 400000 | 0.95 MB | 2118.3 ms | 0.45 |
| 700000 | 1.28 MB | 2029.2 ms | 0.63 |
| 900000 | 1.92 MB | 3667.4 ms | 0.52 |

**Median 2118 ms/block against 162 ms on clearnet — 13× slower.** Throughput 0.45–0.63 MB/s against
5.95–11.12, so ~13–18× worse. The header walk shows the same penalty on pure round-trip work: 900k
headers in 439 s over Tor against 21 s on clearnet, **~21×**.

Extrapolated full chain, sequential, one peer: **529.6 hours — 22 days.**

Three consequences, and they are the practical conclusions of this whole exercise:

1. **Cold bootstrap over the proxy is out of the question for a Tor-only node.** Not "slow" — 22
   days sequential, and concurrency (§7c) at best divides that by a small factor. Indexing during
   bitcoind's IBD is not an optimisation for these users, it is the only workable path.
2. **Query bursts become noticeable — unless fetched concurrently.** 500 historical blocks is ~17
   minutes sequentially over Tor, against ~81 s on clearnet. But concurrency helps *more* here than
   on clearnet, because Tor is heavily round-trip-bound and parallel circuits mask latency instead
   of contending for bandwidth:

   | N onion peers | blocks/s | speedup |
   |---|---|---|
   | 1 | 0.97 | 1.00× |
   | 4 | 3.09 | 3.17× |
   | 8 | **5.92** | **6.08×** |

   At N=8 that same 500-block burst is ~84 s. **This reverses the "concurrency is probably not worth
   it" verdict in §7c** — on clearnet it buys ~2× and is optional; for Tor users it is the difference
   between a wallet's first connect taking 17 minutes and taking under 90 seconds. Full chain at
   that rate is still ~42 h, so it remains no help for bootstrap.
3. **The packaging must tell users to install early.** A user who installs onto a long-pruned
   Tor-only node has a materially different experience from one who installs before bitcoind
   syncs. That is now written into `instructions.md` rather than left to be discovered.

---

## 7b. Failure modes (Phase 7)

Run with [spikes/harness/failure_modes.sh](spikes/harness/failure_modes.sh) on the two-node harness.

All nine pass, as of the retry work in electrs patch 0002.

| Scenario | Result |
|---|---|
| **electrs stopped, bitcoind advances 400 blocks and prunes past where electrs stopped** | **pass** — repaired the whole gap, 300+ blocks of it below the new prune height |
| Proxy unavailable, client query needs a pruned block | **pass** — clean JSON-RPC error after a 10 s retry budget, no hang |
| Proxy unavailable, electrs process | **pass** — stays up |
| Proxy returns | **pass** — recovers with no restart |
| **Pruned blocks unservable during indexing (archival peer gone)** | **pass** — stays up, retries with backoff |
| …then the peer returns | **pass** — index completes **without restarting electrs** |
| Reorg (8-block branch replacing 3) | **pass** — index converged on the active chain |
| Interrupted initial indexing | **pass** — resumed from a partial index, reached tip |

Two findings matter.

**The gap-repair result removes the main argument for lifecycle coordination.** The brief asked
whether, if bitcoind advances and prunes blocks electrs never saw, proxy retrieval can fully repair
the gap. It can, and it does so with no special handling — the `pruneheight` split simply routes the
missing range to RPC on the next sync. Nothing needs to orchestrate electrs and bitcoind start
order, and a stalled electrs is recoverable rather than fatal.

**A failed fetch during indexing used to be terminal; it no longer is.** The first implementation let
the error propagate out of `Index::sync`, ending the process. electrs patch 0002 retries with
exponential backoff, and the budget differs by caller — which turned out to be load-bearing rather
than cosmetic. `handle_events` and `rpc.sync()` share one thread (`server.rs`), so an intermediate
version with a single 300 s budget everywhere meant **one query against a downed proxy froze the
whole server for five minutes**. Indexing now gets 300 s, where stalling beats dying; serving gets
10 s, where a prompt error the wallet can retry beats a freeze.

Note on testing this: killing the proxy is the wrong lever, because with no RPC endpoint electrs
never gets past `Daemon::connect`, which legitimately refuses to start. Stopping the *archival peer*
reproduces the realistic failure deterministically — bitcoind and the proxy stay up and answer
`getblockchaininfo`, but no peer holds the pruned blocks, so every pruned fetch fails. That is peer
churn, which is the common case in the field.

---

## 8. StartOS packaging behaviour

### 8.1 Where pruning is disabled — found

[`electrs-startos/startos/dependencies.ts:5-24`](vendor/electrs-startos/startos/dependencies.ts#L5-L24):

```ts
export const setDependencies = sdk.setupDependencies(async ({ effects }) => {
  await sdk.action.createTask(effects, 'bitcoind', autoconfig, 'critical', {
    input: { kind: 'partial', accept: [{ prune: 0 }], set: { prune: 0 } },
    when: { condition: 'input-not-matches', once: false },
    reason: i18n('Electrs requires an archival bitcoin node.'),
  })
  return {
    bitcoind: {
      healthChecks: ['bitcoind', 'sync-progress'],
      kind: 'running',
      versionRange: '(>=28.4:17 && <29) || (>=29.4:4 && <30) || (>=30.3:4 && <31) || >=31.1:4',
    },
  }
})
```

It is a **`critical` task with `once: false`** — so it re-fires whenever the user sets `prune != 0`,
not just at install. For this project that whole `createTask` block is removed (or inverted).

`versionRange` gates on the bitcoind revisions that introduced the `peer-local` host.

### 8.2 How electrs reaches bitcoind

[`electrs-startos/startos/utils.ts:49-66`](vendor/electrs-startos/startos/utils.ts#L49-L66),
`bitcoindBridge`, resolves two LXC-bridge addresses reactively and `main.ts` writes them into
`electrs.toml`:

- **RPC** → `hostId: rpcHostId` (`'rpc'`), `internalPort: rpcPort` (**8332**), `ssl: false`.
- **P2P** → `hostId: peerLocalHostId` (`'peer-local'`), `internalPort: peerPortLocal` (**58334**) —
  deliberately *not* `peer`, because `peer-local` is bitcoind's `whitebind` listener and grants
  `noban` + `download`.

Auth: bitcoind's data volume is mounted read-only at `/mnt/bitcoind`, and `electrs.toml` sets
`cookie_file = '/mnt/bitcoind/.cookie'`. `auth` is pinned `undefined` because electrs exits if both
are set. `main.ts` also holds a reactive read on the cookie file so electrs restarts when bitcoind
rotates it.

### 8.3 How the pruned-node proxy is exposed

[`bitcoin-core-startos/startos/main.ts:580-641`](vendor/bitcoin-core-startos/startos/main.ts#L580-L641).
**Identical in the Knots package** (same line region, same values) — verified.

When `prune` is set:

- bitcoind's RPC moves to `rpcPortPruned` = **58332**, bound `127.0.0.1` only.
- a `proxy` daemon starts in its own subcontainer and binds **8332** on `0.0.0.0`.

So **port 8332 is the proxy when pruned and bitcoind when archival** — and `bitcoindBridge` resolves
`rpcHostId`/8332 either way. **electrs needs no packaging change to be pointed at the proxy; it
already is, whenever bitcoind is pruned.**

Proxy config written by the package:

```ts
bitcoind_address: '127.0.0.1', bitcoind_port: 58332,
bind_address: '0.0.0.0',       bind_port: 8332,
cookie_file: rpcCookiePath,
tor_proxy: torSocks,  tor_only: <onlynet == onion>,
default_fetch_blocks: true,
max_peer_concurrency: 3,
...(i2p ? { i2p_proxy: `127.0.0.1:${i2pdPort}` } : {}),
passthrough_rpcauth: `${rootDir}/bitcoin.conf`,
passthrough_rpccookie: rpcCookiePath,
```

Critically, `create_state.rs:113-127`: users derived from `passthrough_rpccookie` get
`allowed_calls: None` (⇒ all methods permitted) and `fetch_blocks: None` (⇒ inherits
`default_fetch_blocks: true`). **electrs' existing cookie auth therefore already yields a
block-fetching proxy user with no restrictions.**

### 8.4 Core / Knots parity

Both packages: id `bitcoind`, SDK `2.0.9`, `rpcPort` 8332, `rpcPortPruned` 58332,
`peerLocalHostId` `'peer-local'`, `peerPortLocal` 58334, and byte-identical proxy blocks. **A single
Electrs package can support both with no implementation-specific coupling**, provided it depends
only on the `bitcoind` id, the two host ids, and `getblockchaininfo`.

Note: `electrs-startos` imports these constants from `bitcoin-core-startos/startos/utils` at compile
time (a `package.json` dependency on `#next/28.x`). That is a build-time constant import, not a
runtime coupling to Core.

---

## 9. Reorg handling as it stands

`Index::sync` re-derives headers each pass via `get_new_headers`, which sends a `getheaders` with
`Chain::locator()`. `Chain::update` truncates `self.headers` from the first new height and re-appends
(`chain.rs:96-113`). Stale blocks are then filtered at query time by
`ScriptHashStatus::confirmed_height_entries`, which drops any `blockhash` no longer in `Chain`.

Two consequences:

- **No undo data is needed** — stale index rows are not deleted, they are *ignored*, because
  `filter_by_*` maps height→hash through the current `Chain`.
- **A reorg requires re-fetching the new chain's blocks**, which on a pruned node is the same
  problem as everything else. A reorg deeper than `pruneheight` would need the proxy. In practice
  `pruneheight` on a StartOS node sits far below any plausible reorg depth, so this is a corner
  case, not a design driver.

`reindex_last_blocks` (`Chain::drop_last_headers`) exists to force re-indexing of the last N blocks
at startup, which is the intended recovery for a suspected bad tip.

---

## 9a. Proof of concept — achieved

The brief's preferred PoC — *"modified electrs successfully indexes at least one block that Bitcoin
has already pruned by retrieving it through Start9's RPC proxy"* — is done, and goes further: the
full index builds and serves correct query results at pruned heights.

**Change:** one patch, one call site.
[patches/0001-daemon-route-blocks-below-bitcoind-s-prune-height-to.patch](patches/0001-daemon-route-blocks-below-bitcoind-s-prune-height-to.patch).
`Daemon::for_blocks` walks the requested batch as maximal runs of same-availability blocks, sending
retained runs down the existing single-`getdata` P2P path and pruned runs to `getblock <hash> 0`.
Runs rather than a partition, so each keeps streaming in the caller's order without buffering the
batch. Archival nodes take an early return on `!self.pruned` and are byte-for-byte unchanged.

Because the split lives in `Daemon::for_blocks`, it covers **all four** callers at once — indexing
(`index.rs`), scripthash sync (`status.rs`, twice) and `lookup_transaction` (`tracker.rs`) — so the
query path is fixed by the same change as the bootstrap path.

**Harness:** [spikes/harness/](spikes/harness/), `./setup.sh up`. Archival node A ← P2P ← pruned
node B; proxy in front of B's RPC; electrs with RPC→proxy and P2P→B's whitebind. 800 blocks, B
pruned to `pruneheight=251`.

**Result:** electrs indexes all 800 blocks, 250 of which bitcoind no longer has, in **0.13 s**
(10.4 s before the `TCP_NODELAY` fix).

**Electrum verification** ([spikes/harness/query.py](spikes/harness/query.py)), all at height 100,
well below the prune height — every one of these hangs on stock electrs:

| Method | Result |
|---|---|
| `blockchain.block.header` | pass |
| `blockchain.transaction.get` | pass — byte-identical to the archival node, **witness included** |
| `blockchain.transaction.get_merkle` | pass |
| `blockchain.transaction.id_from_pos` | pass |
| `blockchain.scripthash.get_history` | pass — 800 entries, min height 1 |
| `blockchain.scripthash.get_balance` | pass |
| `blockchain.transaction.get` **verbose=true** | **fail** — `Block not available` |

6/7. The one failure is exactly the case predicted in §6.2: its second leg is `getrawtransaction`,
which the proxy does not intercept. That is now a confirmed gap rather than a suspicion.

The `transaction.get` pass is also an independent check on proxy patch 0002 — it ran before any
scripthash sync, so the cache was cold and the transaction came through the proxy; matching the
archival node byte-for-byte means the witness survived.

### What this does *not* yet show

- Nothing is measured at mainnet block sizes or over real peer latency. Regtest blocks here are
  ~250 bytes, so §7's numbers are per-request overhead, not throughput.
- No reorg, restart-gap, or proxy-unavailable testing yet (Phase 7).
- The failed-fetch path still kills indexing — see the stale-connection note in §7.

---

## 10. Corrections to the brief's assumptions

Recorded per the brief's request.

1. **Wrong: "electrs obtains initial blocks through Bitcoin P2P [and] historical transactions via
   RPC."** Half right. Blocks come over P2P — but so do *historical* blocks, at query time, forever
   (§5). The RPC transaction path (`lookup_transaction`) is itself P2P-backed. This makes the
   problem permanent rather than bootstrap-only, and it is the main reason Architecture B alone
   cannot work.
2. **Wrong: "Can electrs run while `initialblockdownload=true` with a relatively small upstream
   change?"** No change needed — `skip_block_download_wait` already exists upstream (§3).
3. **Wrong (Phase 5): "Can we detect a pruned-data error and fall back transparently?"** Over P2P
   there is no error. bitcoind returns silence for a pruned block on a `noban` connection and
   disconnects on an unprivileged one (§4.3). Fallback must be *predictive*, driven by
   `pruneheight`, not reactive.
4. **Right, and load-bearing: the proxy intercepts `getblock` 0 and 1 but not 2.** Confirmed in
   source (§7). Also confirmed: it tries the local node first, so archival operation is unaffected.
5. **Right: the StartOS electrs package forces `prune = 0`.** Located exactly (§8.1).
6. **New, not anticipated: btc-rpc-proxy hardcodes mainnet P2P magic.** Its peer-fetch path is
   inoperable on regtest/signet/testnet (§7). The brief's Phase 3 and Phase 9 Scenario 3 both assume
   proxy testing on regtest; that cannot work against the proxy as it stands. **Now fixed**
   (patch 0001), which unblocks the whole regtest test plan.
6a. **New: the proxy returns witness-stripped blocks** (patch 0002) and **stalls ~40 ms per fetch on
   Nagle** (patch 0003). Both are live defects on StartOS pruned nodes today, independent of this
   project.
7. **New: undo data is never needed** (§6, §9). The brief lists "requires undo data" as a dependency
   class; nothing falls into it.
8. **New: `pruneheight` is an exact, cheap routing predicate**, and `noban` peers are served
   retained blocks at any depth (§4.4). This is what makes a clean hybrid possible.

---

## 11. Remaining unknowns

Resolved this pass: concurrency scaling (§7c), Tor latency (§7d), retry behaviour (§7b, electrs
patch 0002), and all Phase 7 failure modes.

1. **Prune-frontier timing during IBD.** Now the largest unknown, and the last undemonstrated piece
   of the recommended architecture. §7a/§7c/§7d together rule out proxy-only bootstrap on every
   network, so indexing alongside bitcoind's IBD is mandatory. How early bitcoind starts pruning,
   and what margin electrs has at realistic indexing rates, decides whether a large `prune` target
   suffices on its own.
2. **`transaction.get verbose=true`.** Confirmed broken on pruned blocks (§9a) — its second leg is
   `getrawtransaction`, which the proxy does not intercept. Three options: reconstruct the verbose
   form from the raw block electrs can already fetch, extend the proxy, or document the gap. Needs
   to know whether Sparrow actually calls it, which is a question about the wallet, not the server.
3. **A bounded historical-block cache.** §7d makes this look more attractive than it did: repeated
   query bursts on a Tor node re-fetch the same blocks at ~2 s each, and the proxy caches nothing.
   Must stay bounded, or it eats the disk saving that motivates the project.
4. **How to implement concurrent fetching.** §7d settled *whether*: 6× over Tor, turning a 17-minute
   first-wallet-connect into ~84 s. It needs changes in two codebases — electrs issuing concurrent
   `getblock` calls, and the proxy spending `max_peer_concurrency` across *different* blocks instead
   of racing three peers for the same one. Open: how to make electrs's single-threaded sync loop
   issue them without restructuring, and whether Start9 would take the proxy change.
5. **StartOS packaging (Phase 8).** Untouched so far. Known: remove the `prune: 0` autoconfig task in
   `dependencies.ts`, keep `peer-local` for P2P, no `txindex`. Unknown: how to present a
   possibly-very-long first index in the UI, and whether to surface `skip_block_download_wait`.
6. **Upstreamability.** The electrs change is ~200 lines across two patches at one call site, inert
   on archival nodes. Worth proposing to `romanz/electrs`. The three proxy patches should go to
   Start9 regardless — 0002 and 0003 are live defects on every pruned StartOS node.

---

## 12. Next experiments

**E9 — prune-frontier timing during IBD.** The last undemonstrated piece of the recommended
architecture, and now the top priority. Run electrs alongside a bitcoind doing IBD with a realistic
`prune` target, and measure the margin between electrs's indexing height and the prune frontier.
Everything else assumes this works.

**E11 — build the `.s9pk`.** Phase 8 is scaffolded (`packaging/pruned-electrs-startos`) but nothing
has been built: it needs the `start-cli` toolchain, which is not installed here. Until it builds, the
packaging is unverified.

**E12 — concurrent fetching.** §7d justifies it (6× over Tor, 17 min → 84 s for a first wallet
connect). Needs a design for issuing concurrent `getblock` calls from electrs's single-threaded sync
loop, and a matching proxy change. Worth doing after E9 and E11.

E9 is local. None of these needs the Start9 test server, which is archival (900 GB, no `prune=`) and
so is not running the proxy at all — pruning it to obtain a measurement would destroy ~890 GB of the
user's chain, which is why §7a/§7c/§7d were measured directly against public peers.

---

## Appendix — reproducing the measurements

`-fastprune` is what makes regtest pruning practical throughout — it shrinks block files to ~16 kB
so `pruneblockchain` has whole files to discard. Without it a regtest chain lives in one file and
nothing is ever pruned.

### Single-node P2P probes (§4.3, §4.4) — [spikes/p2p-pruned/](spikes/p2p-pruned/)

```bash
bitcoind -datadir=$PWD/data -conf=$PWD/bitcoin.conf -fastprune -daemon
bitcoin-cli … generatetoaddress 800 <addr>
bitcoin-cli … pruneblockchain 500          # → pruneheight 251
python3 p2p_probe.py 18445 <blockhash>     # whitebind: noban + download
python3 p2p_probe.py 18444 <blockhash>     # plain bind: no permissions
```

`0001-diagnostic-drop-prune-guard.patch` there is a **diagnostic only** — the two-line change used
to prove the guard hides a hang (§2). It is deliberately kept outside any production tree.

### Two-node harness with proxy (§7, §9a) — [spikes/harness/](spikes/harness/)

```bash
./setup.sh up            # node A archival + node B pruned + patched proxy
python3 bench.py 100     # per-request cost of the three fetch paths
# …start electrs against it, then:
python3 query.py         # Electrum methods at a pruned height, vs the archival node
./setup.sh clean
```

Requires the three proxy patches applied to `vendor/btc-rpc-proxy` and the electrs patch applied to
`vendor/electrs`:

```bash
git -C vendor/btc-rpc-proxy am ../../spikes/proxy-regtest/*.patch
git -C vendor/electrs        am ../../patches/*.patch
```

### Tree state

`vendor/` holds reference checkouts and is gitignored. Both `vendor/electrs` and
`vendor/btc-rpc-proxy` currently carry the patches above as local commits on top of their pinned
upstream bases (`35216c6` and `1e9a625`); the exported patch files are the source of truth.
