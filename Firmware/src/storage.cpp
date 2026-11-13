#include "storage.h"
#include <LittleFS.h>
#include <ArduinoJson.h>
#include "config.h"

namespace {
    bool filesystemReady = false;
    constexpr char TEMP_QUEUE_FILE[] = "/readings.tmp";
    constexpr char BACKUP_QUEUE_FILE[] = "/readings.bak";

    bool deserializeReading(const String& line, Reading& reading) {
        JsonDocument doc;
        if (deserializeJson(doc, line)) return false;

        reading.recordId = doc["record_id"].as<String>();
        reading.deviceId = doc["device_id"].as<String>();
        reading.timestamp = doc["timestamp"].as<String>();
        reading.temperatureF = doc["temperature_f"] | NAN;
        reading.humidityPercent = doc["humidity_percent"] | NAN;

        return reading.recordId.length() > 0 &&
               reading.deviceId.length() > 0 &&
               reading.timestamp.length() > 0 &&
               !isnan(reading.temperatureF) &&
               !isnan(reading.humidityPercent);
    }
}

bool Storage::begin() {
    // false prevents an automatic format that could erase queued readings.
    filesystemReady = LittleFS.begin(false);
    if (filesystemReady) {
        Serial.println("LittleFS mounted.");
    } else {
        Serial.println("ERROR: LittleFS mount failed. Filesystem was not formatted.");
    }
    return filesystemReady;
}

bool Storage::enqueue(const Reading& reading) {
    if (!filesystemReady) return false;

    File file = LittleFS.open(QUEUE_FILE, FILE_APPEND);
    if (!file) {
        Serial.println("ERROR: Could not open queue for append.");
        return false;
    }

    JsonDocument doc;
    doc["record_id"] = reading.recordId;
    doc["device_id"] = reading.deviceId;
    doc["timestamp"] = reading.timestamp;
    doc["temperature_f"] = reading.temperatureF;
    doc["humidity_percent"] = reading.humidityPercent;

    const size_t written = serializeJson(doc, file);
    file.print('\n');
    file.flush();
    file.close();
    return written > 0;
}

bool Storage::hasPending() {
    return filesystemReady && LittleFS.exists(QUEUE_FILE);
}

bool Storage::peek(Reading& reading) {
    if (!hasPending()) return false;

    File file = LittleFS.open(QUEUE_FILE, FILE_READ);
    if (!file) return false;

    String line = file.readStringUntil('\n');
    file.close();
    line.trim();
    if (line.isEmpty()) return false;

    if (!deserializeReading(line, reading)) {
        Serial.println("ERROR: Invalid first queue record; preserving it for inspection.");
        return false;
    }
    return true;
}

bool Storage::removeFirst() {
    if (!hasPending()) return false;

    File input = LittleFS.open(QUEUE_FILE, FILE_READ);
    if (!input) return false;

    // Consume exactly one line; retain every remaining byte unchanged.
    String firstLine = input.readStringUntil('\n');
    String remainder = input.readString();
    input.close();
    if (firstLine.length() == 0) return false;

    File output = LittleFS.open(TEMP_QUEUE_FILE, FILE_WRITE);
    if (!output) return false;
    const size_t written = output.print(remainder);
    output.flush();
    output.close();
    if (written != remainder.length()) {
        LittleFS.remove(TEMP_QUEUE_FILE);
        return false;
    }

    // Keep a backup until the replacement queue is safely in place.
    LittleFS.remove(BACKUP_QUEUE_FILE);
    if (!LittleFS.rename(QUEUE_FILE, BACKUP_QUEUE_FILE)) {
        LittleFS.remove(TEMP_QUEUE_FILE);
        return false;
    }

    if (!LittleFS.rename(TEMP_QUEUE_FILE, QUEUE_FILE)) {
        LittleFS.rename(BACKUP_QUEUE_FILE, QUEUE_FILE);
        return false;
    }

    LittleFS.remove(BACKUP_QUEUE_FILE);
    return true;
}

bool Storage::clearQueue() {
    if (!filesystemReady) return false;

    if (LittleFS.exists(QUEUE_FILE)) {
        if (!LittleFS.remove(QUEUE_FILE)) {
            Serial.println("ERROR: Could not delete queue file.");
            return false;
        }
    }

    Serial.println("Queue cleared.");
    return true;
}
