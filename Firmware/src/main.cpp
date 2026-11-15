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
    unsigned long lastNtpAttemptMs = 0;

    bool rtcSynchronized = false;
    bool ntpAttempted = false;

    constexpr unsigned long NTP_RETRY_INTERVAL_MS = 30000;

    String createRecordId(const String& timestamp) {
        return String(DEVICE_ID) + "-" +
               String((uint32_t)esp_random(), HEX) +
               "-" + timestamp;
    }

    void takeSample() {
        float temperatureF = NAN;
        float humidityPercent = NAN;

        if (!Sensor::read(temperatureF, humidityPercent)) {
            return;
        }

        String timestamp;

        if (!MerTime::getTimestamp(timestamp)) {
            Serial.println(
                "ERROR: No valid RTC timestamp; reading was not queued."
            );
            return;
        }

        Reading reading;

        reading.recordId = createRecordId(timestamp);
        reading.deviceId = DEVICE_ID;
        reading.timestamp = timestamp;
        reading.temperatureF = temperatureF;
        reading.humidityPercent = humidityPercent;

        Serial.printf(
            "Reading: %.1f F | %.1f %% RH | %s\n",
            temperatureF,
            humidityPercent,
            timestamp.c_str()
        );

        // Save locally before attempting an upload.
        if (!Storage::enqueue(reading)) {
            Serial.println(
                "ERROR: Could not save reading to LittleFS."
            );
        }
    }

    void uploadOneQueuedReading() {
        if (millis() - lastUploadMs < UPLOAD_INTERVAL_MS) {
            return;
        }

        lastUploadMs = millis();

        if (!MerNetwork::isConnected()) {
            return;
        }

        Reading reading;

        if (!Storage::peek(reading)) {
            return;
        }

        if (MerApiClient::sendReading(reading)) {
            if (!Storage::removeFirst()) {
                Serial.println(
                    "WARNING: Could not remove acknowledged queue record; "
                    "it may be retried."
                );
            }
        }

        // Failed uploads remain queued for retry.
    }

    void synchronizeRtc() {
        if (rtcSynchronized) {
            return;
        }

        if (!MerNetwork::isConnected()) {
            return;
        }

        const unsigned long now = millis();

        if (ntpAttempted &&
            now - lastNtpAttemptMs < NTP_RETRY_INTERVAL_MS) {
            return;
        }

        ntpAttempted = true;
        lastNtpAttemptMs = now;

        Serial.println("Attempting RTC synchronization...");

        if (MerTime::syncWithNTP()) {
            rtcSynchronized = true;

            Serial.println(
                "RTC synchronization successful."
            );

            String timestamp;

            if (MerTime::getTimestamp(timestamp)) {
                Serial.println(
                    "Current UTC time: " + timestamp
                );
            }
        } else {
            Serial.println(
                "RTC synchronization failed. "
                "Will retry in 30 seconds."
            );
        }
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
    // Maintain the Wi-Fi connection.
    MerNetwork::update();

    // Synchronize the DS3231 after Wi-Fi connects.
    synchronizeRtc();

    const unsigned long now = millis();

    if (now - lastSampleMs >= SAMPLE_INTERVAL_MS) {
        lastSampleMs = now;
        takeSample();
    }

    uploadOneQueuedReading();

    delay(10);
}

