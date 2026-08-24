# ARCHITECTURE

How `electrs` should obtain block data when bitcoind is pruned. Evidence is in
[DISCOVERY.md](DISCOVERY.md); this document only draws conclusions from it.

Status: **recommendation made, implemented, working on regtest, and scaffolded as a StartOS
package.** Fetch latency is measured on clearnet ([§7a](DISCOVERY.md)), under concurrency
([§7c](DISCOVERY.md)) and over Tor ([§7d](DISCOVERY.md)). Together they settle the open question: the
RPC path cannot carry a cold bootstrap on any network, so the hybrid needs Architecture B as its
bootstrap arm. The one piece of the recommendation still undemonstrated is the prune-frontier margin
during IBD.

---

## The constraint everything follows from

electrs stores 12 bytes per index row — an 8-byte hash prefix and a height. No transactions, no
txids, no positions, no merkle data. So a query does not read an answer out of the index; it reads
*candidate heights* out of the index and then **re-downloads and re-scans the whole block** to
recover the data and discard prefix collisions.

That is not a bootstrap cost. It is the steady-state query path, forever:

```
blockchain.scripthash.get_history(h)
   → index: prefix(h)  → [height…]            (12-byte rows)
   → chain: height     → [blockhash…]         (in-memory headers)
   → for_blocks(blockhashes)                  ◀── whole blocks, over and over
   → scan each block for outputs paying h
```

**Therefore: any design that only solves indexing is not a solution.** Historical blocks must stay
*retrievable* for as long as the server runs. This single fact decides the comparison below.

---

## Architecture A — historical retrieval over RPC

```
       electrs
          │  getblock <hash> 0
          ▼
   btc-rpc-proxy ──── block still local? ──▶ bitcoind
          │                                    │
          └──── pruned ──▶ fetch from peers ◀───┘ (peer list via getpeerinfo)
```

**Works with an already-pruned node**, needs no bitcoind restart, no IBD, no lifecycle coordination,
and no race against the prune frontier. electrs and bitcoind stay independent StartOS packages.

Measured cost, loopback, ~250-byte blocks ([DISCOVERY.md §7](DISCOVERY.md)):

| path | blocks/s | median |
|---|---|---|
| retained, direct to bitcoind | 2588 | 0.38 ms |
| retained, through proxy | 2106 | 0.46 ms |
| pruned, peer fetch (after `TCP_NODELAY` fix) | 1692 | 0.58 ms |
| pruned, peer fetch (before) | 23.7 | 42.03 ms |

The proxy's overhead for blocks bitcoind still holds is ~20% of a sub-millisecond call — negligible,
because `fetch_block_raw` tries the local node first. The interesting number is the last row, and
what it shows is that the fetch path was dominated by a fixed ~40 ms stall that had nothing to do
with the network. With that gone, per-request overhead is no longer the bottleneck on loopback.

**Now measured against real mainnet peers** ([DISCOVERY.md §7a](DISCOVERY.md)): **162 ms per block,
median, and flat in block size** — 0.25 MB and 1.92 MB cost the same within 13%, because the fetch
is round-trip-bound rather than bandwidth-bound.

That verdict is two-sided:

- **Cold bootstrap: no.** 900k blocks × 162 ms ≈ **40.5 hours** sequentially from one peer — and
  that is the *patched* figure; unpatched it is 136 hours, and over Tor it is **22 days**.
  Architecture A cannot be the bootstrap path on any network.
- **Queries and repair: yes.** A fresh wallet touching 500 historical blocks costs ~81 s
  sequentially on clearnet — and ~17 minutes over Tor, which falls to ~84 s with 8-way concurrency.
  That is the workload this path actually has to carry, and it carries it.

Concurrency was the obvious escape, and it has now been measured
([DISCOVERY.md §7c](DISCOVERY.md)): a work queue drained by 12 peers reaches 9.7–13.2 blocks/s,
against 0.63–1.3 from the single baseline peer in the same run. That reads as 10–15×, but the honest
comparison is against a *good* single peer — §7a's 6.2 blocks/s — and on that basis it is about
**2×**, or a ~19 h bootstrap. Once several fetches are in flight the aggregate is bandwidth-bound,
and the per-request flatness stops mattering.

