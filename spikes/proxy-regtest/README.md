# btc-rpc-proxy patches

Three deltas to [`Start9Labs/btc-rpc-proxy`](https://github.com/Start9Labs/btc-rpc-proxy) at
`1e9a625` (v0.5.1), found while building an electrs variant that indexes from a pruned node through
it.

**Merged upstream and released as
[v0.6.0](https://github.com/Start9Labs/btc-rpc-proxy/releases/tag/v0.6.0)** on 2026-08-25, as
[PR #29](https://github.com/Start9Labs/btc-rpc-proxy/pull/29). These patch files are kept for the
record and for reproducing the measurements against `1e9a625`; against v0.6.0 or later they are
already applied and the harness needs nothing. The current image is
`ghcr.io/start9labs/btc-rpc-proxy:v0.8.0`, which both `bitcoin-core-startos` and
`bitcoin-knots-startos` now pin; see the follow-ups below.

Note the SHAs changed: `master` there requires signed commits, so the branch was re-signed before
merging. Authorship is intact, but a local branch has to be reset to the merged commits rather than
rebased onto them. Same again for the follow-up. Both are done locally.

| | what | why it matters |
|---|---|---|
| 0001 | take p2p magic and default peer port from config | the fetcher hardcoded mainnet magic, so it could not run — or be tested — on any other network |
| 0002 | request `MSG_WITNESS_BLOCK`, not `MSG_BLOCK` | peers were serving the witness-stripped serialization, and neither existing check could see it |
| 0003 | set `TCP_NODELAY` | a Nagle stall on every fetch: 23.7 to 1692 blocks/s on loopback |

0002 and 0003 were live defects on every pruned StartOS node, independent of this project. The
witness bug dated to the original fetcher in `4b3341e` (October 2020), so pruned-block fetches had
been silently witness-stripped for five years.

## What upstream added on top

Worth reading before relying on any of the above, because two of them change behaviour this project
measured and one corrects a claim made here.

- **A peer that strips witnesses it commits to is now rejected.** 0002 fixed what an *honest* peer
  sends; it did not detect a dishonest one. `check_witness_commitment()` short-circuits to true once
  no transaction carries a witness, which is exactly the state a stripped block is in, so a stripped
  block passed every check unchanged. A commitment in the coinbase is the block's own statement that
  witness data exists, so "commits but carries none" now fails the block and the fetch moves to the
  next peer. That gap was mine to find and I did not.
- **The peer list now requires `WITNESS` alongside `NETWORK`.** A peer that cannot serve witness data
  can no longer answer a fetch, so the eligible pool is smaller than when the concurrency numbers in
  [`../mainnet-fetch/RESULTS.md`](../mainnet-fetch/RESULTS.md) were taken. Those used a
  `NETWORK`-only filter and should be re-measured before being quoted for v0.6.0.
- **`strippedsize` now comes from `Block::strippedsize()`** rather than a witness-byte subtraction,
  which 0002 made reachable.
- **The mainnet `TCP_NODELAY` table was trimmed to a note**, and rightly. `TCP_NODELAY` changes only
  what the proxy writes, which is one 61-byte `getdata` per fetch whatever the block weighs, so no
  size scaling can be attributed to it. The loopback measurement carries that commit on its own. The
  claim has been corrected here and in `DISCOVERY.md`.

## The follow-up

**Merged as [PR #30](https://github.com/Start9Labs/btc-rpc-proxy/pull/30) and released as
[v0.7.0](https://github.com/Start9Labs/btc-rpc-proxy/releases/tag/v0.7.0)** on 2026-08-26, hours
after v0.6.0. `bitcoin-core-startos` pinned v0.7.0 the same day, so that is what StartOS ships.

Deriving the network from `getblockchaininfo` was left out of the first PR deliberately, on the
grounds that it would add a startup dependency on bitcoind's RPC that did not belong in a
correctness release. It turns out it does not: `fetch_block_raw` reaches the peer path only after
`getblock` has come back pruned and `getpeerinfo` has returned a list, both through the same
client, so bitcoind has answered twice before the network is resolved behind a `OnceCell`. The
maintainer accepted that reasoning on review.

It also picks up two things the config option could not express. `testnet4` — rust-bitcoin 0.29's
`Network` has no variant for it, so mapping the chain name straight to a magic and port sidesteps
the enum, which is a precondition for A5. And **custom signets**, whose magic is the first four
bytes of the double SHA256 of the block challenge, so `network = "signet"` could only ever mean the
default one; the challenge comes back in the same response.

`network` is gone as a result, which is why the release is 0.7.0 rather than 0.6.1: it also removes
`State.magic`, `State.default_peer_port` and the softfork types from the lib API. Left in a config
file the option is ignored, since configure_me ignores unknown keys, so a stale config still starts;
passed as `--network` it is now rejected outright. It had shipped in v0.6.0 only hours earlier and no
StartOS package ever emitted the key, so nothing deployed could notice. This harness no longer sets
it.

One more defect came out of it: `GetBlockchainInfo` had been in the tree unused since before either
PR, and could not have parsed a response if called. `warnings` became an array in Core 28.0 and the
struct named it as a string, so it fails with `invalid type: sequence, expected a string` against
anything current. Narrowed to the two fields the proxy reads.

They are a **sequence** — `git apply --check` on all three at once fails, because it tests each
against the unpatched base. Apply in order:

```bash
git -C vendor/btc-rpc-proxy am ../../spikes/proxy-regtest/*.patch
```

Measurements behind each are in [`../mainnet-fetch/RESULTS.md`](../mainnet-fetch/RESULTS.md) and
`DISCOVERY.md` §7, §7a, §7c, §7d.

## v0.8.0: two more, for Mempool on a pruned node

Both merged on 2026-08-27 and released as
[v0.8.0](https://github.com/Start9Labs/btc-rpc-proxy/releases/tag/v0.8.0), which
`bitcoin-core-startos` and `bitcoin-knots-startos` both pin already.

**The companion Knots packages do not.** `knots-prerdts-startos` and `knots-rdts-startos` are still
on `v0.5.1`, which predates all four PRs: they are missing the witness-stripping fix, the Nagle
stall, the network derivation, `getrawtransaction` and the cache. Bumping both is a one-line change
and is a prerequisite for MEMPOOL-PLAN §4 working on a companion node.

**[PR #31](https://github.com/Start9Labs/btc-rpc-proxy/pull/31), `getrawtransaction` with a
blockhash.** A pruned node cannot answer a verbose transaction lookup for a block it no longer has,
which is the exact call an Electrum server makes when a wallet asks about an old transaction. The
proxy now intercepts the three-argument form, fetches the block it already knows how to fetch, pulls
the transaction out and runs it through `decoderawtransaction`, then merges in the six fields that
come from the block rather than the transaction. This is what MEMPOOL-PLAN §4 depends on.

**[PR #32](https://github.com/Start9Labs/btc-rpc-proxy/pull/32), a bounded block cache.** Nothing
remembered a peer-fetched block, so a second request for one cost the same as the first, and a single
Electrum verbose lookup fetches the same block twice by itself. A block page of ten transactions
measured 20 peer fetches cold and 20 again warm; with the cache it is 9 cold and 0 warm.
`block_cache_size_mib` bounds it at 64 MiB by default, 0 to disable. Only peer-fetched blocks are
stored, since anything bitcoind still holds is cheap to ask for again.

**The maintainer caught a real bug in #31 after merge**
([`352393f`](https://github.com/Start9Labs/btc-rpc-proxy/commit/352393f)). Core omits `time` and
`blocktime` entirely for a transaction in a block that is not on the active chain; the merged code
emitted them anyway, so a proxied answer for a reorged-out block had 15 keys where Core gives 13.
That is the one way a caller could have told a proxied answer from a direct one.

Worth recording *how* it got through, because the mistake was in the verification rather than the
code. The stale-block case **was** tested, and that test is what found the two defects fixed before
the PR went out (`confirmations` needed to be `i32`, and `in_active_chain` could not be hardcoded
true). But it checked the fields that had been changed rather than comparing the whole response
against Core's. Confirmed here afterwards on Knots 29.3: an active-chain transaction returns 15 keys,
one in an invalidated block returns 13, with both timestamps absent.

The rule that follows: when the point of a change is to be indistinguishable from the thing it
proxies, assert on the **whole shape**, not on the fields that were touched. Against v0.8.0 the
pruned-block path now compares byte-for-byte with an archival node's answer, key sets and all.
