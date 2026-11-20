# Wiring (phase 1)

All values below come from `esp32/finality_led/finality_led.ino`.

| item | value |
|---|---|
| Board | ESP32-C6, Arduino-ESP32 core ≥ 3.0, board "ESP32C6 Dev Module" |
| PC link | USB-C port labelled "USB" (native USB-CDC), 115200 baud |
| RGB LED | on-board WS2812 on `RGB_PIN = 27` (board labelled `RGB@IO27`; ESP32-C6-DevKitC-1 uses GPIO8) |
| Relay / solenoid | `RELAY_PIN = 4`, active HIGH (optional) |
| Release time | `RELEASE_MS = 1500` |

## Serial protocol (PC → ESP32)

| byte | action |
|---|---|
| `G` | relay HIGH + LED green for 1.5 s, then relay LOW + LED idle (dim blue); replies `RELEASE <n> us` |
| `R` | LED red for 0.5 s (not sent by `gate.py` yet) |

On boot the sketch prints `finality_led ready`.

## Relay (optional)

```
ESP32-C6 GPIO4 ──> relay module IN
ESP32-C6 GND   ──> relay module GND
relay module VCC <── supply rated for the module
relay contacts   ──> lock / solenoid circuit (its own supply)
```

Drive a solenoid through a relay module or a transistor/MOSFET stage with a
flyback diode, never directly from the GPIO pin. Check the relay module's input
voltage and logic level against the ESP32-C6's 3.3 V GPIO.

No photos or videos of the build are included yet.