So concurrency does not promote Architecture A to a bootstrap path. On clearnet it is worth ~2× and
is optional.

**Over Tor it is not optional** ([DISCOVERY.md §7d](DISCOVERY.md)). Onion peers cost **2118 ms per
block against 162 ms on clearnet — 13× — and a sequential full chain is 22 days.** But concurrency
helps *more* there, because Tor is heavily round-trip-bound and parallel circuits mask latency
rather than contending for bandwidth: 6.08× at N=8, which turns a 500-block first-wallet-connect
from ~17 minutes into ~84 s. For the `onlynet=onion` users who are a large share of StartOS, that is
the difference between usable and not.

It still requires changes in **both** codebases — electrs issuing concurrent `getblock` calls, and
the proxy spending `max_peer_concurrency` across different blocks instead of racing three peers for
the same one.

## Architecture B — stream during bitcoind's IBD

```
   peers ──▶ bitcoind ──validated block──▶ electrs ──▶ index
                  │
                  └── prunes later
```

Efficient — sequential download, blocks fetched once, bitcoind's normal P2P does the work. And
cheaper to enable than the brief assumed: **no upstream change is needed to run during IBD**,
because `skip_block_download_wait` already exists ([DISCOVERY.md §3](DISCOVERY.md)).

But it fails on the constraint above. Even a perfectly executed IBD stream leaves electrs unable to
answer `scripthash.get_history` for any scripthash a client has not already subscribed to, because
that requires re-reading old blocks that bitcoind has since discarded. It also cannot help a user
who installs electrs onto an already-pruned node — the common case on StartOS — and it introduces a
lifecycle race: a stalled electrs falls behind the prune frontier and cannot recover.

**Architecture B is an optimisation, not an architecture.** It can only ever be the fast path for
*initial* indexing, sitting on top of something that solves retrieval.

## Recommendation — hybrid, split on `pruneheight`

Route per block, deterministically, on `getblockchaininfo.pruneheight`:

```
   for_blocks([h₁ … hₙ])
        │
        ├── height ≥ pruneheight ──▶ existing p2p path   (batched getdata, streaming, fast)
        │
        └── height <  pruneheight ──▶ getblock <hash> 0  (proxy: local, else peers)
```

Why this split and not something adaptive: **there is nothing to adapt to.** bitcoind answers a
`getdata` for a pruned block with *silence* — no block, no `notfound`, no disconnect — and
`Connection::for_blocks` consumes replies positionally on an untimed blocking `recv`. A batch below
the prune height hangs forever; one straddling it fails with a misleading `got unexpected block`
([DISCOVERY.md §4.3](DISCOVERY.md)). So a reactive "detect the error and fall back" design, which the
brief anticipated, is not available. The decision has to be made *before* asking — and `pruneheight`
makes it exactly, with no probing, no timeouts and no heuristics: bitcoind serves every block at or
above it and none below ([DISCOVERY.md §4.4](DISCOVERY.md)).

This satisfies the brief's principles directly:

- **Modify electrs, not Bitcoin.** No consensus software is touched.
- **Standard interfaces only.** `getblockchaininfo`, `getblockheader`, `getblock <hash> 0`.
- **Reuse Start9's proxy** rather than reimplementing peer fetching inside electrs.
- **Don't store the blockchain.** Storage model is unchanged.
- **Pruning code stays localized.** Two patches, one call site, ~200 lines, gated on a single flag.
- **Archival behaviour preserved.** `if !self.pruned` returns the upstream path byte-for-byte.
- **Easy to rebase.** The change is additive around an existing function body.

And it covers the query path for free: because the split lives in `Daemon::for_blocks`, all four
callers — indexing, both scripthash syncs, and `lookup_transaction` — are fixed by one change.

### Architecture B is required after all — as the bootstrap arm

§7a rules out proxy-only bootstrap, so B is no longer optional. The two compose rather than compete:

- **Bootstrap:** run electrs alongside bitcoind's IBD (`skip_block_download_wait`, already upstream),
  so blocks are indexed from the fast batched P2P path before they are ever pruned.
- **Serve, and repair:** everything afterwards through the `pruneheight` split.

