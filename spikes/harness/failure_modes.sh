#!/usr/bin/env bash
# Phase 7 failure modes against the two-node harness.
#
# Run ./setup.sh up first. Each scenario reports PASS/FAIL on its own terms;
# a FAIL here is a finding, not a broken test.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Derived from this script's own location rather than written out, so the tree
# can be moved or the repo renamed without editing paths here.
REPO="$(cd "$ROOT/../.." && pwd)"
ELECTRS="${ELECTRS:-$REPO/vendor/electrs/target/release/electrs}"
PROXY="${PROXY:-$REPO/vendor/btc-rpc-proxy/target/release/btc_rpc_proxy}"
CLI="$HOME/bin/knots/bin/bitcoin-cli"
acli() { "$CLI" -datadir="$ROOT/nodeA" -conf="$ROOT/nodeA/bitcoin.conf" "$@"; }
bcli() { "$CLI" -datadir="$ROOT/nodeB" -conf="$ROOT/nodeB/bitcoin.conf" "$@"; }

pass() { echo "  PASS  $*"; }
fail() { echo "  FAIL  $*"; }

start_electrs() {
  nohup "$ELECTRS" --conf "$ROOT/electrs.toml" > "$ROOT/electrs.log" 2>&1 &
  echo $! > "$ROOT/electrs.pid"
}
stop_electrs() {
  [ -f "$ROOT/electrs.pid" ] && kill "$(cat "$ROOT/electrs.pid")" 2>/dev/null
  rm -f "$ROOT/electrs.pid"; sleep 2
}
wait_indexed() {  # wait_indexed <height> <timeout>
  for _ in $(seq "${2:-90}"); do
    grep -q "height=$1\b" "$ROOT/electrs.log" 2>/dev/null && return 0
    sleep 1
  done
  return 1
}
electrum() {  # electrum <method> <json-params>
  printf '{"jsonrpc":"2.0","id":1,"method":"%s","params":%s}\n' "$1" "$2" \
    | timeout 20 nc -q2 127.0.0.1 19014 2>/dev/null
}

