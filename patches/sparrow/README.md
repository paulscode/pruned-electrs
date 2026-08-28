# A Sparrow that can follow the BLAKE2b chain

Two patches, against two repositories, plus a build script. They implement the
client half of [`docs/electrum-header-v2.md`](../../docs/electrum-header-v2.md);
the server half is in this repo's own patch set, as `patches/0006`.

```bash
./build.sh                     # clones, patches and builds into ../sparrow-blake2b
cd ../../../sparrow-blake2b && ./sparrow -n testnet4
```

Sparrow needs **Java 25 or newer**. Its releases are built with Temurin 25.0.2+10.
Anything at 21 or below fails to compile drongo, which uses unnamed lambda
parameters: a preview feature there, final in 22. Distributions are still
shipping 21 as their newest, so this usually means an unpacked Temurin;
`build.sh` finds one under `~/bin/jdk-25*` or `~/jdk-25*` without being told,
and `JAVA_HOME` overrides it.

The tree defaults to a sibling of the repository rather than `/tmp`. It is about
1.4 GB and costs a clone plus a full build to recreate, which is not something to
lose to a reboot in the middle of a test session.

Run it with `./sparrow`, not `build/image/bin/sparrow`. `./gradlew jlink`
produces an image whose launcher looks for `bin/java` beside itself and does not
put one there, so that launcher exits with `build/image/bin/java: not found`.
`./sparrow` goes through `./gradlew run` and uses the compiled classes directly,
which also means it cannot go stale: `./gradlew build` refreshes `build/libs` but
not the jlink jars, so a rebuild that skips `jlink` leaves that image behind.

## What was wrong

Unpatched Sparrow connects to a server on this chain and then cannot sync, in a
way that reports itself badly. Measured against a live server:

- `server.version` negotiates fine, so nothing looks wrong at the protocol level
- headers past the activation are 164 bytes where it expects 80
- `blockchain.block.headers` returns one concatenated blob below protocol 1.6,
  and unpatched Sparrow negotiates 1.4.2, so it always gets that form. A
  40-header response spanning the activation is 4964 bytes where it expects
  3200, and `checkBlockHeaders` stops there with "contains 4964 bytes for 40
  headers". That reads as a server fault, so the sync stalls and the message
  points away from the cause
- the single-header methods have no such check, and there a v2 header's first 80
  bytes are byte-for-byte a v1 header, so parsing *succeeds* and yields
  something plausible. Only the hash disagrees, so the chain fails to link with
  nothing saying why

## What the patches do

| | repo | contents |
|---|---|---|
| `0001` | drongo | `BlockHeaderV2`: the extra fields and the staged BLAKE2b hash. `BlockHeader` reads bit 31 for the length and delegates |
| `0002` | sparrow | `VariableHeaders`: walk a mixed run, read either response form, size the on-disk record. The call sites, the response check, the store stride, and protocol 1.8 |

Each confines its changes to one new class plus small call-site edits, so
replacing them when upstream lands should be a deletion and a few reverts rather
than a merge.

Two things `0002` has to do that are not obvious from the format alone.

`checkBlockHeaders` in `ElectrumServerRpc` applied the 80-byte stride to the
whole response, one layer above the three call sites that split it, so a chain
carrying 164-byte headers was refused before anything that could read them was
reached. It now counts the run rather than dividing it, walking the hex without
decoding it so the check stays as cheap as it was.

And `blockchain.block.headers` has two response forms. Below protocol 1.6 it is
one concatenated hex string; at 1.6 and above it is a list. Raising the
negotiated maximum to 1.8 changes which one arrives from a server that caps
between: Fulcrum 2.1.2 answers 1.4.2 with the string and 1.6 with the list.
Reading only the string would have broken header sync against a Fulcrum server
on every network, mainnet included. Both are read.

A header marked v2 is refused on a network that cannot carry one, so mainnet is
exactly as strict as it was before variable lengths existed.

Two things that could have been obstacles and were not. Sparrow's compiled-in
testnet4 checkpoints pin 74 retarget periods, so height **149183** — 354 blocks
below the activation and on shared history, so they need no change. And
BouncyCastle is already a dependency and provides `Blake2bDigest`, so the hash
needed no new library.

The on-disk store indexes by height times a fixed stride, which a variable
record length would break. The stride is now per network rather than per header,
and an 80-byte header is padded where both can occur. Only testnet4 and regtest
can, so every other network is byte-for-byte as before and existing mainnet
stores are untouched.

## Verification

