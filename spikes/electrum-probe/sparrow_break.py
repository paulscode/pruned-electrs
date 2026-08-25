"""What a fixed-80-byte Electrum client actually does with a v2 header."""
import socket, json, os, hashlib
HOST = os.environ.get("EHOST", "10.0.3.1"); PORT = int(os.environ.get("EPORT", "50343"))
s = socket.create_connection((HOST, PORT), timeout=25); f = s.makefile("rw")
def rpc(m, p):
    f.write(json.dumps({"id": 0, "method": m, "params": p}) + "\n"); f.flush()
    return json.loads(f.readline())
rpc("server.version", ["Sparrow", ["1.3", "1.4.2"]])
def dsha(b): return hashlib.sha256(hashlib.sha256(b).digest()).digest()

a = bytes.fromhex(rpc("blockchain.block.header", [149537])["result"])
b = bytes.fromhex(rpc("blockchain.block.header", [149538])["result"])

print("Header 149537 is", len(a), "bytes. Its first 80 parse as a valid-looking v1 header:")
print("  version  :", hex(int.from_bytes(a[0:4], "little")), "(bit 31 set: the v2 marker)")
print("  prev     :", a[4:36][::-1].hex()[:32], "...")
print("  merkle   :", a[36:68][::-1].hex()[:32], "...")
print("  -> a client reading 80 bytes sees a structurally fine header\n")

client_hash = dsha(a[:80])[::-1].hex()
real_next_prev = b[4:36][::-1].hex()
print("But the block hash it computes is SHA256d of those 80 bytes:")
print("  client computes :", client_hash)
print("  header 149538's prev_blockhash says the real one is:")
print("                    ", real_next_prev)
print("  match:", client_hash == real_next_prev)
print("\n  -> chain linkage fails at the first BLAKE2b block. The client cannot")
print("     connect 149537 to 149538, so it cannot verify the chain at all.")
