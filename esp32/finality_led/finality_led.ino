/*
  finality_led — ESP32-C6 side of finality-gate (phase 1: serial-driven).

  The PC runs gate.py, waits for the Solana transaction to reach `finalized`,
  then writes one byte 'G' over USB serial. This sketch turns the on-board
  RGB LED green for RELEASE_MS, then back to idle (dim blue).

  Later phases replace the serial byte with the C6 talking to the RPC itself,
  and the LED with a relay / solenoid on RELAY_PIN.

  Board : ESP32-C6 (Arduino-ESP32 core >= 3.0, "ESP32C6 Dev Module")
  Lib   : Adafruit NeoPixel (Library Manager)
  Port  : use the USB-C labelled "USB" (native USB-CDC) for Serial.

  RGB pin: the board in use is labelled "RGB@IO27". Espressif's own
  ESP32-C6-DevKitC-1 uses GPIO8. Change RGB_PIN if the LED stays dark.
*/

#include <Adafruit_NeoPixel.h>

#define RGB_PIN     27      // on-board WS2812 (see note above)
#define RELAY_PIN   4       // optional relay / solenoid, active HIGH
#define RELEASE_MS  1500    // how long the "release" lasts
#define BAUD        115200

Adafruit_NeoPixel px(1, RGB_PIN, NEO_GRB + NEO_KHZ800);

void setColor(uint8_t r, uint8_t g, uint8_t b) {
  px.setPixelColor(0, px.Color(r, g, b));
  px.show();
}

void setup() {
  Serial.begin(BAUD);
  pinMode(RELAY_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, LOW);
  px.begin();
  px.setBrightness(40);
  setColor(0, 0, 20);              // idle: dim blue
  Serial.println("finality_led ready");
}

void loop() {
  if (Serial.available()) {
    char c = Serial.read();
    if (c == 'G') {                // finalized -> release
      unsigned long t0 = micros();
      digitalWrite(RELAY_PIN, HIGH);
      setColor(0, 255, 0);
      Serial.printf("RELEASE %lu us\n", micros() - t0);
      delay(RELEASE_MS);
      digitalWrite(RELAY_PIN, LOW);
      setColor(0, 0, 20);
    } else if (c == 'R') {         // error / rejected
      setColor(255, 0, 0);
      delay(500);
      setColor(0, 0, 20);
    }
  }
}
