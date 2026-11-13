#include "sensor.h"
#include <DHT.h>
#include "config.h"

namespace {
    DHT dht(DHT_PIN, DHT_TYPE);
}

void Sensor::begin() {
    dht.begin();
    Serial.println("DHT11 sensor initialized.");
}

bool Sensor::read(float& temperaturef, float& humidityPercent) {
    // DHT11 returns NaN when a measurement fails.
    humidityPercent = dht.readHumidity();
    temperaturef = (dht.readTemperature() * 1.8f) + 32.0f;

    if (isnan(temperaturef) || isnan(humidityPercent)) {
        Serial.println("ERROR: DHT11 read failed.");
        return false;
    }

    if (humidityPercent < 0.0f || humidityPercent > 100.0f) {
        Serial.println("ERROR: DHT11 returned an invalid humidity value.");
        return false;
    }

    return true;
}
