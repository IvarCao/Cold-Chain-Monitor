#include <Arduino.h>
#include <esp_system.h>
#include "config.h"
#include "reading.h"
#include "sensor.h"
#include "mer_time.h"
#include "storage.h"
#include "mer_network.h"
#include "mer_api_client.h"

namespace {
    unsigned long lastSampleMs = 0;
    unsigned long lastUploadMs = 0;

    String createRecordId(const String& timestamp) {
        // Random suffix avoids reusing an ID after an ESP32 reboot.
        return String(DEVICE_ID) + "-" + String((uint32_t)esp_random(), HEX) + "-" + timestamp;
    }

    void takeSample() {
        float temperatureF = NAN;
        float humidityPercent = NAN;
        if (!Sensor::read(temperatureF, humidityPercent)) return;

        String timestamp;
        if (!MerTime::getTimestamp(timestamp)) {
            Serial.println("ERROR: No valid RTC timestamp; reading was not queued.");
            return;
        }

        Reading reading;
        reading.recordId = createRecordId(timestamp);
        reading.deviceId = DEVICE_ID;
        reading.timestamp = timestamp;
        reading.temperatureF = temperatureF;
        reading.humidityPercent = humidityPercent;

        Serial.printf("Reading: %.1f F | %.1f %% RH | %s\n",
                      temperatureF, humidityPercent, timestamp.c_str());

        // Persist first. This prevents a successful live send from being the
        // only copy of a reading if the device loses power unexpectedly.
        if (!Storage::enqueue(reading)) {
            Serial.println("ERROR: Could not save reading to LittleFS.");
        }
    }

    void uploadOneQueuedReading() {
        if (millis() - lastUploadMs < UPLOAD_INTERVAL_MS) return;
        lastUploadMs = millis();

        if (!MerNetwork::isConnected()) return;

        Reading reading;
        if (!Storage::peek(reading)) return;

        if (MerApiClient::sendReading(reading)) {
            if (!Storage::removeFirst()) {
                Serial.println("WARNING: Could not remove acknowledged queue record; it may be retried.");
            }
        }
        // On failure, leave the record at the head of the queue for retry.
    }
}

void setup() {
    Serial.begin(115200);
    delay(500);
    Serial.println("\nCold-Chain Monitor starting...");

    Sensor::begin();
    MerTime::begin();
    Storage::begin();
    MerNetwork::begin();

    Serial.println("Startup complete.");
}

void loop() {
    const unsigned long now = millis();

    if (now - lastSampleMs >= SAMPLE_INTERVAL_MS) {
        lastSampleMs = now;
        takeSample();
    }

    MerNetwork::update();
    uploadOneQueuedReading();
    delay(10);
}
