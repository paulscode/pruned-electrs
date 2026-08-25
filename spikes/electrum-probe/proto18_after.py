import socket, json, os
def q(port, method, params):
    s = socket.create_connection(("127.0.0.1", port), timeout=15); f = s.makefile("rw")
    f.write(json.dumps({"id":0,"method":method,"params":params})+"\n"); f.flush()
    r = json.loads(f.readline()); s.close(); return r
P = int(os.environ.get("EPORT","60610"))
print("--- chain ABOVE the activation height (v2) ---")
for label, params in [("Sparrow today (1.3-1.4.2)", ["Sparrow", ["1.3","1.4.2"]]),
                      ("Electrum today (1.4-1.6)",  ["Electrum", ["1.4","1.6"]]),
                      ("a patched 1.8 client",      ["patched", ["1.4","1.8"]])]:
    r = q(P, "server.version", params)
    ok = r.get("result")
    print(f"  {label:<28} -> {ok if ok else 'REFUSED'}")
    if not ok: print(f"        {str(r['error'].get('message'))[:150]}")
f = q(P, "server.features", [])["result"]
print(f"\n  protocol_min={f['protocol_min']} protocol_max={f['protocol_max']}")
print(f"  blake2b_fork={json.dumps(f.get('blake2b_fork'))}")
print(f"  genesis_hash={f['genesis_hash'][:24]}...  (same as ordinary testnet4 on that chain)")
