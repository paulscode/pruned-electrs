#!/usr/bin/env python3
"""Grind a difficulty-1 share and submit it through DATUM Gateway.

DATUM enforces the share target (a block-target-only share came back
"H-not-zero") and its difficulty floor is 1, so a share costs about 2**32
hashes however easy regtest's own block target is. At 31 MH/s that is around
138 seconds, and DATUM will not accept a share older than 150, so this grinds
in windows and takes a fresh job each round until one lands.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from stratum_miner import Stratum, build_header, swap4, target_from_nbits, DIFF1

WINDOW_HASHES = 3_800_000_000    # about 125s at the measured rate
THREADS = 20


def run_round(s, name, extranonce2):
    job = s.job
    share_target = int(DIFF1 / s.difficulty)
    header, _ = build_header(job, s.extranonce1, extranonce2, 0, swap4)
    target_hex = share_target.to_bytes(32, "big").hex()

    started = time.time()
    out = subprocess.run(
        [os.path.join(HERE, "miner"), header[:76].hex(), target_hex,
         str(WINDOW_HASHES), str(THREADS)],
        capture_output=True, text=True)
    elapsed = time.time() - started

    if out.stdout.startswith("nonce"):
        nonce = int(out.stdout.split()[1])
        print(f"  found after {elapsed:.0f}s: nonce={nonce}")
        mid = s.send("mining.submit",
                     [name, job["job_id"], extranonce2, job["ntime"], "%08x" % nonce])
        return s.wait_for(mid)
    print(f"  no share in {elapsed:.0f}s, taking a fresh job")
    return None


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "test.worker"
    s = Stratum("127.0.0.1", 23334)
    s.subscribe_and_authorize(name)
    if s.wait_for_job() is None:
        raise SystemExit("no job arrived")
    print(f"difficulty {s.difficulty}, share target {int(DIFF1 / s.difficulty):#x}")
    print(f"block target {target_from_nbits(s.job['nbits']):#x}")

    for round_no in range(8):
        print(f"round {round_no}, job {s.job['job_id']}")
        reply = run_round(s, name, "%016x" % round_no)
        if reply is not None:
            print(f"  submit reply: {reply}")
            if reply.get("result"):
                print("SHARE ACCEPTED")
            break
        s.job = None
        if s.wait_for_job(timeout=150) is None:
            print("no fresh job arrived")
            break
    time.sleep(3)
