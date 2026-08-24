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

**Status:** pruning track complete and proven on regtest; three StartOS packages built and
installed on a test server. Header v2 track: wire format and hash verified against live testnet4,
`HeaderV2` implemented ([patches/0003](patches/)), not yet wired into the index.

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
onto it; 160500; 169000). Nothing calls them yet — substituting them for `bitcoin::block::Header`
across `chain`/`index`/`status`/`electrum` is the next change.

The format is self-describing: bit 31 of the version field, in the first four bytes, marks v2. So a
run of headers spanning the transition is parseable without either side knowing an activation
height, which is what makes [docs/electrum-header-v2.md](docs/electrum-header-v2.md)'s protocol 1.8
proposal workable.

Every live testnet4 header so far is ASIC profile 0, solo mined (null XOR key), and does not roll
time. The other profiles are covered only by Knots' own vectors.

## Measured

Real mainnet peers, clearnet, replicating the proxy's fetch path exactly:

| | median per block | full chain (900k, sequential) |
|---|---|---|
| with `TCP_NODELAY` (patch 0003) | **162 ms** | 40.5 h |
| without (upstream proxy) | 544 ms | 136 h |

Latency is flat in block size (0.25 MB and 1.92 MB cost the same within 13%) — the fetch is
round-trip-bound.

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
| [spikes/proxy-regtest/0001](spikes/proxy-regtest/) | `Start9Labs/btc-rpc-proxy` @ `1e9a625` | configurable p2p network (was mainnet-only) |
| [spikes/proxy-regtest/0002](spikes/proxy-regtest/) | ″ | request `MSG_WITNESS_BLOCK` — fetched blocks were witness-stripped |
| [spikes/proxy-regtest/0003](spikes/proxy-regtest/) | ″ | set `TCP_NODELAY` — removes a ~40 ms stall per fetch (71× on loopback) |

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
