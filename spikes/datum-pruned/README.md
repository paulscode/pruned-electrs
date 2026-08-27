# Datum Gateway against a pruned node

Closes MEMPOOL-PLAN §11.10, the one cell of the goal matrix that had no evidence behind it.

**Result: Datum works against a pruned node, and works through btc-rpc-proxy too.** Two blocks were
mined end to end on the regtest harness, and the archival node accepted both.

| | block 801 | block 802 |
|---|---|---|
| Datum's RPC target | pruned node B directly | btc-rpc-proxy, then node B |
| template produced | height 801, 1.5625 BTC | height 802 |
| share accepted | yes | yes |
| `submitblock` | "submitted to upstream node successfully" | same, via the proxy |
| node B tip | 800 → 801 | 801 → 802 |
| archival node A agrees | yes, same hash | yes, same hash |

Node A validating both independently is what makes this evidence rather than a self-report. Node B
stayed pruned throughout (`pruneheight` 251), and the proxy log shows it handled 7 `getblocktemplate`
and 3 `submitblock` calls rather than being bypassed.

This is the expected outcome: `getblocktemplate` builds from the UTXO set and the mempool, both of
which pruning leaves intact, and connecting a block at the tip needs the chainstate rather than
historical block data. Datum never asks for an old block, so it never reaches the case pruning
breaks. It does not need the proxy at all, but it works through one, which matters because StartOS
packages generally take that route.

## Why this needed a miner

Three things make driving Datum on regtest more awkward than it sounds.

**Datum rejects `bcrt1` addresses.** It logs `Could not generate output script for pool addr!` and
panics. `bc1` and `tb1` are both fine. The workaround is sound rather than a hack: the address prefix
is only a display wrapper, so the same witness program encoded under a different HRP gives a
byte-identical scriptPubKey. `bcrt1qug9gpr6xl4mz3yyfumsx29guhndt7adm62xfme` and
`bc1qug9gpr6xl4mz3yyfumsx29guhndt7admj9yhhr` both mean `0014e20a808f46fd76289089e6e065151cbcdabf75bb`,
and node B's wallet recognised the resulting coinbase as its own.

**Datum enforces the share target, not just the block target.** A share that beat regtest's trivial
block target (`0x7fffff00...`, so roughly every other hash) came back `H-not-zero`. Since
`vardiff_min` floors at 1, a valid share costs about 2^32 hashes regardless. Python manages about
18 MH/s across 20 cores, which is not enough inside the 150-second stale window, hence `miner.c`:
about 31 MH/s, or roughly 135 seconds per share.

**Three config constraints are fatal at startup** and are not visible in `--example-conf`:

- `pooled_mining_only` must be false when `pool_host` is empty
- `share_stale_seconds` may not exceed 150
- `protocol_global_timeout` must be at least `work_update_seconds` plus 5

A packaged Datum wants its defaults checked against all three.

One cosmetic thing to know when reading Datum's logs: it submits every block twice, and logs the
second attempt's `duplicate` response as `Upstream node rejected our block!`. The block is already
in by that point.

## Running it

```sh
../harness/setup.sh up                       # node A archival, node B pruned, proxy on 19013
gcc -O3 -fopenmp -Wno-deprecated-declarations -o miner miner.c -lcrypto

cp conf.json.template conf.json              # then edit:
#   bitcoind.rpccookiefile -> ../harness/nodeB/regtest/.cookie
#   bitcoind.rpcurl        -> http://127.0.0.1:19011 direct, or :19013 through the proxy
#   mining.pool_address    -> a bc1/tb1 encoding of an address your wallet owns

datum_gateway -c conf.json &
python3 grind.py
```

`grind.py` takes a job, grinds in windows sized to fit the stale limit, and retries on a fresh job
until a share lands. Expect one or two rounds. Then check both nodes agree:

```sh
bitcoin-cli -datadir=../harness/nodeB getblockhash 802
bitcoin-cli -datadir=../harness/nodeA getblockhash 802
```

## Files

- `miner.c` — grinds nonces for a stratum share across threads. Validated against `hashlib` before
  it was trusted: for a given prefix and nonce it produces the same digest Python does.
- `stratum_miner.py` — a minimal stratum v1 client. The prevhash byte order was derived empirically
  rather than from memory, by comparing what Datum sent against the tip hash the node reported: the
  wire form is the display hash's eight 4-byte words in reverse order, and reversing each word
  recovers the header's internal form.
- `grind.py` — the driver, with the retry-across-jobs loop.
- `conf.json.template` — the working Datum config, with the three constraints above already
  satisfied.