**The hash, against two independent oracles.** Knots' five published vectors,
covering all four ASIC profiles, both time-offset settings and null and non-null
XOR keys, prove the port faithful to the C++. Two headers off the live testnet4
chain prove the reading of the format correct, which the vectors cannot since
they are generated by the same code this mirrors: 149538 chains onto 149537, so
a wrong hash cannot also satisfy the next header's `prevBlockHash`.

**No regressions.** 542 drongo, 248 Sparrow and 3 lark tests pass. The Sparrow count
includes upstream's `TransactionProofTest` and `WalletFormTest`, added in `12d45a8c`,
which verify inclusion proofs for newly confirmed transactions. That feature fetches
headers through `getBlockHeadersChunks`, so it reaches `checkBlockHeaders` and would
refuse every range on this chain without the fix below. A v1
header is asserted untouched: same length, same SHA256d hash, no v2 state.

**The response path a running Sparrow uses.** `BlockHeadersResponseTest` carries
a `blockchain.block.headers` response from the JSON-RPC call through
deserialization and the checks to the split, in both response forms, using the
real testnet4 headers 149536 and 149537: the last v1 header and the first v2
one. It asserts both hashes, including the BLAKE2b one, and that 149537 names
149536 as its parent, so a wrong hash cannot pass. Restoring the fixed stride
fails it, which is what makes it a regression test rather than a description of
the code.

**The whole path, against a live server.** `Blake2bChainSyncHarness` is a `main`
rather than a test, since it needs one. It drives `SimpleElectrumServerRpc` over
a real transport rather than speaking JSON-RPC itself. Against electrs on a
regtest chain activating at height 20:

```
negotiated : [electrs/0.11.1, 1.8]
anchor     : height 0, 80 bytes, hash 0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206
response   : concatenated, 4964 bytes for 40 headers (a fixed 80-byte stride would read 62)
parsed     : 40 headers, 19 v1 and 21 v2
verified   : 40 headers accepted, tip height 40
```

That last line is `HeaderChainState.add()` — Sparrow's own verification, with
linkage, the difficulty rule, proof of work and median time past — accepting a
chain that spans the activation. Restoring the fixed stride stops it at
`checkBlockHeaders`, three lines earlier.

An earlier version of this harness spoke JSON-RPC over a raw socket and called
the split directly. It reported the same success while the client itself could
not sync at all, because everything it bypassed was where the failures were. A
harness that reimplements the client tests the reimplementation, which is worth
remembering next time one looks cheaper to write.

```bash
# from the built tree, with a server running
CP="build/classes/java/test:build/classes/java/main:build/resources/main:$(ls build/jlinkbase/jlinkjars/*.jar | tr '\n' ':')"
java -cp "$CP" com.sparrowwallet.sparrow.net.Blake2bChainSyncHarness \
    127.0.0.1 50001 regtest 0 40
```

**The live chain, from Sparrow's own checkpoint.** A regtest chain has a
difficulty of one, timestamps a second apart and an activation height chosen to
be convenient, so it cannot fail the rules it is asked about.
`Blake2bLiveHeadersHarness` uses real data instead, and nothing about it is
chosen: the headers come off a fork peer over p2p, and the anchor is the last
checkpoint Sparrow already ships for testnet4.

```
anchor     : Sparrow's last compiled-in testnet4 checkpoint, height 149183, hash 00000000007899e1a91526d0258948b023f565a35fe0b0d2f9c898a39906495b
source     : 76.249.147.146:48333 (/Satoshi:29.4.1/Knots:20260508rc2/)
checked    : 68748 bytes accepted for 600 headers (a fixed 80-byte stride would read 859)
parsed     : 600 headers, 353 v1 and 247 v2
activation : first v2 header at height 149537
verified   : 600 headers accepted, tip height 149783
```

The activation height is the one thing here worth pausing on: 149537 is what
Knots documents, and the harness is told nothing about it. It falls out of the
first header whose version has bit 31 set.

```bash
# fetch the headers, then run them through Sparrow's verification
cd spikes/blake2b-testnet4
./fetch_fork_headers.py "$(curl -s https://mempool.guide/testnet4/api/block-height/149183)" --limit 600
java -cp "$CP" com.sparrowwallet.sparrow.net.Blake2bLiveHeadersHarness fork-headers.json
```

## Not verified

**Nothing has driven the GUI.** The response path, the header path and the
verification rules are all covered above, on live data, but wallet sync, address
discovery and transaction display on this chain have not been tried. That needs
a person in front of the application, and it is what the build script exists
for.

