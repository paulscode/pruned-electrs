import socket, json, sys
import os
HOST = os.environ.get("EHOST", "127.0.0.1")
PORT = int(os.environ.get("EPORT", "50343"))
s = socket.create_connection((HOST, PORT), timeout=25); f = s.makefile("rw")
def rpc(m, p):
    f.write(json.dumps({"id": 0, "method": m, "params": p}) + "\n"); f.flush()
    return json.loads(f.readline())

# Exactly what Sparrow sends: it caps at 1.4.2.
v = rpc("server.version", ["Sparrow", ["1.3", "1.4.2"]])
print("server.version ->", v.get("result", v.get("error")))

tip = rpc("blockchain.headers.subscribe", [])["result"]
hexlen = len(tip["hex"]) // 2
print(f"\nheaders.subscribe -> height {tip['height']}, header {hexlen} bytes")
print("  a client expecting 80 bytes gets:", hexlen)

print("\nblockchain.block.headers(149535, 6)  [spans the fork]")
r = rpc("blockchain.block.headers", [149535, 6])["result"]
blob = r["hex"]; total = len(blob)//2
print(f"  count={r['count']}  concatenated hex = {total} bytes")
print(f"  a client slicing 80-byte chunks would read {total//80} 'headers' from 6")
print(f"  actual: 2 x 80 (pre-fork) + 4 x 164 (post-fork) = {2*80+4*164}")

print("\nper-height sizes:")
for h in (149535, 149536, 149537, 170000):
    hx = rpc("blockchain.block.header", [h])["result"]
    print(f"  {h}: {len(hx)//2} bytes")
