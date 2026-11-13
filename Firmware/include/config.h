#pragma once
#include <cstdint>
// Replace these placeholders before building
#define WIFI_SSID       "Ivar_hotspot"
#define WIFI_PASSWORD   "Ivarcao123"
#define API_BASE_URL    "http://10.234.209.20:8000"
#define DEVICE_ID       "ESP32-001"

constexpr uint8_t DHT_PIN = 5;  // GPIO pin connected to the DHT11 data pin
constexpr uint8_t DHT_TYPE = 11; // DHT11
constexpr uint8_t I2C_SDA_PIN = 21;  // GPIO pin connected to the I2C SDA line
constexpr uint8_t I2C_SCL_PIN = 22;  // GPIO pin connected to the I2C_SCL line

constexpr unsigned long SAMPLE_INTERVAL_MS = 5000;
constexpr unsigned long WIFI_RETRY_INTERVAL_MS = 10000;
constexpr unsigned long UPLOAD_INTERVAL_MS = 5000;

constexpr char QUEUE_FILE[] = "/readings.jsonl";
constexpr char API_READING_PATH[] = "/api/v1/readings";
