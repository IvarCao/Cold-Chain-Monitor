#pragma once
#include <Arduino.h>

namespace MerTime {
    bool begin();
    bool getTimestamp(String& timestamp);
}
