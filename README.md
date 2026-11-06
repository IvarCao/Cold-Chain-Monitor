# Cold-Chain-Monitor

### Embedded Temperature Monitoring and Data Logging for Cold-Chain Deliveries

## Overview

The Cold-Chain Monitor is an embedded temperature-monitoring system designed to demonstrate how environmental data can be collected, timestamped, stored, and transmitted during temperature-sensitive deliveries.

The system uses an **ESP32 microcontroller** to collect temperature and humidity readings from a DHT11 sensor at five-second intervals. Readings are timestamped using a DS3231 real-time clock (RTC) and transmitted over Wi-Fi to a Raspberry Pi running a Python-based backend.

When network connectivity is unavailable, the ESP32 temporarily stores readings in its internal flash memory and attempts to transmit them once the connection is restored.

A locally hosted web dashboard allows users to view current sensor readings, historical temperature data, device status, and recent monitoring activity.

This project is intended as an educational embedded-systems and software-engineering prototype, not a certified pharmaceutical monitoring device.

## Project Goals

- Develop embedded firmware using C++ on the ESP32.
- Interface with digital environmental sensors and an RTC module.
- Implement periodic sensor sampling and timestamped data collection.
- Transmit sensor readings to a Raspberry Pi over Wi-Fi.
- Implement local data buffering for temporary network outages.
- Prevent duplicate records when buffered readings are retransmitted.
- Develop a Python backend with persistent SQLite storage.
- Build a local web dashboard for monitoring and visualizing collected data.

## System Architecture

```text
         SENSOR LAYER
    ┌─────────────────────┐
    │       DHT11         │
    │ Temperature/Humidity│
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │       DS3231        │
    │   Real-Time Clock   │
    └──────────┬──────────┘
               │ I2C / GPIO
               ▼
         ESP32 FIRMWARE
    ┌─────────────────────┐
    │ Sensor Acquisition  │
    │ Timestamp Generation│
    │ Data Processing     │
    │ LittleFS Data Queue │
    └──────────┬──────────┘
               │
          Wi-Fi / HTTP
               │
               ▼
       RASPBERRY PI 4
    ┌─────────────────────┐
    │ Python / FastAPI    │
    │ REST API            │
    │ SQLite Database     │
    └──────────┬──────────┘
               │
               ▼
        LOCAL DASHBOARD
    ┌─────────────────────┐
    │ HTML / CSS / JS     │
    │ Temperature History │
    │ Device Status       │
    │ Recent Readings     │
    └─────────────────────┘
```

## Hardware Components

| Component | Purpose |
|---|---|
| ESP32 DevKitC | Main microcontroller and Wi-Fi communication |
| DHT11 Sensor | Temperature and humidity measurements |
| DS3231 RTC | Maintains wall-clock timestamps |
| Raspberry Pi 4 | Hosts the backend and dashboard |
| Jumper Wires | Hardware connections |

### Pin Configuration

| Component | ESP32 Pin |
|---|---|
| DHT11 Data | GPIO 4 |
| DS3231 SDA | GPIO 21 |
| DS3231 SCL | GPIO 22 |
| Sensor Power | 3.3V |
| Ground | GND |

## Software and Technologies

**Embedded Firmware**
- C++
- Arduino Framework
- PlatformIO
- ESP32
- Adafruit DHT Sensor Library
- RTClib
- ArduinoJson
- LittleFS

**Backend**
- Python
- FastAPI
- SQLite
- Uvicorn
- Pytest

**Frontend**
- HTML
- CSS
- JavaScript
- Chart.js

**Development Tools**
- Visual Studio Code
- PlatformIO IDE
- Git and GitHub
- Raspberry Pi OS

## Key Features

### 1. Periodic Sensor Monitoring

The ESP32 reads temperature and humidity from the DHT11 sensor every five seconds. Each valid reading is associated with a timestamp from the DS3231 RTC.

### 2. Offline Data Buffering

The firmware uses the ESP32's internal flash storage through LittleFS to temporarily queue readings when Wi-Fi or backend connectivity is unavailable.

Once connectivity is restored, the ESP32 attempts to transmit the stored readings to the backend.

### 3. Reliable Data Transmission

Each reading receives a unique record identifier. The backend uses this identifier to prevent duplicate database entries when the ESP32 retries a transmission.