Crucially, this needs **no lifecycle coordination**, and that is now measured rather than hoped.
[DISCOVERY.md §7b](DISCOVERY.md): with electrs stopped, bitcoind advanced 400 blocks and pruned past
where electrs left off; on restart electrs repaired the entire gap, 300+ blocks of it below the new
prune height, with no special handling. So B falling behind is **recoverable, not fatal** — which is
exactly the brief's "prefer recoverability over assumptions about startup ordering", and the reason
the hybrid is the right base whichever way the concurrency question goes.

The remaining case the hybrid does not make fast is a user installing electrs onto a long-established
pruned node with no local history. That user pays the 40.5 h (or, with concurrency, perhaps ~2.5 h)
one-time cost. It works — which is more than upstream offers — but it should be signposted in the
StartOS package rather than discovered.

---

## Trust model

Unchanged or slightly improved. Worth stating explicitly because the data now comes from a different
place.

| | validates |
|---|---|
| electrs, own P2P (`for_blocks`) | block hash only — it trusts bitcoind |
| btc-rpc-proxy, peer fetch | block hash **+ merkle root + witness commitment** |
| bitcoind | full consensus validation, for blocks it still has |

The requested hash always comes from the local node's own validated header chain, and a header
commits to the merkle root, so a peer can substitute nothing — it can only fail to answer. Routing a
fetch through the proxy is therefore *stricter* than electrs' existing P2P path, not weaker.

One caveat found and fixed: the proxy's witness-commitment check was vacuous against a peer that
strips witnesses, because it short-circuits to true when no transaction carries a witness. A peer
serving `MSG_BLOCK` did exactly that, and the proxy passed the stripped block through
([DISCOVERY.md §7](DISCOVERY.md), patch 0002). Omission, not forgery — but silent omission.

---

## Known gaps in the recommendation

1. **`blockchain.transaction.get verbose=true`** fails on pruned blocks. Its second leg is
   `getrawtransaction`, which the proxy does not intercept. Fix by reconstructing the verbose form
   from the raw block electrs can already fetch, or by extending the proxy. Confirmed failing, and
   now the only known functional gap.
2. **No concurrency**, which §7d shows is the difference between a 17-minute and an 84-second first
   wallet connect for a Tor-only node. The largest outstanding improvement.
3. **Per-batch RPC overhead.** The split costs one `getblockchaininfo` plus one batched
   `getblockheader` per batch on pruned nodes. Avoidable by threading heights down from callers
   (which already know them) instead of resolving them — deliberately not done, because it touches
   four call sites and is dwarfed by the 162 ms fetch cost it sits next to.
4. **No cache.** The proxy caches nothing, so repeated bursts re-fetch the same blocks — at ~2 s
   each on Tor. A bounded cache is worth considering, and must stay bounded or it eats the disk
   saving that motivates the project.
5. **Prune-frontier timing during IBD is still undemonstrated.** Architecture B is now load-bearing,
   and this is the one piece of it not yet measured.

Resolved since the first draft: **a failed fetch during indexing no longer kills electrs.** Patch
0002 adds retry with exponential backoff, and — importantly — a *different budget per caller*, since
`handle_events` and `rpc.sync()` share a thread and a single long budget meant one query against a
downed proxy froze the whole server for five minutes. Indexing waits 300 s, serving waits 10 s.

---

## Repository organisation

**Recommendation: two repositories**, matching how `electrs-startos` already works.

1. `paulscode/pruned-electrs` — the electrs fork, or better, a thin patch set over a pinned upstream
   submodule.
2. a StartOS packaging repo, consuming the above.

Rationale: `electrs-startos` already carries upstream deltas as `patches/*.patch` applied at build
time with `patch -p1 --fuzz=0`, each documented with the condition that retires it. That discipline
is exactly right here and is worth adopting rather than replacing — a hard fork of electrs would
make every upstream bump a merge, whereas a patch that fails to apply loudly is a feature. The
current single change is ~90 lines at one call site, well within what a patch set handles.

The three btc-rpc-proxy patches should be filed upstream with Start9 independently of this project.
0002 (witness stripping) and 0003 (Nagle stall) are live defects affecting every pruned StartOS node
today, and neither is specific to electrs.
