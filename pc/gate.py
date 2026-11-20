#!/usr/bin/env python3
"""
finality-gate: wait for a Solana Pay transfer to reach `finalized`, then fire a
physical output (serial byte -> ESP32-C6 LED / relay) and log timing.

Flow
  1. Load (or create) recipient + reference keypairs.
  2. Print the Solana Pay URL to write onto the NFC tag (NTAG215).
  3. Subscribe (WebSocket):
       logsSubscribe  mentions=[reference]  commitment=processed  -> first sight
       signatureSubscribe(sig)             commitment=finalized  -> finality
  4. On finalized: validate transfer (recipient got >= amount), write b"G" to
     serial, append a CSV row with all timestamps.

Only the Python stdlib + solders + websockets + pyserial + requests + base58.
No solana-py, so it also runs on small ARM boards without native builds.

Usage
  python gate.py --network devnet                # default
  python gate.py --network mainnet --serial /dev/ttyACM0
  python gate.py --recipient <YOUR_PUBKEY>       # pay into a wallet you own
  python gate.py --show-url                      # just print the Solana Pay URL
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests
import websockets
from solders.keypair import Keypair
from solders.pubkey import Pubkey

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

NETWORKS = {
    "devnet": {
        "http": "https://api.devnet.solana.com",
        "ws": "wss://api.devnet.solana.com",
    },
    "mainnet": {
        "http": "https://api.mainnet-beta.solana.com",
        "ws": "wss://api.mainnet-beta.solana.com",
    },
}

LAMPORTS_PER_SOL = 1_000_000_000
HERE = Path(__file__).resolve().parent
KEYS_DIR = HERE / "keys"
DATA_DIR = HERE.parent / "data"

CSV_FIELDS = [
    "trial",
    "network",
    "alpenglow",
    "signature",
    "slot_finalized",
    "t_tap_unix",         # operator pressed Enter (approximate tap time)
    "t_seen_unix",        # tx first seen (processed) via logsSubscribe
    "t_finalized_unix",   # signatureSubscribe(finalized) notification
    "t_fired_unix",       # serial byte written
    "ms_tap_to_seen",
    "ms_seen_to_finalized",
    "ms_tap_to_fired",
    "amount_lamports",
    "validated",
]


@dataclass
class Trial:
    n: int
    t_tap: float
    t_seen: Optional[float] = None
    t_finalized: Optional[float] = None
    t_fired: Optional[float] = None
    signature: Optional[str] = None
    slot: Optional[int] = None
    validated: bool = False
    amount: int = 0
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #

def load_or_create_keypair(path: Path) -> Keypair:
    if path.exists():
        return Keypair.from_bytes(bytes(json.loads(path.read_text())))
    kp = Keypair()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(bytes(kp))))
    os.chmod(path, 0o600)
    return kp


# --------------------------------------------------------------------------- #
# RPC helpers (plain JSON-RPC; no solana-py)
# --------------------------------------------------------------------------- #

class Rpc:
    def __init__(self, http: str):
        self.http = http
        self._id = 0

    def call(self, method: str, params: list | None = None):
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or []}
        r = requests.post(self.http, json=body, timeout=20)
        r.raise_for_status()
        j = r.json()
        if "error" in j:
            raise RuntimeError(f"{method}: {j['error']}")
        return j["result"]

    def alpenglow_active(self) -> Optional[bool]:
        """True if cluster runs Alpenglow, False if TowerBFT, None if node is too old."""
        try:
            cert = self.call("getAgGenesisCert")
            return cert is not None
        except RuntimeError as e:
            if "-32601" in str(e) or "Method not found" in str(e):
                return None
            raise

    def get_transaction(self, sig: str):
        return self.call(
            "getTransaction",
            [sig, {"commitment": "finalized", "maxSupportedTransactionVersion": 0,
                   "encoding": "json"}],
        )


def validate_transfer(tx: dict, recipient: Pubkey, min_lamports: int) -> tuple[bool, int]:
    """Check that `recipient` received at least `min_lamports` of SOL in `tx`."""
    if tx is None or tx.get("meta") is None or tx["meta"].get("err") is not None:
        return False, 0
    keys = tx["transaction"]["message"]["accountKeys"]
    # accountKeys may be strings or {pubkey:...} depending on encoding
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
    try:
        idx = keys.index(str(recipient))
    except ValueError:
        return False, 0
    pre = tx["meta"]["preBalances"][idx]
    post = tx["meta"]["postBalances"][idx]
    delta = post - pre
    return delta >= min_lamports, delta


# --------------------------------------------------------------------------- #
# Output (serial -> ESP32-C6)
# --------------------------------------------------------------------------- #

class Output:
    def __init__(self, port: Optional[str], baud: int = 115200):
        self.ser = None
        if port:
            import serial  # pyserial
            self.ser = serial.Serial(port, baud, timeout=0.1)
            time.sleep(0.3)  # let the board settle after DTR toggle

    def fire(self) -> float:
        t = time.time()
        if self.ser:
            self.ser.write(b"G")
            self.ser.flush()
        else:
            print("  [output] (no serial port) -> would fire now")
        return t


# --------------------------------------------------------------------------- #
# Solana Pay URL
# --------------------------------------------------------------------------- #

def solana_pay_url(recipient: Pubkey, reference: Pubkey, amount_sol: float,
                   label: str, message: str) -> str:
    # https://docs.solanapay.com/spec  (transfer request)
    amt = f"{amount_sol:.9f}".rstrip("0").rstrip(".")
    return (
        f"solana:{recipient}?amount={amt}&reference={reference}"
        f"&label={quote(label)}&message={quote(message)}"
    )


# --------------------------------------------------------------------------- #
# Watcher
# --------------------------------------------------------------------------- #

async def ws_subscribe(ws, method: str, params: list) -> int:
    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == 1:
            if "error" in msg:
                raise RuntimeError(f"{method}: {msg['error']}")
            return msg["result"]


async def wait_finalized(ws_url: str, signature: str) -> tuple[float, int]:
    """Open a dedicated socket, subscribe to one signature at finalized, return (t, slot)."""
    async with websockets.connect(ws_url, ping_interval=20, max_size=None) as ws:
        await ws_subscribe(ws, "signatureSubscribe",
                           [signature, {"commitment": "finalized"}])
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("method") == "signatureNotification":
                t = time.time()
                res = msg["params"]["result"]
                if res["value"].get("err"):
                    raise RuntimeError(f"tx failed: {res['value']['err']}")
                return t, res["context"]["slot"]


async def run(args):
    net = NETWORKS[args.network]
    http_url = args.http or net["http"]
    ws_url = args.ws or net["ws"]

    rpc = Rpc(http_url)
    recipient_kp = load_or_create_keypair(KEYS_DIR / f"recipient-{args.network}.json")
    recipient = Pubkey.from_string(args.recipient) if args.recipient else recipient_kp.pubkey()
    reference_kp = load_or_create_keypair(KEYS_DIR / f"reference-{args.network}.json")
    reference = reference_kp.pubkey()

    url = solana_pay_url(recipient, reference, args.amount, args.label, args.message)
    print("=" * 72)
    print(f"network   : {args.network}  ({http_url})")
    print(f"recipient : {recipient}")
    print(f"reference : {reference}")
    print(f"amount    : {args.amount} SOL")
    print("Solana Pay URL (write this to the NFC tag):")
    print(f"  {url}")
    print("=" * 72)
    if args.show_url:
        return

    ag = rpc.alpenglow_active()
    ag_str = {True: "yes", False: "no (TowerBFT)", None: "unknown (node too old)"}[ag]
    print(f"Alpenglow active on this RPC: {ag_str}")

    out = Output(args.serial)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = DATA_DIR / f"trials-{args.network}.csv"
    new_file = not csv_path.exists()
    f = open(csv_path, "a", newline="")
    w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
    if new_file:
        w.writeheader()
        f.flush()

    min_lamports = int(args.amount * LAMPORTS_PER_SOL)
    seen: set[str] = set()
    trial_n = 0

    print(f"\nListening for transfers that mention {recipient} ...")
    print("Press Enter right as you tap the phone (records t_tap). Ctrl-C to quit.\n")

    async with websockets.connect(ws_url, ping_interval=20, max_size=None) as ws:
        await ws_subscribe(ws, "logsSubscribe",
                           [{"mentions": [str(recipient)]}, {"commitment": "processed"}])

        loop = asyncio.get_running_loop()
        pending_tap: Optional[float] = None

        async def read_enter():
            nonlocal pending_tap
            while True:
                await loop.run_in_executor(None, sys.stdin.readline)
                pending_tap = time.time()
                print(f"  [tap] t_tap recorded @ {pending_tap:.3f}")

        asyncio.create_task(read_enter())

        while True:
            msg = json.loads(await ws.recv())
            if msg.get("method") != "logsNotification":
                continue
            t_seen = time.time()
            val = msg["params"]["result"]["value"]
            sig = val["signature"]
            if sig in seen or set(sig) == {"1"}:   # all-ones = default/empty signature
                continue
            seen.add(sig)
            if val.get("err"):
                print(f"  [seen] {sig[:16]}... but tx errored, ignoring")
                continue

            trial_n += 1
            tr = Trial(n=trial_n, t_tap=pending_tap or t_seen, t_seen=t_seen, signature=sig)
            pending_tap = None
            print(f"[{tr.n}] seen (processed) {sig[:20]}...  +{(t_seen - tr.t_tap)*1000:.0f} ms from tap")

            try:
                tr.t_finalized, tr.slot = await asyncio.wait_for(wait_finalized(ws_url, sig), 30)
            except Exception as e:
                print(f"  finalized wait failed: {e}")
                continue
            print(f"[{tr.n}] FINALIZED slot {tr.slot}  +{(tr.t_finalized - tr.t_seen)*1000:.0f} ms from seen")

            # fire first, validate after: physical latency is what we measure.
            # (For a real vending machine, validate BEFORE firing. See README.)
            if args.validate_first:
                tx = rpc.get_transaction(sig)
                tr.validated, tr.amount = validate_transfer(tx, recipient, min_lamports)
                if not tr.validated:
                    print(f"  validation FAILED (got {tr.amount} lamports), not firing")
                    continue
                tr.t_fired = out.fire()
            else:
                tr.t_fired = out.fire()
                tx = rpc.get_transaction(sig)
                tr.validated, tr.amount = validate_transfer(tx, recipient, min_lamports)

            print(f"[{tr.n}] fired  +{(tr.t_fired - tr.t_tap)*1000:.0f} ms from tap   "
                  f"validated={tr.validated} amount={tr.amount} lamports")

            w.writerow({
                "trial": tr.n,
                "network": args.network,
                "alpenglow": ag,
                "signature": sig,
                "slot_finalized": tr.slot,
                "t_tap_unix": f"{tr.t_tap:.6f}",
                "t_seen_unix": f"{tr.t_seen:.6f}",
                "t_finalized_unix": f"{tr.t_finalized:.6f}",
                "t_fired_unix": f"{tr.t_fired:.6f}",
                "ms_tap_to_seen": f"{(tr.t_seen - tr.t_tap)*1000:.1f}",
                "ms_seen_to_finalized": f"{(tr.t_finalized - tr.t_seen)*1000:.1f}",
                "ms_tap_to_fired": f"{(tr.t_fired - tr.t_tap)*1000:.1f}",
                "amount_lamports": tr.amount,
                "validated": tr.validated,
            })
            f.flush()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--network", choices=NETWORKS.keys(), default="devnet")
    p.add_argument("--http", help="override HTTP RPC url (e.g. Helius)")
    p.add_argument("--ws", help="override WebSocket RPC url")
    p.add_argument("--recipient", help="pubkey that receives payment (default: generated key in ./keys)")
    p.add_argument("--amount", type=float, default=0.001, help="SOL per tap (default 0.001)")
    p.add_argument("--label", default="FinalityGate")
    p.add_argument("--message", default="Tap-to-release demo")
    p.add_argument("--serial", help="serial port of the ESP32-C6 (e.g. /dev/ttyACM0, COM5)")
    p.add_argument("--validate-first", action="store_true",
                   help="validate amount/recipient BEFORE firing (production behaviour)")
    p.add_argument("--show-url", action="store_true", help="print the Solana Pay URL and exit")
    args = p.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
