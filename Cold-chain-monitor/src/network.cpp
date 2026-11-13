#include "mer_network.h"
#include <WiFi.h>
#include "config.h"

namespace {
    unsigned long lastWiFiAttemptMs = 0;
}

void MerNetwork::begin() {
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID , WIFI_PASSWORD);
    lastWiFiAttemptMs = millis();
    Serial.printf("Connecting to Wi-Fi network: %s\n", WIFI_SSID);
}

void MerNetwork::update() {
    if (WiFi.status() == WL_CONNECTED) return;
    if (millis() - lastWiFiAttemptMs < WIFI_RETRY_INTERVAL_MS) return;

    lastWiFiAttemptMs = millis();
    Serial.println("Retrying Wi-Fi connection...");
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

bool MerNetwork::isConnected() {
    return WiFi.status() == WL_CONNECTED;
}