echo "=== Scenario 4: electrs stopped while bitcoind advances past the prune frontier ==="
rm -rf "$ROOT/electrs-db"/*; start_electrs
if wait_indexed 800; then pass "initial index to 800"; else fail "initial index"; fi
stop_electrs
ADDR=$(acli getnewaddress)
acli generatetoaddress 400 "$ADDR" >/dev/null
for _ in $(seq 60); do [ "$(bcli getblockcount)" = "1200" ] && break; sleep 1; done
bcli pruneblockchain 1100 >/dev/null
NEWPH=$(bcli getblockchaininfo | jq -r .pruneheight)
echo "  advanced to 1200, node B pruneheight now $NEWPH (electrs last saw 800)"
start_electrs
if wait_indexed 1200 120; then
  pass "electrs repaired a 400-block gap, 300+ of it below the new prune height"
else
  fail "electrs did not catch up"; tail -3 "$ROOT/electrs.log"
fi

echo
echo "=== Scenario: proxy unavailable mid-operation ==="
kill "$(cat "$ROOT/proxy.pid")" 2>/dev/null; sleep 1
OUT=$(electrum blockchain.transaction.get "[\"$(acli getblock "$(acli getblockhash 100)" 1 | jq -r .tx[0])\"]")
if echo "$OUT" | grep -q '"error"'; then
  pass "query fails cleanly with an error rather than hanging"
  echo "        $(echo "$OUT" | head -c 150)"
else
  fail "unexpected: $(echo "$OUT" | head -c 150)"
fi
if kill -0 "$(cat "$ROOT/electrs.pid")" 2>/dev/null; then
  pass "electrs survived the proxy outage (did not exit)"
else
  fail "electrs died when the proxy went away"
fi
nohup "$PROXY" --conf "$ROOT/proxy.toml" -vvvv > "$ROOT/proxy.log" 2>&1 &
echo $! > "$ROOT/proxy.pid"; sleep 3
OUT=$(electrum blockchain.transaction.get "[\"$(acli getblock "$(acli getblockhash 100)" 1 | jq -r .tx[0])\"]")
if echo "$OUT" | grep -q '"result"'; then
  pass "recovers once the proxy returns, no restart needed"
else
  fail "did not recover: $(echo "$OUT" | head -c 150)"
fi

echo
echo "=== Scenario: pruned blocks unservable during indexing (peer gone) ==="
# Killing the proxy is the wrong lever here — with no RPC endpoint electrs never
# gets past Daemon::connect, which legitimately refuses to start, and on loopback
# it finishes indexing faster than the kill can land mid-build anyway.
#
# Stopping the archival peer instead reproduces the realistic failure
# deterministically: bitcoind and the proxy both stay up and answer
# getblockchaininfo/getblockheader, but node B has no peer holding the pruned
# blocks, so every pruned fetch fails with "Block not available (pruned data)".
# That is peer churn, which is the common case in the field.
stop_electrs
acli stop >/dev/null 2>&1; sleep 3
echo "  archival peer stopped; B has $(bcli getpeerinfo | jq 'length') peers"
rm -rf "$ROOT/electrs-db"/*; start_electrs; sleep 12
if kill -0 "$(cat "$ROOT/electrs.pid")" 2>/dev/null; then
  pass "electrs stayed up while pruned blocks were unservable"
else
  fail "electrs exited — indexing still treats a failed fetch as terminal"
  grep -E "Caused|Error|failing after" "$ROOT/electrs.log" | tail -4 | sed 's/^/        /'
fi
if grep -q "retrying in" "$ROOT/electrs.log"; then
  pass "retry backoff engaged ($(grep -c 'retrying in' "$ROOT/electrs.log") attempts logged)"
  grep -m1 "retrying in" "$ROOT/electrs.log" | sed 's/^/        /'
else
  fail "no retry attempts logged — is the retry wired in?"
fi
# Bring the peer back WITHOUT restarting electrs: a correct retry heals on its
# own, which is the whole point of the budget.
"$HOME/bin/knots/bin/bitcoind" -datadir="$ROOT/nodeA" -conf="$ROOT/nodeA/bitcoin.conf" -daemon >/dev/null 2>&1
for _ in $(seq 30); do acli getblockcount >/dev/null 2>&1 && break; sleep 1; done
# A restart does not reload wallets, and later scenarios need one to mine to.
acli loadwallet w >/dev/null 2>&1 || true
bcli addnode 127.0.0.1:19000 onetry >/dev/null 2>&1
TIP_NOW=$(acli getblockcount)
if wait_indexed "$TIP_NOW" 180; then
  pass "index completed WITHOUT restarting electrs — recovered by itself"
else
  fail "did not self-heal after the peer returned"; tail -3 "$ROOT/electrs.log"
fi

echo
echo "=== Scenario 7: reorg ==="
STOP_H=$(acli getblockcount)
FORK_FROM=$((STOP_H - 3))
echo "  invalidating A's chain back to $FORK_FROM and building a longer branch"
acli invalidateblock "$(acli getblockhash $((FORK_FROM + 1)))" >/dev/null
# A different address, so the regenerated branch is not byte-identical to the
# invalidated one — regtest is deterministic enough that reusing $ADDR
# reproduces the very blocks just marked invalid, and they are rejected.
FORK_ADDR=$(acli getnewaddress)
acli generatetoaddress 8 "$FORK_ADDR" >/dev/null
NEW_TIP_H=$(acli getblockcount)
for _ in $(seq 60); do [ "$(bcli getblockcount)" = "$NEW_TIP_H" ] && break; sleep 1; done
echo "  A and B now at $NEW_TIP_H"
if wait_indexed "$NEW_TIP_H" 120; then
  E_TIP=$(electrum blockchain.headers.subscribe "[]" | jq -r .result.height 2>/dev/null)
  if [ "$E_TIP" = "$NEW_TIP_H" ]; then
    pass "index converged on the active chain (tip $E_TIP)"
  else
    fail "electrs tip $E_TIP != bitcoind $NEW_TIP_H"
  fi
else
  fail "electrs did not follow the reorg"; tail -3 "$ROOT/electrs.log"
fi

echo
echo "=== Scenario: interrupted initial indexing resumes cleanly ==="
# batch_size=1 so the build is slow enough to actually interrupt; at the
# default it finishes in ~0.1s and there is nothing to cut into.
sed 's/^index_batch_size.*/index_batch_size = 1/' "$ROOT/electrs.toml" > "$ROOT/electrs-slow.toml"
stop_electrs; rm -rf "$ROOT/electrs-db"/*
nohup "$ELECTRS" --conf "$ROOT/electrs-slow.toml" > "$ROOT/electrs.log" 2>&1 &
echo $! > "$ROOT/electrs.pid"
sleep 4; PARTIAL=$(grep -c "chain updated" "$ROOT/electrs.log")
stop_electrs
echo "  killed mid-build after $PARTIAL committed chain updates"
start_electrs
if wait_indexed "$NEW_TIP_H" 120; then
  pass "resumed from a partial index and reached tip $NEW_TIP_H"
else
  fail "did not resume"; tail -3 "$ROOT/electrs.log"
fi
stop_electrs
echo
echo "done"
