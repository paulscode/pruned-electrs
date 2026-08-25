#!/usr/bin/env python3
"""Minimal stand-in for btc-rpc-proxy, for testing the pruned path on a BLAKE2b chain.

Forwards every JSON-RPC call to the pruned node. When `getblock` at verbosity 0
or 1 fails there because the block has been pruned, it retries against an
archival node and returns that answer instead. That is the behaviour of
btc-rpc-proxy that electrs's pruned routing depends on, and the same verbosity
range the real proxy intercepts.

It exists because btc-rpc-proxy itself cannot serve a v2 block: it decodes peer
blocks with rust-bitcoin's `Block` and checks a SHA256d `block_hash()`, both of
which are wrong past the BLAKE2b activation. Teaching it the new format is real
work and out of scope here; this shim isolates *electrs's* pruned routing so it
can be tested on its own.

Deliberately not a substitute for the real thing: no peer fetching, no block
validation, no auth passthrough beyond a fixed credential. Test scaffolding.

  ./fallback_proxy.py <listen-port> <pruned-url> <archival-url>
"""
import json
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PRUNED, ARCHIVAL = "", ""


def call(url, body, auth=None):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


class Handler(BaseHTTPRequestHandler):
    # electrs's JSON-RPC transport rejects an HTTP/1.0 response outright, and
    # it reuses the connection, so both of these are required.
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        # Pass the caller's credentials straight through, so the shim holds none.
        auth = self.headers.get("Authorization")
        try:
            out = call(PRUNED, body, auth)
            parsed = json.loads(out)
        except Exception as e:
            self.send_error(502, str(e))
            return

        # `getblock` at verbosity 0 or 1 falls back, matching btc-rpc-proxy's
        # scope; verbosity 2 is not intercepted there and is not here either.
        # bitcoind defaults to verbosity 1 when the parameter is omitted, and
        # electrs relies on that for `transaction.id_from_pos` and
        # `transaction.get_merkle`, so an absent parameter counts as 1.
        def needs_fallback(req, resp):
            if not isinstance(req, dict) or req.get("method") != "getblock":
                return False
            params = req.get("params") or []
            verbosity = params[1] if len(params) >= 2 else 1
            return (
                verbosity in (0, 1)
                and isinstance(resp, dict)
                and resp.get("error") is not None
            )

        try:
            req = json.loads(body)
        except Exception:
            req = None

        retry = False
        if isinstance(req, list) and isinstance(parsed, list):
            retry = any(needs_fallback(q, r) for q, r in zip(req, parsed))
        elif needs_fallback(req, parsed):
            retry = True

        if retry:
            try:
                out = call(ARCHIVAL, body, auth)
            except Exception as e:
                self.send_error(502, str(e))
                return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


if __name__ == "__main__":
    port = int(sys.argv[1])
    PRUNED, ARCHIVAL = sys.argv[2], sys.argv[3]
    print(f"fallback proxy on :{port}  pruned={PRUNED}  archival={ARCHIVAL}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
