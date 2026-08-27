"""Compare response shapes for the methods Sparrow uses, across the versions its
old and new SUPPORTED_VERSIONS ranges negotiate."""
import json, socket, ssl, sys

METHODS = [
    ("server.features", []),
    ("blockchain.headers.subscribe", []),
    ("blockchain.block.header", [100000]),
    ("blockchain.block.headers", [100000, 2]),
    ("blockchain.estimatefee", [3]),
    ("blockchain.relayfee", []),
    ("blockchain.transaction.get_merkle",
     ["8c14f0db3df150123e6f3dbbf30f8b955a8249b62ac1d1ff16284aefa3d06d87", 100000]),
]

def shape(v):
    if isinstance(v, dict):
        return "{" + ",".join(f"{k}:{shape(v[k])}" for k in sorted(v)) + "}"
    if isinstance(v, list):
        return "[" + (shape(v[0]) if v else "") + "]"
    return type(v).__name__

def run(host, port, rng):
    ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    s = ctx.wrap_socket(socket.create_connection((host, port), timeout=25), server_hostname=host)
    n = [0]
    def call(m, p):
        n[0] += 1
        s.sendall((json.dumps({"jsonrpc":"2.0","id":n[0],"method":m,"params":p})+"\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            c = s.recv(1<<20)
            if not c: raise EOFError
            buf += c
        return json.loads(buf)
    try:
        v = call("server.version", ["probe", rng])
        out = {"negotiated": v.get("result")}
        for m, p in METHODS:
            try:
                r = call(m, p)
                out[m] = "ERROR" if r.get("error") else shape(r.get("result"))
            except Exception as e:
                out[m] = f"{type(e).__name__}"
        return out
    finally:
        s.close()

for host, port in [("fulcrum.sethforprivacy.com", 50002), ("bitcoin.lu.ke", 50002)]:
    print(f"=== {host} ===")
    old = run(host, port, ["1.3", "1.4.2"])
    new = run(host, port, ["1.3", "1.8"])
    print(f"  old range -> {old['negotiated']}")
    print(f"  new range -> {new['negotiated']}")
    for m, _ in METHODS:
        flag = "  DIFFERS" if old[m] != new[m] else ""
        print(f"    {m:38s} {old[m]}")
        if flag:
            print(f"    {'':38s} {new[m]}{flag}")
