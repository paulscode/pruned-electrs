import json,socket,ssl,sys
host,port=sys.argv[1],int(sys.argv[2]); ver=sys.argv[3] if len(sys.argv)>3 else "1.4"
ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
s=ctx.wrap_socket(socket.create_connection((host,port),timeout=20),server_hostname=host)
n=0
def call(m,p):
    global n;n+=1
    s.sendall((json.dumps({"jsonrpc":"2.0","id":n,"method":m,"params":p})+"\n").encode())
    buf=b""
    while not buf.endswith(b"\n"):
        c=s.recv(1<<20)
        if not c: raise EOFError
        buf+=c
    return json.loads(buf)
v=call("server.version",["probe",ver])
print(f"{host}: negotiated {v.get('result')}")
r=call("blockchain.block.headers",[100000,2]).get("result",{})
keys=sorted(r.keys())
shape="list" if isinstance(r.get("headers"),list) else ("concatenated hex" if isinstance(r.get("hex"),str) else "?")
print(f"  block.headers keys={keys} -> {shape}")
