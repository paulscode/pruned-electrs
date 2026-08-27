#!/usr/bin/env bash
# Regtest harness for electrs-pruned.
#
# Topology — the smallest thing that can reproduce a StartOS pruned node:
#
#   node A (archival, :19000)  ◀── p2p ──  node B (pruned, :19010)
#                                             │
#                                             ├─ rpc :19011  (private, bitcoind)
#                                             ├─ whitebind :19012 (noban+download, for electrs p2p)
#                                             │
#                                          proxy :19013  ──▶ B's rpc, falls back to peers (= A)
#
# B connects OUT to A so A shows up in B's `getpeerinfo` as `!inbound` with the
# NETWORK service — which is what btc-rpc-proxy filters for. Without the
# outbound direction the proxy sees no eligible peers and every pruned fetch
# fails.
#
# Needs btc-rpc-proxy v0.7.0 or later. Before v0.6.0 it hardcoded mainnet magic
# and could not speak regtest at all; v0.6.0 took the magic from a `network`
# option, and v0.7.0 derives it from bitcoind, so this config sets neither.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BITCOIND="${BITCOIND:-$HOME/bin/knots/bin/bitcoind}"
BITCOIN_CLI="${BITCOIN_CLI:-$HOME/bin/knots/bin/bitcoin-cli}"
PROXY="${PROXY:-$(cd "$ROOT/../.." && pwd)/vendor/btc-rpc-proxy/target/release/btc_rpc_proxy}"

A_DIR="$ROOT/nodeA"; A_RPC=19001; A_P2P=19000
B_DIR="$ROOT/nodeB"; B_RPC=19011; B_P2P=19010; B_WHITE=19012
PROXY_PORT=19013

acli() { "$BITCOIN_CLI" -datadir="$A_DIR" -conf="$A_DIR/bitcoin.conf" "$@"; }
bcli() { "$BITCOIN_CLI" -datadir="$B_DIR" -conf="$B_DIR/bitcoin.conf" "$@"; }

write_confs() {
  mkdir -p "$A_DIR" "$B_DIR"
  cat > "$A_DIR/bitcoin.conf" <<EOF
regtest=1
server=1
listen=1
fallbackfee=0.0001
[regtest]
rpcport=$A_RPC
port=$A_P2P
bind=127.0.0.1:$A_P2P
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
EOF
  # prune=1 is manual-prune mode: nothing is discarded until `pruneblockchain`
  # is called, which keeps the harness deterministic.
  cat > "$B_DIR/bitcoin.conf" <<EOF
regtest=1
server=1
prune=1
listen=1
fallbackfee=0.0001
[regtest]
rpcport=$B_RPC
port=$B_P2P
bind=127.0.0.1:$B_P2P
whitebind=127.0.0.1:$B_WHITE
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
connect=127.0.0.1:$A_P2P
EOF
}

start_nodes() {
  # -fastprune shrinks block files to ~16kB so `pruneblockchain` has whole
  # files to discard. Without it a regtest chain lives in a single file and
  # nothing is ever actually pruned.
  "$BITCOIND" -datadir="$A_DIR" -conf="$A_DIR/bitcoin.conf" -daemon
  "$BITCOIND" -datadir="$B_DIR" -conf="$B_DIR/bitcoin.conf" -fastprune -daemon
  for i in $(seq 30); do
    if acli getblockcount >/dev/null 2>&1 && bcli getblockcount >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "nodes failed to come up" >&2; return 1
}

start_proxy() {
  cat > "$ROOT/proxy.toml" <<EOF
bitcoind_address = "127.0.0.1"
bitcoind_port = $B_RPC
bind_address = "127.0.0.1"
bind_port = $PROXY_PORT
cookie_file = "$B_DIR/regtest/.cookie"
default_fetch_blocks = true
max_peer_concurrency = 3
passthrough_rpccookie = "$B_DIR/regtest/.cookie"
EOF
  nohup "$PROXY" --conf "$ROOT/proxy.toml" -vvvv > "$ROOT/proxy.log" 2>&1 &
  echo $! > "$ROOT/proxy.pid"
  sleep 2
}

write_electrs_conf() {
  # RPC points at the proxy, not bitcoind: that is the whole arrangement under
  # test. P2P points at node B's whitebind listener, which is what StartOS's
  # `peer-local` host maps onto — an unprivileged binding gets disconnected
  # rather than served when it asks for a pruned block.
  cat > "$ROOT/electrs.toml" <<EOF
network = "regtest"
daemon_rpc_addr = "127.0.0.1:$PROXY_PORT"
daemon_p2p_addr = "127.0.0.1:$B_WHITE"
cookie_file = "$B_DIR/regtest/.cookie"
db_dir = "$ROOT/electrs-db"
electrum_rpc_addr = "127.0.0.1:19014"
monitoring_addr = "127.0.0.1:19015"
log_filters = "INFO"
index_batch_size = 50
EOF
  mkdir -p "$ROOT/electrs-db"
}

stop_all() {
  [ -f "$ROOT/proxy.pid" ] && kill "$(cat "$ROOT/proxy.pid")" 2>/dev/null || true
  rm -f "$ROOT/proxy.pid"
  bcli stop 2>/dev/null || true
  acli stop 2>/dev/null || true
  sleep 3
}

case "${1:-up}" in
  up)
    write_confs; start_nodes
    # Idempotent: `up` on an existing datadir has a wallet already, which
    # createwallet refuses and loadwallet handles, and has blocks already, which
    # only need topping up to 800.
    acli -named createwallet wallet_name=w >/dev/null 2>&1 \
      || acli loadwallet w >/dev/null 2>&1 || true
    ADDR=$(acli getnewaddress)
    HAVE=$(acli getblockcount)
    if [ "$HAVE" -lt 800 ]; then
      echo "mining $((800 - HAVE)) blocks on A…"
      acli generatetoaddress "$((800 - HAVE))" "$ADDR" > /dev/null
    fi
    echo "waiting for B to sync…"
    for i in $(seq 60); do
      [ "$(bcli getblockcount)" = "800" ] && break; sleep 1
    done
    echo "A=$(acli getblockcount) B=$(bcli getblockcount)"
    # Already-pruned is not an error, so do not let it stop the run.
    bcli pruneblockchain 500 > /dev/null 2>&1 || true
    echo "B pruneheight=$(bcli getblockchaininfo | jq -r .pruneheight)"
    start_proxy
    write_electrs_conf
    echo "proxy up on 127.0.0.1:$PROXY_PORT"
    echo "wrote electrs.toml — run electrs with: electrs --conf $ROOT/electrs.toml"
    ;;
  down) stop_all ;;
  clean) stop_all
    rm -rf "$A_DIR" "$B_DIR" "$ROOT/electrs-db" "$ROOT"/*.log "$ROOT"/*.pid \
           "$ROOT/proxy.toml" "$ROOT/electrs.toml" "$ROOT/electrs-slow.toml" ;;
  *) echo "usage: $0 {up|down|clean}" >&2; exit 1 ;;
esac
