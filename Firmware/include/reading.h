#pragma once
#include <Arduino.h>

// One complete measurement set sent to the Raspberry Pi API
struct Reading {
    String recordId;
    String deviceId;
    String timestamp;       
    float temperatureF;
    float humidityPercent;
};