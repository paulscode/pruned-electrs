import socket, json, os
def q(port, method, params):
    s = socket.create_connection(("127.0.0.1", port), timeout=15); f = s.makefile("rw")
    f.write(json.dumps({"id":0,"method":method,"params":params})+"\n"); f.flush()
    r = json.loads(f.readline()); s.close(); return r
P = int(os.environ.get("EPORT","60610"))
print("--- chain BELOW the activation height (still v1) ---")
for label, params in [("Sparrow today (1.3-1.4.2)", ["Sparrow", ["1.3","1.4.2"]]),
                      ("a 1.8 client",              ["patched", ["1.4","1.8"]])]:
    r = q(P, "server.version", params)
    print(f"  {label:<28} -> {r.get('result') or 'REFUSED: ' + str(r['error'])[:70]}")
f = q(P, "server.features", [])["result"]
print(f"  features: protocol_min={f['protocol_min']} protocol_max={f['protocol_max']} blake2b_fork={f.get('blake2b_fork')}")