Buffered readings are retained until the backend acknowledges successful receipt.

### 4. REST API and Data Storage

The Raspberry Pi hosts a FastAPI application that receives sensor data and stores it in an SQLite database.

Current API endpoints:

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/v1/health` | Checks backend availability |
| POST | `/api/v1/readings` | Receives a sensor reading |
| GET | `/api/v1/readings` | Retrieves historical readings |
| GET | `/api/v1/devices` | Retrieves registered devices |

### 5. Local Monitoring Dashboard

The dashboard provides a browser-based interface for viewing:
- Latest temperature and humidity readings.
- Historical temperature trends.
- Device information and status.
- Total recorded measurements.
- Recent sensor readings.

## Repository Structure

```text
meridian-cold-chain-monitor/
│
├── firmware/
│   ├── platformio.ini
│   └── src/
│       ├── config.h
│       └── main.cpp
│
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   └── main.py
│   ├── tests/
│   │   └── test_api.py
│   ├── requirements.txt
│   └── requirements-dev.txt
│
├── frontend/
│   ├── index.html
│   ├── css/
│   │   └── style.css
│   └── js/
│       └── dashboard.js
│
├── hardware/
│   ├── README.md
│   └── pinout.md
│
├── documentation/
│   ├── system_architecture.md
│   ├── api_documentation.md
│   ├── testing_plan.md
│   └── troubleshooting.md
│
├── scripts/
│   ├── setup_pi.sh
│   ├── start_backend.sh
│   └── backup_database.sh
│
├── .github/
│   └── workflows/
│       └── backend-tests.yml
│
├── .gitignore
├── docker-compose.yml
├── LICENSE
└── README.md
```

## Getting Started

### Prerequisites

Before setting up the project, make sure you have:

- ESP32 development board.
- DHT11 temperature and humidity sensor.
- DS3231 RTC module.
- Raspberry Pi 4 running Raspberry Pi OS.
- A shared local Wi-Fi network.
- Visual Studio Code with PlatformIO installed.
- Python 3.10 or newer.
- Git installed.

### 1. Clone the Repository

```bash
git clone https://github.com/YOUR-USERNAME/meridian-cold-chain-monitor.git
cd meridian-cold-chain-monitor
```

Replace `YOUR-USERNAME` with your GitHub username.

### 2. Configure the ESP32

Open the `firmware` directory in Visual Studio Code using PlatformIO.

Edit `firmware/src/config.h` and configure:

- Your Wi-Fi network name.
- Your Wi-Fi password.
- Your Raspberry Pi's local IP address.
- A unique device identifier.

Build and upload the firmware using PlatformIO.

### 3. Set Up the Raspberry Pi Backend

On the Raspberry Pi, clone the repository and navigate to the project directory.

Create and activate a Python virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install the backend dependencies:

```bash
pip install -r backend/requirements.txt
```

Start the backend:

```bash
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
```

The backend should now be accessible on your local network.

### 4. Access the Dashboard

Open a browser on a device connected to the same local network.

Navigate to:

```text
http://RASPBERRY_PI_IP:8000/
```

Replace `RASPBERRY_PI_IP` with your Raspberry Pi's local IP address.

Additional endpoints:

```text
http://RASPBERRY_PI_IP:8000/docs
http://RASPBERRY_PI_IP:8000/api/v1/health
```

The first address opens the monitoring dashboard, while `/docs` provides the interactive API documentation.

## Running Tests

The backend includes automated tests for API functionality and duplicate-reading prevention.

Install development dependencies:

```bash
pip install -r backend/requirements-dev.txt
```

Run the test suite:

```bash
pytest backend/tests/
```

## Current Development Status

| Feature | Status |
|---|---|
| ESP32 firmware foundation | Initial implementation |
| DHT11 sensor integration | Pending hardware validation |
| DS3231 RTC integration | Pending hardware validation |
| Five-second sampling interval | Implemented in firmware |
| Wi-Fi communication | Implemented in firmware |
| LittleFS offline buffering | Implemented in firmware; hardware validation pending |
| FastAPI backend | Initial implementation |
| SQLite data storage | Initial implementation |
| Duplicate-reading prevention | Implemented in backend |
| Local web dashboard | Initial implementation |
| End-to-end hardware testing | Pending |
