# electrs-pruned

Making [`romanz/electrs`](https://github.com/romanz/electrs) serve a normal Electrum interface from
a **pruned** Bitcoin Core or Bitcoin Knots node, packaged for StartOS 0.4.0.x.

Upstream electrs refuses to start against a pruned node. Removing that check does not help — it
hides a silent, permanent hang. This project routes blocks bitcoind no longer holds to
`getblock <hash> 0`, which Start9's `btc-rpc-proxy` satisfies from peers.

There is a second track. The BLAKE2b hard fork ([Knots PR #359](https://github.com/bitcoinknots/bitcoin/pull/359))
changes the block header to a 164-byte format with a staged BLAKE2b hash, and it is live on
testnet4 from height 150027. (That height is not fixed: it is re-cut with every Knots release
candidate, and the chain restarts under it. rc2 forked at 149537, rc3 at 150027.) Both changes are
consensus, so on that chain electrs cannot currently
parse, hash or serve a single header. Supporting it is the point at which running two nodes side by
side, one pruned, becomes worth the trouble — which is what the pruning track is for.

**Status: the whole wallet stack ran end to end on a live BLAKE2b testnet4**, the rc2 chain, and has
not been re-run since that network restarted (see the next paragraph). electrs indexed all 170,086
blocks, crossed the activation at 149537, and followed new blocks as they arrived; headers across the
activation, transactions in a v2 block and recomputed merkle proofs all matched
`mempool.guide/testnet4` when that was checked on 2026-08-24. The pruning track is complete and
proven on regtest. The Electrum protocol surface (the [1.8 proposal](docs/electrum-header-v2.md)) is
implemented on both sides: electrs serves it, and a patched Sparrow follows the chain across the
activation, **verified against a live chain rather than only regtest** (see below). Nothing in
either implementation is tied to a particular activation height, so none of this is expected to have
regressed; it is simply unverified against the chain running today. Upstream adoption is the piece
that still needs other people.

**The chain those checks ran against no longer exists, and this was misread once.** On 2026-08-27
`mempool.guide/testnet4` began reporting a different block at height 149537 (`0000000000871854…`,
against `000000000068f604…`), and the note here concluded the explorer had drifted to ordinary
testnet4. It had not. The **network restarted** on release candidate rc3, which moved the fork from
149537 to 150027, and the explorer followed it. Checked 2026-08-28: mempool.guide serves an 80-byte
header at 149537 and a **164-byte** header at 150027, which no ordinary testnet4 node has. It is
still a good oracle; it is just a different chain.

So the 2026-08-24 comparisons above stand as a record of what the software did on the rc2 chain, and
nothing more. They have not been redone against rc3. The header hashes recorded in
[spikes/blake2b-testnet4/](spikes/blake2b-testnet4/) belong to the rc2 chain and are now historical.
What does carry over is everything not tied to a particular chain: the format is unchanged between
rc2 and rc3, and the vectors the implementation is tested against come from Knots'
`block_header_v2.json`, not from either chain.

## Documents

- **[docs/electrum-header-v2.md](docs/electrum-header-v2.md)** — draft proposal for
  carrying variable-length (BLAKE2b header v2) block headers over the Electrum protocol.
  Open for comment.
- **[DISCOVERY.md](DISCOVERY.md)** — pinned versions, how electrs actually fetches blocks and
  transactions, the Electrum method dependency table, proxy capabilities and defects, StartOS
  packaging behaviour, measurements.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — the three candidate designs, why the hybrid wins, trust
  model, known gaps, repo-organisation recommendation.

## What works today

On a two-node regtest harness (archival peer + pruned node + proxy), patched electrs indexes all 800
blocks including the 250 bitcoind has pruned, in 0.13 s, and serves `block.header`,
`transaction.get`, `transaction.get_merkle`, `transaction.id_from_pos`, `scripthash.get_history` and
`scripthash.get_balance` correctly at pruned heights.

All nine Phase 7 failure modes pass: gap repair across the prune frontier (electrs stopped, bitcoind
advances 400 blocks and prunes past it — repairs fully, so **no lifecycle coordination is needed**),
reorg, interrupted indexing, block source unavailable during indexing (retries, then completes
without a restart), and proxy outage during queries (prompt error, no freeze).

The last known functional gap, **`transaction.get` with `verbose=true`** on a pruned block, is
closed: btc-rpc-proxy now intercepts `getrawtransaction` when a blockhash is supplied, which is the
form electrs sends. Needs a proxy carrying that change.

## BLAKE2b header v2

`HeaderV2` and `AnyHeader` are implemented in [patches/0003](patches/) and tested against two
independent oracles: the five vectors Knots publishes in `block_header_v2.json`, checked stage by
stage, and four headers taken off live testnet4 (149537, then the activation block; 149538, which
chains onto it; 160500; 169000). Those four came from the rc2 chain, which has since been replaced;
the published vectors are the oracle that does not expire.

[patches/0004](patches/) wires them through `chain`, `types`, `db`, `index`, `p2p`, `status`,
`tracker` and `electrum`. Against a Knots `v29.4.1.knots20260508rc2` regtest node with activation
at height 20, electrs indexes all 132 blocks (19 v1, 113 v2), reaches the node's own
`bestblockhash`, and agrees with the node on every transaction at every height, on scripthash
history and balances to the satoshi for addresses paid in v2 blocks, and on merkle proofs that
recompute to each header's own merkle root.

The obstacle was `bitcoin_slices`, not `rust-bitcoin`: `bsl::Block::visit` reads the
transaction-count varint from a hardcoded offset 80, which on a 164-byte header lands inside
`m_nonce2`. It does not reliably error. On our fixture it reads a count of zero, returns `Ok` and
visits nothing, so a four-transaction block indexes as empty with no error anywhere.
`headerv2::visit_block_txs` replaces the few lines of `Block::visit` that assume the offset, using
that crate's own public `scan_len` and `Transaction` for everything else.

The format is self-describing: bit 31 of the version field, in the first four bytes, marks v2. So a
run of headers spanning the transition is parseable without either side knowing an activation
height, which is what makes [docs/electrum-header-v2.md](docs/electrum-header-v2.md)'s protocol 1.8
proposal workable.

Every live testnet4 header so far is ASIC profile 0, solo mined (null XOR key), and does not roll
time. The other profiles are covered only by Knots' own vectors.

**Pruning and header v2 compose.** On one regtest chain with two frontiers, BLAKE2b activating at
height 20 and the node pruned to 384, electrs indexes all 800 blocks and answers correctly at every
combination: 364 of the indexed blocks are v2 blocks below the prune height, so they came over RPC
rather than p2p. The routing needed no change, because it splits on `pruneheight` and never looks at
header format. Details and the two caveats in
[spikes/blake2b-pruned/](spikes/blake2b-pruned/).

**The fork's testnet4 is reachable**, which was not obvious: its DNS seeds return ordinary
testnet4 nodes, since the fork shares testnet4's genesis, port and magic bytes. A scan of ~14k
addresses found **18 peers on the BLAKE2b chain**, identified by asking each for the headers after
the block before activation and checking whether the answer was 164 or 80 bytes. A second
requirement is the `blake2b_headline` that chain committed to in its activation block; get it wrong
and the node rejects that block and stalls indistinguishably from having no peers. Both the height
and the headline change with each release candidate: the scan above was run against rc2 (149536,
`Totoro`), and the live chain is now rc3 (150026, `Catbus`). The technique is what carries over, not
the numbers. Tool, findings and
an end-to-end sync in [spikes/blake2b-testnet4/](spikes/blake2b-testnet4/).

**One blocker remains for pruned BLAKE2b on StartOS: `btc-rpc-proxy` cannot serve a v2 block.** It
decodes peer blocks with `rust-bitcoin`'s `Block` and checks a SHA256d `block_hash()`, and
`rust-bitcoin` refuses a 164-byte header outright. That test above used a shim in its place. Teaching
the proxy the format is separate work in a separate repo, and it is not needed for a non-pruned
BLAKE2b node.

## Privacy: what fetching blocks from peers costs

Upstream electrs has had a request to support pruned nodes open since 2022
([romanz/electrs#673](https://github.com/romanz/electrs/issues/673)). It has not been refused, but
it stalled on an objection worth answering rather than ignoring:

> **antonilol:** for queries this won't improve privacy, if you request a bunch of blocks, simply
> making an address list and intersecting them can show the query address
>
> **Kixunil:** Leaking privacy makes electrs pretty much pointless unless we query over separate Tor
> circuits which would be very slow.

The mechanism is real. A peer that serves you a block on demand learns you wanted that block, and if
the fetch was triggered by a user's query, intersecting the addresses in the fetched blocks narrows
down what was asked for.

How much of it applies here:

- **Address history does not fetch anything.** electrs indexes the whole chain up front and then
  answers `scripthash.get_history`, `get_balance` and `listunspent` from its own index. Looking up an
  address touches no peer. This is the case the objection is really about, and it does not arise.
- **The initial index is bulk, not targeted.** Fetching the whole pruned range reveals that you are
  syncing a chain, which is what initial block download reveals anyway. It says nothing about which
  addresses you care about, because at that point you have not asked about any.
- **The exposure is on-demand fetches after indexing.** A verbose `blockchain.transaction.get` below
  the prune height pulls its containing block, and so does opening an old block in an explorer built
  on this. Those are query-triggered, and the objection applies to them as written.
- **Tor is the mitigation named in the thread, and on StartOS and Umbrel it is usually already on.**
  btc-rpc-proxy fetches over the node's existing peer connections, so if those are Tor, the fetches
  are. "Very slow" is right: see [Measured](#measured), where a block fetch over Tor is about 13×
  the clearnet cost.

None of this is an argument that the tradeoff is free. It is an argument that it is narrower than
"electrs on a pruned node leaks your addresses", and that the part which does apply is bounded, is
mitigated by the transport these packages already run on, and is the price of not storing a second
copy of the chain. An archival node remains the private option, and it always will be.

## Measured

Real mainnet peers, clearnet, replicating the proxy's fetch path exactly:

| | median per block | full chain (900k, sequential) |
|---|---|---|
| patched proxy | **162 ms** | 40.5 h |
| upstream proxy | 544 ms | 136 h |

Latency is flat in block size (0.25 MB and 1.92 MB cost the same within 13%) — the fetch is
round-trip-bound.

These two runs were against different peers at different times and control for neither, so the gap
should not be attributed to any single patch. `TCP_NODELAY` in particular cannot explain it:
it changes only what the proxy writes, which is one 61-byte `getdata` per fetch whatever the block
weighs. Its effect is established on loopback, where it was the only variable: 23.7 to 1692
blocks/s.

Concurrency (work queue, 12 peers) reaches 9.7–13.2 blocks/s — about **2×** a good single peer, not
the near-linear gain the flat latency suggested, because the aggregate becomes bandwidth-bound.

**Over Tor it is 13× worse: 2118 ms/block, 22 days for a full chain sequentially.** But concurrency
helps *more* there (6× at 8 peers), because Tor is round-trip-bound and parallel circuits mask
latency — a 500-block first-wallet-connect drops from ~17 minutes to ~84 s.

So a cold bootstrap over the proxy is impractical on every network, and **indexing during bitcoind's
IBD is required**. The proxy path carries queries and gap repair, which it does comfortably.

Full numbers in [spikes/mainnet-fetch/RESULTS.md](spikes/mainnet-fetch/RESULTS.md).

## Patches

| | applies to | what |
|---|---|---|
| [patches/0001](patches/) | `romanz/electrs` @ `v0.11.1` | route blocks below `pruneheight` to RPC |
| [patches/0002](patches/) | ″ | retry pruned-block RPCs, with separate budgets for indexing vs serving |
| [patches/0003](patches/) | ″ | `HeaderV2`: the BLAKE2b 164-byte header type and its staged hash |
| [patches/0004](patches/) | ″ | wire it through chain/db/index/p2p/status so a BLAKE2b chain indexes |
| [patches/0005](patches/) | ″ | test recording that `rust-bitcoin` cannot decode a v2 block |
| [patches/0006](patches/) | ″ | Electrum protocol 1.8: negotiate it on a v2 chain, refuse below it, report the fork point |
| [patches/sparrow/](patches/sparrow/) | `sparrowwallet/sparrow` + `drongo` | the client half: read the header length, hash a v2 header with BLAKE2b, negotiate 1.8 |
| [spikes/proxy-regtest/0001](spikes/proxy-regtest/) | `Start9Labs/btc-rpc-proxy` @ `1e9a625` | configurable p2p network (was mainnet-only) |
| [spikes/proxy-regtest/0002](spikes/proxy-regtest/) | ″ | request `MSG_WITNESS_BLOCK` — fetched blocks were witness-stripped |
| [spikes/proxy-regtest/0003](spikes/proxy-regtest/) | ″ | set `TCP_NODELAY` — 23.7 to 1692 blocks/s on loopback |

Proxy patches 0002 and 0003 are live defects on every pruned StartOS node, independent of this
project. All three are submitted upstream as
[Start9Labs/btc-rpc-proxy#29](https://github.com/Start9Labs/btc-rpc-proxy/pull/29).

## Layout

```
DISCOVERY.md  ARCHITECTURE.md
patches/      electrs patch set (the deliverable)
packaging/
  electrs-pruned-startos/   the StartOS package, forked from electrs-startos
spikes/
  p2p-pruned/     single-node probes: what bitcoind does with a pruned getdata
  proxy-regtest/  btc-rpc-proxy patches
  harness/        two-node regtest harness, benchmarks, Electrum + failure-mode checks
  mainnet-fetch/  real-peer latency and concurrency benchmarks (clearnet + Tor), plus RESULTS.md
  blake2b-pruned/ pruning and BLAKE2b header v2 together, on one regtest chain
  blake2b-testnet4/ finding peers on the fork's testnet4, and the headline it commits to
  electrum-probe/ what a fixed-80-byte client does with a v2 header, and why
vendor/       reference checkouts (gitignored)
```

The packaging is a separate git repo, per the two-repo recommendation in ARCHITECTURE.md — the
electrs deltas stay as a patch set over a pinned upstream submodule so rebasing stays cheap.

## Reproducing

The proxy patches are a sequence and must be applied in order (`git apply --check` on all three at
once will fail, because it tests each against the unpatched base).

```bash
git -C vendor/btc-rpc-proxy am ../../spikes/proxy-regtest/*.patch && \
  (cd vendor/btc-rpc-proxy && cargo build --release)
git -C vendor/electrs am ../../patches/*.patch && \
  (cd vendor/electrs && LIBCLANG_PATH=/path/to/libclang cargo build --release)

cd spikes/harness && ./setup.sh up
python3 bench.py 100      # per-request cost of each fetch path
python3 query.py          # Electrum methods at a pruned height (needs electrs running)
./failure_modes.sh        # Phase 7 scenarios
./setup.sh clean
```

The mainnet benchmarks need no local node — they talk to public peers directly:

```bash
cd spikes/mainnet-fetch
python3 fetch_bench.py --heights 200000,900000 --repeat 5
python3 concurrency_bench.py --mode peers --levels 1,4,12 --blocks 36

# over Tor (needs a SOCKS proxy; onion peers are harvested via addrv2 gossip)
python3 fetch_bench.py --tor 127.0.0.1:9050 --heights 400000,900000 --repeat 4
python3 concurrency_bench.py --mode peers --tor 127.0.0.1:9050 --levels 1,4,8 --blocks 24
```

See the DISCOVERY.md appendix for details, including why `-fastprune` is needed to prune on regtest
at all.
