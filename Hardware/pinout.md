# Initial ESP32 pinout

## DHT11 three-pin module

| DHT11 label | ESP32 |
|---|---|
| VCC / + | 3V3 |
| DATA / S / OUT | GPIO 5 |
| GND / - | GND |

Many three-pin modules include a pull-up resistor. Confirm on the board or datasheet. If absent, use 4.7 kΩ–10 kΩ between DATA and 3V3.

## DS3231 RTC

| DS3231 label | ESP32 |
|---|---|
| VCC | 3V3 only if the exact breakout supports it |
| GND | GND |
| SDA | GPIO 21 |
| SCL | GPIO 22 |

The DS3231 normally uses I²C address 0x68. Ensure SDA/SCL pull-ups are to 3.3 V. Some boards designed for 5 V systems pull I²C lines to their VCC; inspect the exact board before connecting to ESP32.

Disconnect USB before changing wiring. Never assume pin order from physical orientation; read the silkscreen. Do not connect 5 V I²C pull-ups directly to ESP32 GPIO.
