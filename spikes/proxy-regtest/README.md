# btc-rpc-proxy patches

Three deltas to [`Start9Labs/btc-rpc-proxy`](https://github.com/Start9Labs/btc-rpc-proxy) at
`1e9a625` (v0.5.1), found while building an electrs variant that indexes from a pruned node through
it.

**Merged upstream and released as
[v0.6.0](https://github.com/Start9Labs/btc-rpc-proxy/releases/tag/v0.6.0)** on 2026-08-25, as
[PR #29](https://github.com/Start9Labs/btc-rpc-proxy/pull/29). These patch files are kept for the
record and for reproducing the measurements against `1e9a625`; against v0.6.0 or later they are
already applied and the harness needs nothing. The image is
`ghcr.io/start9labs/btc-rpc-proxy:v0.6.0`.

Note the SHAs changed: `master` there requires signed commits, so the branch was re-signed before
merging. Authorship is intact, but a local `fix/peer-fetch-correctness` should be reset to the
merged commits rather than rebased onto them.

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

Left for a separate PR, deliberately: deriving the network from `getblockchaininfo` instead of
configuring it. Better shape, since it removes a knob whose only failure mode is silent, but it adds
a startup dependency on bitcoind's RPC that does not belong in a correctness release.

They are a **sequence** — `git apply --check` on all three at once fails, because it tests each
against the unpatched base. Apply in order:

```bash
git -C vendor/btc-rpc-proxy am ../../spikes/proxy-regtest/*.patch
```

Measurements behind each are in [`../mainnet-fetch/RESULTS.md`](../mainnet-fetch/RESULTS.md) and
`DISCOVERY.md` §7, §7a, §7c, §7d.