**No full sync of the live chain has been run either.** The live harness covers
600 headers around the activation, not 170,000 from genesis, and it runs against
a p2p peer rather than an Electrum server on that chain. What is untested is
volume and the store: repeated `blockchain.block.headers` calls, the on-disk
records accumulating at the wider stride, and a reorg across the activation.

**Nothing reads `blake2b_fork`.** electrs reports the fork point in
`server.features`, which is the field the proposal adds for chain identity
because `genesis_hash` cannot carry it: this chain and ordinary testnet4 share a
genesis block. No check here uses it, so pointing this build at an ordinary
testnet4 server rather than a fork one is silent. It will sync, because a chain
of 80-byte headers is one this build reads perfectly well; it will just be the
other chain.

**`supportsV2` covers all of testnet4 and all of regtest**, forked or not,
because a wallet cannot tell them apart before connecting. The visible effect is
the store stride: this build writes 164-byte padded records there where an
unpatched one writes 80. Both are self-consistent and a store with the wrong
stride is discarded and re-downloaded, so the cost is one re-sync when switching
builds. Mainnet, testnet3 and signet are byte-for-byte unchanged, and a header
marked v2 on one of them is refused.

**`cp_height` checkpoint proofs are untouched.** The proposal says leaves at or
above the activation height must be BLAKE2b, but electrs implements no
`cp_height` at all, so there is nothing on the server to verify against and
nothing here that needs to. A client talking to Fulcrum or ElectrumX on such a
chain would need it.

## Verified on the live chain, 2026-08-27

Until now this build was proven end to end only on regtest, with a chain activating at
height 20. That was the honest limit and it is now closed: a patched build has run
against the live BLAKE2b testnet4, with a wallet spending, receiving, and crediting
coinbase rewards mined by `datum-blake2b` on the same chain.

The evidence is Sparrow's own on-disk header store, `~/.sparrow/testnet4/headers/149184`,
which is the artifact `HeaderStore` writes and later reads back:

```
22503 records at a 164-byte stride, anchored at 149184, so the tip is 171686
  h149184  version 0x23f2c000  bit 31 clear  v1
  h149536  version 0x2d7ac000  bit 31 clear  v1
  h149537  version 0xa0000000  bit 31 SET    v2   <- the compiled activation height
  h149538  version 0xa0000000  bit 31 SET    v2
  h171686  version 0xa0000000  bit 31 SET    v2
```

The stride is the first half of the proof: 3690492 bytes divides by 164 and not by 80, so
these are the padded v2 records described above rather than an unpatched store. The
transition falls exactly at 149537 rather than near it.

The second half is that the stored bytes are the real chain. Feeding three records
straight from the store into `spikes/electrum-probe/headerv2_hash.py` reproduces the block
hashes recorded independently in `spikes/blake2b-testnet4/`:

```
h149537 -> 000000000068f60429c933dc0c8befbcc7edadb1cf8f8d0d7804c608fd736d82
h149538 -> 000000000008b8d8f1ce043359100c971e3e0db6bb8ae8ac8618f554564d9177
h170000 -> 00000000000003e3b861c431483954fc8feb0107d7ea110826578ae69389b2a8
```

So the client stored v2 headers, at the right stride, from the right chain, and they hash
correctly under that chain's own proof-of-work. Note this is a check of what was stored,
not a replay of `HeaderChainState.add()`; the regtest run above is still what exercises
the linkage, difficulty and median-time-past rules directly.

**Which revision.** The build that ran this was Sparrow `74060d14` plus these two patches.
That was neither what `build.sh` pinned at the time (`d8ea4264`) nor what it pinned next
(`624f999e`): the tree had been built before the pin moved, so for a while the pin named a
revision nobody had run. That is closed, and has been kept closed since.

`build.sh` now pins **tag `2.5.4`** (`8871f4f1`), a Sparrow release rather than a commit off
master, with drongo at its matching pin `080cf3f`. Both patches were checked against it with
`git apply --check` before the pin moved, then the tree was rebuilt there and the three header
suites re-run: `BlockHeaderV2Test` 4, `HeaderStoreTest` 12, `BlockHeadersResponseTest` 7, all
passing, read from the XML reports rather than the console. Same counts as at `624f999e`.

Worth keeping straight: the live evidence above was gathered at `74060d14`, and what is
verified at `2.5.4` is that the patches apply, compile and pass their tests. The commits
between the two touch none of the thirteen files these patches change, which is why the gap is
small, but it is not zero and a re-run against the live chain is what would make it zero. That
re-run is also owed for a second reason now: the chain it was gathered on no longer exists, the
public BLAKE2b testnet4 having restarted on a later Knots release candidate that moved the fork
from height 149537 to 150027.
