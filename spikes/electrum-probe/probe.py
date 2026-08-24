import json,socket,ssl,sys
host,port = sys.argv[1], int(sys.argv[2])
ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
s=ctx.wrap_socket(socket.create_connection((host,port),timeout=20),server_hostname=host)
n=0
def call(m,p):
    global n; n+=1
    s.sendall((json.dumps({"jsonrpc":"2.0","id":n,"method":m,"params":p})+"\n").encode())
    buf=b""
    while not buf.endswith(b"\n"):
        c=s.recv(65536)
        if not c: raise EOFError
        buf+=c
    return json.loads(buf)
print("server.version ->", call("server.version",["probe","1.4"]))
print("server.features ->", json.dumps(call("server.features",[]), indent=1)[:700])
r=call("blockchain.headers.subscribe",[])
res=r.get("result",{})
hexh=res.get("hex","")
print(f"tip height={res.get('height')}  header_hex_len={len(hexh)}  ({len(hexh)//2} bytes)")
print("first 8 hex (version LE):", hexh[:8])
