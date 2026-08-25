# pruned-electrs

Making [`romanz/electrs`](https://github.com/romanz/electrs) serve a normal Electrum interface from
a **pruned** Bitcoin Core or Bitcoin Knots node, packaged for StartOS 0.4.0.x.

Upstream electrs refuses to start against a pruned node. Removing that check does not help — it
hides a silent, permanent hang. This project routes blocks bitcoind no longer holds to
`getblock <hash> 0`, which Start9's `btc-rpc-proxy` satisfies from peers.

There is a second track. The BLAKE2b hard fork ([Knots PR #359](https://github.com/bitcoinknots/bitcoin/pull/359))
changes the block header to a 164-byte format with a staged BLAKE2b hash, and it is live on
testnet4 from height 149537. Both changes are consensus, so on that chain electrs cannot currently
parse, hash or serve a single header. Supporting it is the point at which running two nodes side by
side, one pruned, becomes worth the trouble — which is what the pruning track is for.

**Status: electrs serves the live BLAKE2b testnet4.** It indexed all 170,086 blocks, crossed the
activation at 149537, and follows new blocks as they arrive. Its tip matches `mempool.guide`, headers
across the activation are byte-identical to the explorer's, transactions in a v2 block match, and
merkle proofs recompute. The pruning track is complete and proven on regtest. The Electrum protocol
surface (the [1.8 proposal](docs/electrum-header-v2.md)) is implemented on both sides: electrs serves it,
and a patched Sparrow follows the chain across the activation. Upstream adoption is the piece that
still needs other people.

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

One known functional gap: **`transaction.get` with `verbose=true`** fails on pruned blocks — its
second leg is `getrawtransaction`, which the proxy does not intercept.

## BLAKE2b header v2

`HeaderV2` and `AnyHeader` are implemented in [patches/0003](patches/) and tested against two
independent oracles: the five vectors Knots publishes in `block_header_v2.json`, checked stage by
stage, and four headers taken off live testnet4 (149537, the activation block; 149538, which chains
onto it; 160500; 169000).

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
block 149536 and checking whether the answer was 164 or 80 bytes. A second requirement is
`blake2b_headline=Totoro`, which that chain committed to in its activation block; get it wrong and
the node rejects block 149537 and stalls indistinguishably from having no peers. Tool, findings and
an end-to-end sync in [spikes/blake2b-testnet4/](spikes/blake2b-testnet4/).

**One blocker remains for pruned BLAKE2b on StartOS: `btc-rpc-proxy` cannot serve a v2 block.** It
decodes peer blocks with `rust-bitcoin`'s `Block` and checks a SHA256d `block_hash()`, and
`rust-bitcoin` refuses a 164-byte header outright. That test above used a shim in its place. Teaching
the proxy the format is separate work in a separate repo, and it is not needed for a non-pruned
BLAKE2b node.

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
  pruned-electrs-startos/   the StartOS package, forked from electrs-startos
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
