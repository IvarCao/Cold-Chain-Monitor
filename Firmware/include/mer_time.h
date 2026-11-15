#pragma once
#include <Arduino.h>

namespace MerTime {
    // Initialize the DS3231 RTC.
    bool begin();
    // Synchronize the DS3231 with NTP over Wi-Fi.
    bool syncWithNTP();
    // Return a valid UTC timestamp.
    // Example: 2026-11-15T19:40:00Z
    bool getTimestamp(String& timestamp);
}

