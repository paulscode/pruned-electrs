import json, socket, ssl, sys
def probe(host, port, rng):
    ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    s = ctx.wrap_socket(socket.create_connection((host,port),timeout=25), server_hostname=host)
    n=[0]
    def call(m,p):
        n[0]+=1
        s.sendall((json.dumps({"jsonrpc":"2.0","id":n[0],"method":m,"params":p})+"\n").encode())
        buf=b""
        while not buf.endswith(b"\n"):
            c=s.recv(1<<20)
            if not c: raise EOFError
            buf+=c
        return json.loads(buf)
    try:
        v = call("server.version", ["probe", rng])
        if "error" in v and v["error"]:
            print(f"  range {rng}: server.version ERROR {v['error']}"); return
        neg = v.get("result")
        r = call("blockchain.block.headers", [100000, 2]).get("result", {})
        shape = "LIST" if isinstance(r.get("headers"), list) else ("concat hex" if isinstance(r.get("hex"), str) else f"? keys={sorted(r.keys())}")
        print(f"  range {rng}: negotiated {neg} -> block.headers is {shape}  keys={sorted(r.keys())}")
    finally:
        s.close()

for host, port in [("fulcrum.sethforprivacy.com", 50002), ("electrum.emzy.de", 50002), ("bitcoin.lu.ke", 50002)]:
    print(host)
    for rng in (["1.3","1.4.2"], ["1.3","1.8"]):
        try: probe(host, port, rng)
        except Exception as e: print(f"  range {rng}: {type(e).__name__}: {e}")
