# Finding peers on the BLAKE2b testnet4

A node running the BLAKE2b fork cannot find the fork's chain by itself. This is
the tool that finds it peers, and the record of what it found on 2026-08-24.

> **The chain below is the rc2 one, and it no longer exists.** The public BLAKE2b
> testnet4 restarted on Knots release candidate rc3, which moved activation from
> 149537 to 150027. Every height and hash in this document belongs to the earlier
> chain and is kept as the record of that scan. `find_fork_peers.py` has been
> retargeted to the live chain (`FORK_HEIGHT = 150027`, last common block 150026,
> `00000000000000004f7721bb…`, taken from the fork block's own `prev_blockhash`),
> and the constants carry a note on refreshing them, because the activation height
> is re-cut with every release candidate.
>
> **Re-probed 2026-08-28 against the rc3 chain**, using the eight peers the
> `knots-blake2b` package ships. Seven answered, all seven on the BLAKE2b chain,
> all reporting `/Satoshi:29.4.1/Knots:20260508rc3/` or a variant, each serving
> 195 headers after 150026 whose first is 164 bytes. `172.117.233.59:48333` did not
> answer. So the shipped peer list survived the restart and needs no change, and
> the peers' own user agents are independent confirmation that rc3 is what is live.

## Why this is needed at all

The fork shares testnet4's genesis block, magic bytes (`1c163f28`) and default
port (48333). So a fork node and an ordinary testnet4 node connect to each other
happily, exchange addresses, and agree on every block up to 149536. From 149537
the fork's blocks carry 164-byte BLAKE2b headers, which an ordinary node rejects,
and the ordinary chain's blocks carry 80-byte SHA256d headers, which the fork
node rejects.

testnet4's DNS seeds (`seed.testnet4.bitcoin.sprovoost.nl`, `seed.testnet4.wiz.biz`)
return ordinary nodes. Measured: **33 addresses from the seeds, every one on the
SHA256d chain**, mostly `/Satoshi:30.0.0(@wiz)/` and `/Satoshi:31.0.0/`. So a
fresh fork node syncs to 149536 and stops, with peers connected and nothing
visibly wrong.

Nothing published lists fork peers. `btc-blake2b.org`'s getting-started and
developers pages carry none, and neither does bitcoinknots.org.

## The trick

The same property that causes the problem solves it. Fork nodes are *on*
testnet4's network, so they are reachable through ordinary discovery. They just
have to be told apart afterwards, and they can be told apart decisively:

> Ask for the headers following block 149536, the last block both chains have.
> A fork node answers with a 164-byte header; an ordinary node answers with an
> 80-byte one. The header's own version field says which, in its first four bytes.

The user agent is reported too but is deliberately *not* the test. A node can run
the fork build and still be following the other chain, which is exactly the
failure being diagnosed.

The locator has to be 149536 rather than 149537: an ordinary node has never heard
of the fork's 149537 and would answer from genesis instead of from the fork
point. Its hash is derived from block 149537's own `prev_blockhash` field rather
than looked up.

## What was found

**18 peers on the BLAKE2b chain**, out of ~14,000 addresses probed (209 of which
answered at all). All ran `Knots:20260508rc2`; a couple carry custom user agents
(`(BIP110 meow miao)`, `(filteroor)`), which is to say these are people's nodes,
not infrastructure.

```
47.183.246.78:18310      172.117.233.59:48333     86.8.92.221:48333
64.177.11.149:48333      173.24.24.140:48333      136.36.150.88:48333
67.163.234.38:48333      174.3.117.235:48333      136.36.150.88:48334
67.167.88.194:48333      178.118.234.189:48333    193.149.176.77:8333
76.186.131.69:48333      184.179.145.52:48333     207.81.196.105:48333
76.249.147.146:48333     76.249.147.146:52304     82.67.102.15:48333
```

Note the ports: several run on something other than 48333, including one on 8333,
mainnet's port. Do not assume the default.

Cross-checked on re-probe: **17 of the 18 answered again and every one reported
height 170062, exactly matching `mempool.guide/testnet4/api/blocks/tip/height`.**
So they agree with the explorer this project has been verifying header-v2 claims
against from the start, which is independent evidence they are on the intended
chain rather than merely on *a* BLAKE2b chain.

Two routes found them. The first peer came from noticing that `mempool.guide`
resolves to a single host and probing 48333 on it: the explorer's own node. A
breadth-first crawl out through its gossip found two more. The randomised sweep of
the full seed-plus-gossip address space found 18, so the broad sweep is worth the
wall-clock and the targeted crawl is not sufficient on its own.

**This list will rot.** Re-run the tool rather than trusting it.

## The other half of the problem: the headline

Peers alone were not enough, and this cost more time than finding them.

A node given good peers still stopped dead at 149536, logging:

```
ProcessNewBlock: AcceptBlock FAILED (bad-headline, Headline is wrong)
```

`validation.cpp:4639` applies a consensus check at the activation height, and only
there: the node's configured `blake2b_headline` must appear as a substring of that
block's coinbase `scriptSig`. testnet4's block 149537 carries **`Totoro`**
(coinbase `d1ef84d4...c17342`, scriptSig `0321480207546f746f726f00...`). Any other
value and the node rejects 149537 and everything after it.

**This is the same observable failure as having no fork peers**: synced to 149536,
peers connected, nothing obviously wrong. The two are told apart by the header
count. A node with no fork peers never learns the fork's headers, so `headers`
stops at 149536 too. A node with the wrong headline has all the headers (170,000+)
and is refusing the blocks. The package's `chain` health check now uses exactly
that distinction and names the likely cause.

## End to end

With the peers **and** `blake2b_headline=Totoro`, a fresh node synced the whole
chain and crossed the fork:

```
h149535: 80B  v1/SHA256d  0000000000d13ed0076865b8659f37589b15e54c260eb2f5202540d9a189a548
h149536: 80B  v1/SHA256d  0000000000601b1b360b505bd6d999c450fd5bc1ec48cfbcefea599b25dc1951
h149537: 164B v2/BLAKE2b  000000000068f60429c933dc0c8befbcc7edadb1cf8f8d0d7804c608fd736d82
h149538: 164B v2/BLAKE2b  000000000008b8d8f1ce043359100c971e3e0db6bb8ae8ac8618f554564d9177
h170000: 164B v2/BLAKE2b  00000000000003e3b861c431483954fc8feb0107d7ea110826578ae69389b2a8
```

Tip at 170061, hash `0000000000000dbb881c88e43e4567769c20b9ecbb4207365b5353b30306c4ac`,
**byte-identical to `mempool.guide/testnet4` at the same height**. Zero
`bad-headline` errors. h149536's hash also matches the locator this tool derives
from 149537's `prev_blockhash`, which closes that loop.

## Using it

```bash
# probe specific addresses
./find_fork_peers.py 82.67.102.15:48333

# crawl outward from known fork peers (highest yield)
./find_fork_peers.py --from-fork --rounds 3 --out fork-peers.txt 82.67.102.15:48333

# broad sweep: DNS seeds, one round of gossip, then probe everything
./find_fork_peers.py --limit 14000 --workers 64 --out fork-peers.txt
```

Dependency-free Python 3. It makes plaintext outbound connections to port 48333
on many hosts, which some networks will find interesting; it is a peer-discovery
scan, not a port scan, but it does connect to every address it is given.

## Feeding them to the node

In the StartOS package, the **Set Peers** action, one `host:port` per line. Under
the hood that is `addnodes` in `store.json`, which `entrypoint.sh` writes as
`addnode=` lines. Plain Docker:

```bash
docker run ... -e CHAIN=testnet4 \
  -e ADDNODES="82.67.102.15:48333 178.118.234.189:48333 193.149.176.77:8333" \
  knots-blake2b:...
```

Verified end to end: a fresh testnet4 node given these three connected to two of
them, took all 170,029 headers, and began downloading the fork's blocks.

## Shipped as defaults

Eight of them are now `testnet4Seeds` in the package's `utils.ts`, merged with
whatever the user sets. Without some default a new user's node stalls with no way
forward, and inbound connections on 48333 are what a public node is for.

Worth being explicit about the tradeoff, since it was a judgement call: this points
StartOS users' traffic at nodes whose operators were never asked. The load is
negligible (a handful of testers, and these are already publicly reachable), but
the list is deliberately short rather than all 18, and it is not a substitute for
re-running the tool. If any operator objects, dropping an entry is a one-line
change.

## Fetching headers from the fork

`fetch_fork_headers.py` takes a block hash and pulls the headers following it off
one of the confirmed peers, over p2p rather than from an explorer. It exists to
feed a client's verification with data nobody chose:

```bash
./fetch_fork_headers.py "$(curl -s https://mempool.guide/testnet4/api/block-height/149183)" --limit 600
```

That anchor is deliberate. 149183 is the last checkpoint Sparrow ships for
testnet4, being 74 retarget periods, and it is 354 blocks below the activation on
history the two chains share, so a run starting there spans the fork with the
real difficulty targets and the real timestamps either side of it.

Asking a *fork* peer specifically is the point. An ordinary testnet4 peer answers
the same request with the other chain, and the two agree up to 149536.

`fork-headers.json` is output, not a source file: the script rewrites it, and
`Blake2bLiveHeadersHarness` in the Sparrow patches reads it. What that harness
found is in `patches/sparrow/README.md`.
