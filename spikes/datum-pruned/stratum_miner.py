#!/usr/bin/env python3
"""A minimal stratum v1 miner, enough to drive DATUM Gateway on regtest.

Not a serious miner. It exists to prove a block can travel miner -> DATUM ->
pruned bitcoind and be accepted, which is the one cell of the goal matrix that
had no evidence behind it.
"""
import binascii
import hashlib
import json
import socket
import struct
import sys
import time


def dsha(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


class Stratum:
    def __init__(self, host, port):
        self.sock = socket.create_connection((host, port), timeout=30)
        self.buf = b""
        self.next_id = 1
        self.job = None
        self.extranonce1 = None
        self.extranonce2_size = 4
        self.difficulty = 1

    def send(self, method, params):
        mid = self.next_id
        self.next_id += 1
        line = json.dumps({"id": mid, "method": method, "params": params}) + "\n"
        self.sock.sendall(line.encode())
        return mid

    def recv(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("stratum server closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def wait_for(self, mid):
        """Pump messages until the reply to `mid` arrives, handling notifies."""
        while True:
            msg = self.recv()
            if msg.get("id") == mid and "method" not in msg:
                return msg
            self.handle_notification(msg)

    def handle_notification(self, msg):
        method = msg.get("method")
        if method == "mining.notify":
            p = msg["params"]
            self.job = {
                "job_id": p[0], "prevhash": p[1], "coinb1": p[2], "coinb2": p[3],
                "merkle_branch": p[4], "version": p[5], "nbits": p[6],
                "ntime": p[7], "clean": p[8],
            }
        elif method == "mining.set_difficulty":
            self.difficulty = msg["params"][0]

    def subscribe_and_authorize(self, user, password="x"):
        mid = self.send("mining.subscribe", ["pruned-node-test/0.1"])
        reply = self.wait_for(mid)
        result = reply["result"]
        self.extranonce1 = result[1]
        self.extranonce2_size = result[2]
        mid = self.send("mining.authorize", [user, password])
        reply = self.wait_for(mid)
        if not reply.get("result"):
            raise SystemExit(f"authorize refused: {reply}")

    def wait_for_job(self, timeout=60):
        deadline = time.time() + timeout
        while self.job is None and time.time() < deadline:
            self.sock.settimeout(max(1, deadline - time.time()))
            try:
                self.handle_notification(self.recv())
            except socket.timeout:
                break
        return self.job


def swap4(b):
    """Stratum sends prevhash as eight 4-byte words, each reversed."""
    return b"".join(b[i:i + 4][::-1] for i in range(0, len(b), 4))


def build_header(job, extranonce1, extranonce2, nonce, prev_transform):
    coinbase = (binascii.unhexlify(job["coinb1"]) + binascii.unhexlify(extranonce1)
                + binascii.unhexlify(extranonce2) + binascii.unhexlify(job["coinb2"]))
    merkle = dsha(coinbase)
    for branch in job["merkle_branch"]:
        merkle = dsha(merkle + binascii.unhexlify(branch))
    prev = prev_transform(binascii.unhexlify(job["prevhash"]))
    return (
        struct.pack("<I", int(job["version"], 16))
        + prev
        + merkle
        + struct.pack("<I", int(job["ntime"], 16))
        + struct.pack("<I", int(job["nbits"], 16))
        + struct.pack("<I", nonce)
    ), coinbase


def target_from_nbits(nbits):
    nbits = int(nbits, 16)
    exponent = nbits >> 24
    mantissa = nbits & 0xffffff
    return mantissa * (1 << (8 * (exponent - 3)))


DIFF1 = 0x00000000FFFF0000000000000000000000000000000000000000000000000000


def mine(s, worker, max_nonces):
    """Grind until a hash beats the block target, then submit it.

    On regtest the block target is around 2**255, so this lands almost at once,
    while the share target at difficulty 1 is 2**224 and would take 2**32 tries.
    Submitting the block-beating share is the point: what matters here is
    whether DATUM forwards it to a pruned bitcoind, not share accounting.
    """
    job = s.job
    block_target = target_from_nbits(job["nbits"])
    share_target = int(DIFF1 / s.difficulty)
    extranonce2 = "00" * s.extranonce2_size

    for nonce in range(max_nonces):
        header, coinbase = build_header(job, s.extranonce1, extranonce2, nonce, swap4)
        h = dsha(header)
        value = int.from_bytes(h, "little")
        if value <= block_target:
            print(f"  nonce {nonce}: hash {h[::-1].hex()}")
            print(f"    beats block target: yes")
            print(f"    beats share target: {'yes' if value <= share_target else 'no'}")
            mid = s.send("mining.submit",
                         [worker, job["job_id"], extranonce2, job["ntime"], "%08x" % nonce])
            reply = s.wait_for(mid)
            print(f"    submit reply: {json.dumps(reply)}")
            return reply
    print(f"  no hash beat the block target in {max_nonces} tries")
    return None


if __name__ == "__main__":
    host, port = "127.0.0.1", 23334
    worker = sys.argv[1] if len(sys.argv) > 1 else "test.worker"
    s = Stratum(host, port)
    s.subscribe_and_authorize(worker)
    print(f"subscribed: extranonce1={s.extranonce1} extranonce2_size={s.extranonce2_size}")
    job = s.wait_for_job()
    if job is None:
        raise SystemExit("no job arrived")
    print(f"job_id={job['job_id']}")
    print(f"  version  {job['version']}")
    print(f"  prevhash {job['prevhash']}")
    print(f"  nbits    {job['nbits']}  ntime {job['ntime']}")
    print(f"  merkle_branch entries: {len(job['merkle_branch'])}")
    print(f"  difficulty set to {s.difficulty}")
    print(f"  block target: {target_from_nbits(job['nbits']):#x}")
    mine(s, worker, 200000)
