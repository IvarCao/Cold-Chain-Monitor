#include "mer_time.h"
#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <RTClib.h>
#include <time.h>
#include "config.h"

namespace {
    RTC_DS3231 rtc;

    bool rtcReady = false;
    bool rtcTimeValid = false;
}

bool MerTime::begin() {
    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);

    if (!rtc.begin()) {
        rtcReady = false;
        rtcTimeValid = false;

        Serial.println(
            "ERROR: DS3231 not detected. Check power, SDA, and SCL."
        );

        return false;
    }

    rtcReady = true;

    // If the DS3231 lost power, its time may be incorrect.
    rtcTimeValid = !rtc.lostPower();

    Serial.println("DS3231 detected.");

    if (!rtcTimeValid) {
        Serial.println(
            "WARNING: RTC lost power. Waiting for NTP synchronization."
        );
    } else {
        Serial.println(
            "RTC retained its time from the previous power cycle."
        );
    }

    return true;
}

bool MerTime::syncWithNTP() {
    if (!rtcReady) {
        Serial.println("ERROR: RTC is not initialized.");
        return false;
    }

    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("ERROR: Wi-Fi not connected.");
        return false;
    }

    // Configure NTP synchronization.
    // Both timezone offsets are zero, so time is UTC.
    configTime(
        0,
        0,
        "pool.ntp.org",
        "time.google.com"
    );

    struct tm timeInfo;

    // Wait up to 10 seconds for valid system time.
    if (!getLocalTime(&timeInfo, 10000)) {
        Serial.println("ERROR: NTP synchronization timed out.");
        return false;
    }

    // Unix time is timezone-independent.
    time_t unixTime = time(nullptr);

    if (unixTime < 1700000000) {
        Serial.println("ERROR: Invalid NTP timestamp.");
        return false;
    }

    // Store UTC time in the DS3231.
    rtc.adjust(DateTime((uint32_t)unixTime));

    rtcTimeValid = true;

    Serial.println("DS3231 synchronized with NTP (UTC).");

    return true;
}

bool MerTime::getTimestamp(String& timestamp) {
    if (!rtcReady || !rtcTimeValid) {
        return false;
    }

    const DateTime now = rtc.now();

    char buffer[25];

    snprintf(
        buffer,
        sizeof(buffer),
        "%04d-%02d-%02dT%02d:%02d:%02dZ",
        now.year(),
        now.month(),
        now.day(),
        now.hour(),
        now.minute(),
        now.second()
    );

    timestamp = String(buffer);

    return true;
}
