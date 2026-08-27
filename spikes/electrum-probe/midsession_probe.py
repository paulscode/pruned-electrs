"""A client that negotiates 1.4 below the activation height and stays connected across it.

The connect-time refusal cannot cover this: the version was agreed when the
chain had no v2 header, and the chain crosses the activation afterwards.
"""
import json, socket, subprocess, sys, time

HOST, PORT = "127.0.0.1", 50501

def cli(*args):
    return subprocess.run(["docker", "exec", "midsession", "bitcoin-cli", "-datadir=/data",
                           "-chain=regtest", "-rpcuser=lab", "-rpcpassword=lab", *args],
                          capture_output=True, text=True).stdout.strip()

class Conn:
    def __init__(self):
        self.s = socket.create_connection((HOST, PORT), timeout=20)
        self.f = self.s.makefile("rwb")
        self.n = 0
    def call(self, method, params):
        self.n += 1
        self.f.write((json.dumps({"id": self.n, "method": method, "params": params}) + "\n").encode())
        self.f.flush()
        line = self.f.readline()
        return json.loads(line) if line else None
    def readline(self, timeout):
        """A line, None on a timeout, or b"" at end of stream, which is the server closing."""
        self.s.settimeout(timeout)
        try:
            return self.f.readline()
        except socket.timeout:
            return None

print("--- a 1.4 client, connected below the activation height ---")
c = Conn()
print("server.version   :", c.call("server.version", ["probe", ["1.3", "1.4"]])["result"])
sub = c.call("blockchain.headers.subscribe", [])["result"]
print("headers.subscribe:", f"height {sub['height']}, {len(sub['hex']) // 2} byte header")

print("\n--- mining past the activation at 20 ---")
addr = cli("getnewaddress")
cli("generatetoaddress", "10", addr)
print("node height      :", cli("getblockcount"))
tip = cli("getblockhash", "25")
print("header at 25     :", len(cli("getblockheader", tip, "false")) // 2, "bytes (v2)")

print("\n--- what the attached 1.4 client sees next ---")
deadline = time.time() + 90
outcome = "still connected after 90s, no notification and no disconnect"
while time.time() < deadline:
    line = c.readline(5)
    if line is None:
        continue                      # nothing yet, electrs polls the node every 10s
    if line == b"":
        outcome = "DISCONNECTED by the server"
        break
    msg = json.loads(line)
    if msg.get("method") == "blockchain.headers.subscribe":
        hexhdr = msg["params"][0]["hex"]
        outcome = f"SERVED a {len(hexhdr) // 2} byte header at height {msg['params'][0]['height']}"
        break
print("outcome          :", outcome)

print("\n--- reconnecting ---")
for rng in (["1.3", "1.4"], ["1.3", "1.8"]):
    c2 = Conn()
    r = c2.call("server.version", ["probe", rng])
    if r.get("error"):
        print(f"  {rng}: REFUSED -> {r['error']['message'][:110]}...")
    else:
        print(f"  {rng}: negotiated {r['result']}")
        h = c2.call("blockchain.block.headers", [24, 2])["result"]
        print(f"           block.headers -> {h['count']} headers, {len(h['hex']) // 2} bytes")
    c2.s.close()
