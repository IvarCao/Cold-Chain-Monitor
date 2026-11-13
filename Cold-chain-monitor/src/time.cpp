#include "mer_time.h"
#include <Wire.h>
#include <RTClib.h>
#include "config.h"

namespace {
    RTC_DS3231 rtc;
    bool rtcReady = false;
}

bool MerTime::begin() {
    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);

    if (!rtc.begin()) {
        rtcReady = false;
        Serial.println("ERROR: DS3231 not detected. Check power, SDA, and SCL.");
        return false;
    }

    rtcReady = true;
    Serial.println("DS3231 detected.");

    if (rtc.lostPower()) {
        Serial.println("WARNING: DS3231 lost power; setting compile-time fallback.");
        Serial.println("Set and verify the RTC to UTC before relying on timestamps.");
        rtc.adjust(DateTime(F(__DATE__), F(__TIME__)));
    }

    return true;
}

bool MerTime::getTimestamp(String& timestamp) {
    if (!rtcReady) return false;

    const DateTime now = rtc.now();
    char buffer[25];
    snprintf(buffer, sizeof(buffer), "%04d-%02d-%02dT%02d:%02d:%02dZ",
             now.year(), now.month(), now.day(),
             now.hour(), now.minute(), now.second());
    timestamp = String(buffer);
    return true;
}