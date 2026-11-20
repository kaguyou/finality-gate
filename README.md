# finality-gate

Open-source reference implementation that releases physical goods only after a
Solana transaction is **`finalized`** (NFC / Solana Pay / ESP32).

Tap an NTAG215 tag that carries a Solana Pay URL, pay from a phone wallet, and a
PC controller (`pc/gate.py`) waits for the transaction at `commitment=finalized`.
Only when the finalized notification arrives does it send one serial byte `G` to an
ESP32, which drives an LED / relay (the stand-in for a locker, vending machine or
gate). The transaction is then checked with `getTransaction` and every trial is
appended to a CSV.

## How it works

```
 1. tap        NTAG215 tag ──(Solana Pay URL)──> phone wallet
 2. pay        phone wallet ──(transfer tx)──> Solana cluster
 3. finalized  pc/gate.py, over WebSocket:
                 logsSubscribe(mentions=[recipient], commitment=processed)  -> "seen"
                 signatureSubscribe(signature,       commitment=finalized)  -> "finalized"
 4. release    gate.py ──(USB serial 'G')──> ESP32-C6 ──> LED / relay
 5. record     gate.py ──getTransaction──> validate recipient + amount
                       ──> append row to data/trials-<network>.csv
```

- `processed` is used only to timestamp when the transaction is first seen. It
  never triggers the release.
- Release is triggered only by the `signatureSubscribe` notification at
  `commitment=finalized`.

### Fire-then-validate vs. validate-then-fire

By default `gate.py` fires on the finalized notification and *then* fetches the
transaction to check recipient and amount, because the harness measures physical
latency. A real machine must validate first: `--validate-first` adds one
`getTransaction` round-trip before firing and does not fire if the check fails.
The `validated` column in the CSV records the result either way.

## Measured results

### Devnet (n = 10)

| metric | median | p95 |
|---|---|---|
| seen → finalized (`ms_seen_to_finalized`) | 717.3 ms | 1,467.4 ms |
| tap → fired (`ms_tap_to_fired`)\* | 847.5 ms | 1,723.8 ms |

- Source: [`data/trials-devnet.csv`](data/trials-devnet.csv), summarised with
  `python pc/stats.py data/trials-devnet.csv`.
- 10 trials, all `validated=True`, 0.001 SOL each, recorded 2026-10-26 (JST).
  `getAgGenesisCert` returned a certificate on the RPC used (`alpenglow=True`).
- \* **Caveat on "tap → fired":** in this dataset the operator's Enter key press
  (`t_tap`) was not recorded, so `gate.py` fell back to `t_tap = t_seen`
  (`ms_tap_to_seen` is `0.0` in all 10 rows). The "tap → fired" figure therefore
  measures *first seen at `processed` → serial byte written*. It does not include
  the time from the physical tap through wallet approval until the transaction is
  first visible to the RPC.

### Mainnet

Mainnet dataset: in preparation.

## Repository layout

| path | contents |
|---|---|
| `pc/gate.py` | PC controller: watches the chain, sends `G` over serial, logs to CSV |
| `pc/stats.py` | count / median / p95 / min / max of a trials CSV (validated rows only) |
| `pc/requirements.txt` | Python dependencies |
| `esp32/finality_led/finality_led.ino` | Arduino sketch for ESP32-C6: `G` → green LED + relay pin |
| `data/` | measurement CSVs |
| `docs/` | wiring notes |
| `index.html` | static NFC landing page (GitHub Pages), see below |

## Reproduce

### Hardware

- ESP32-C6 board with an on-board WS2812 RGB LED (the board used here is labelled
  `RGB@IO27`; Espressif's ESP32-C6-DevKitC-1 uses GPIO8 — set `RGB_PIN`)
- USB cable to the PC (use the port labelled "USB", native USB-CDC)
- NTAG215 NFC tag and a phone that can write it (e.g. the *NFC Tools* app)
- Phone with a Solana wallet that handles Solana Pay URLs (e.g. Phantom)
- Optional: relay module on GPIO4 (see [`docs/wiring.md`](docs/wiring.md))

### Software

- Python 3 (developed on 3.14; `solders`, `websockets`, `pyserial`, `requests`, `base58`)
- Arduino IDE / arduino-cli with Arduino-ESP32 core ≥ 3.0 and the
  *Adafruit NeoPixel* library; board "ESP32C6 Dev Module"

### Environment variables

None are required. RPC endpoints are passed as flags. For a dedicated RPC
provider whose URL contains an API key, pass it with `--http` / `--ws` and keep
it out of the repository (`.env` files are git-ignored).

### Steps (devnet)

1. Flash `esp32/finality_led/finality_led.ino` to the ESP32-C6. The LED idles dim
   blue. Note the serial port (e.g. `/dev/ttyACM0`, `COM5`).
2. Install the PC side:
   ```bash
   cd pc
   python -m venv .venv && . .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Print the Solana Pay URL:
   ```bash
   python gate.py --network devnet --show-url
   ```
   On first run this creates `pc/keys/recipient-devnet.json` and
   `pc/keys/reference-devnet.json` (git-ignored; never commit them). To receive
   into a wallet you already own, add `--recipient <PUBKEY>`.
4. Write the URL to the NTAG215 (NFC Tools → Write → Add record → URL).
   iOS background NFC only opens `http(s)` URLs, so for iPhones write the landing
   page URL instead, with the Solana Pay URL in the fragment:
   `https://<you>.github.io/finality-gate/#solana:<recipient>?amount=0.001&reference=<ref>&label=...`
   The page (`index.html`) hands the URL to the wallet; the fragment is never sent
   to a server.
5. Switch the wallet to devnet and fund it from `https://faucet.solana.com`.
6. Run the gate:
   ```bash
   python gate.py --network devnet --serial /dev/ttyACM0
   # production behaviour: add --validate-first
   ```
7. Press **Enter** at the moment you tap the phone (records `t_tap`), then
   approve in the wallet. The console shows `seen` → `FINALIZED` → `fired`, the
   LED turns green for 1.5 s, and a row is appended to `data/trials-devnet.csv`.
8. Summarise:
   ```bash
   python stats.py ../data/trials-devnet.csv
   ```

For mainnet, use `--network mainnet` and preferably a dedicated RPC via
`--http` / `--ws`.

### CSV columns

| column | meaning |
|---|---|
| `t_tap_unix` | Enter press (approximate tap time); falls back to `t_seen_unix` if Enter was not pressed |
| `t_seen_unix` | transaction first seen at `processed` via `logsSubscribe` |
| `t_finalized_unix` | `signatureSubscribe` notification at `finalized` |
| `t_fired_unix` | serial byte written |
| `ms_tap_to_seen` / `ms_seen_to_finalized` / `ms_tap_to_fired` | differences of the above, in ms |
| `alpenglow` | whether `getAgGenesisCert` returned a certificate on the RPC used |
| `validated`, `amount_lamports` | result of the `getTransaction` recipient/amount check |

## Notes and limitations

- `gate.py` subscribes to logs that mention the **recipient** address, so any
  transfer to that address during a run is treated as a new trial. Use a
  dedicated recipient address for the gate.
- Only native SOL transfers are validated (`preBalances` / `postBalances` delta).
  SPL tokens (e.g. USDC) are not handled yet.
- Uses raw JSON-RPC + `solders` only (no `solana-py`).
- The ESP32 sketch also understands `R` (red flash for "rejected"), but
  `gate.py` does not send it yet.

## License

MIT — see [LICENSE](LICENSE).
