#pragma once
#include <Arduino.h>

namespace Sensor{
    void begin();
    bool read(float& temperatureC, float& humidityPercent);
}
