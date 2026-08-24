# btc-rpc-proxy patches

Three deltas to [`Start9Labs/btc-rpc-proxy`](https://github.com/Start9Labs/btc-rpc-proxy) at
`1e9a625` (v0.5.1), found while building an electrs variant that indexes from a pruned node through
it.

**Submitted upstream as [Start9Labs/btc-rpc-proxy#29](https://github.com/Start9Labs/btc-rpc-proxy/pull/29).**
Until that merges, the harness in [`../harness/`](../harness/) needs them applied locally.

| | what | why it matters |
|---|---|---|
| 0001 | take p2p magic and default peer port from config | the fetcher hardcoded mainnet magic, so it could not run — or be tested — on any other network |
| 0002 | request `MSG_WITNESS_BLOCK`, not `MSG_BLOCK` | peers were serving the witness-stripped serialization, and neither existing check could see it |
| 0003 | set `TCP_NODELAY` | ~40 ms Nagle stall per fetch, compounding with block size |

0002 and 0003 are live defects on every pruned StartOS node, independent of this project.

They are a **sequence** — `git apply --check` on all three at once fails, because it tests each
against the unpatched base. Apply in order:

```bash
git -C vendor/btc-rpc-proxy am ../../spikes/proxy-regtest/*.patch
```

Measurements behind each are in [`../mainnet-fetch/RESULTS.md`](../mainnet-fetch/RESULTS.md) and
`DISCOVERY.md` §7, §7a, §7c, §7d.
