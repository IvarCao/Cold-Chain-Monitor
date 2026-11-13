#include "mer_api_client.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include "config.h"
#include "mer_network.h"

bool MerApiClient::sendReading(const Reading& reading) {
    if (!MerNetwork::isConnected()) return false;

    JsonDocument doc;
    doc["record_id"] = reading.recordId;
    doc["device_id"] = reading.deviceId;
    doc["timestamp"] = reading.timestamp;
    doc["temperature_f"] = reading.temperatureF;
    doc["humidity_percent"] = reading.humidityPercent;

    String payload;
    serializeJson(doc, payload);

    WiFiClient client;
    HTTPClient http;
    const String url = String(API_BASE_URL) + API_READING_PATH;

    if (!http.begin(client, url)) {
        Serial.println("ERROR: Could not initialize HTTP connection.");
        return false;
    }

    http.addHeader("Content-Type", "application/json");
    http.setTimeout(5000);
    const int statusCode = http.POST(payload);
    const String response = http.getString();
    http.end();

    // FastAPI returns 201 for a newly inserted reading. A duplicate record
    // is also accepted and returns 201, so it is safe to remove from the queue.
    if (statusCode == 200 || statusCode == 201) {
        Serial.printf("API accepted record %s. Response: %s\n",
                      reading.recordId.c_str(), response.c_str());
        return true;
    }

    Serial.printf("API upload failed (HTTP %d): %s\n",
                  statusCode, response.c_str());
    return false;
}
