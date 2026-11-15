from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(main, "DEFAULT_MIN_TEMP_F", 35.6)
    monkeypatch.setattr(main, "DEFAULT_MAX_TEMP_F", 46.4)
    monkeypatch.setattr(main, "DEFAULT_EXCURSION_DURATION_SECONDS", 120)
    main.initialize_database()
    with TestClient(main.app) as test_client:
        yield test_client


def payload(record_id="rec-1", sequence=1, temperature=41.0, timestamp=None):
    return {
        "record_id": record_id,
        "device_id": "ESP32-001",
        "sequence": sequence,
        "timestamp": (timestamp or datetime.now(timezone.utc)).isoformat(),
        "temperature_f": temperature,
        "humidity_percent": 48.0,
    }


def post_sustained_out_of_range(client, prefix, start, temperature=48.2, duration_seconds=120, first_sequence=1):
    count = duration_seconds // 5 + 1
    for offset in range(count):
        seconds = offset * 5
        sequence = first_sequence + offset
        client.post("/api/v1/readings", json=payload(
            f"{prefix}-{sequence}", sequence, temperature, start + timedelta(seconds=seconds)
        ))
    return count


def test_health(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_reading_is_idempotent(client):
    body = payload()
    first = client.post("/api/v1/readings", json=body)
    second = client.post("/api/v1/readings", json=body)
    readings = client.get("/api/v1/readings").json()["readings"]
    assert first.status_code == second.status_code == 201
    assert first.json()["accepted"] is True
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert len(readings) == 1


def test_sequence_unique_per_device(client):
    first = payload("r1", 1)
    assert client.post("/api/v1/readings", json=first).json()["duplicate"] is False
    exact_retry = client.post("/api/v1/readings", json=first)
    assert exact_retry.status_code == 201
    assert exact_retry.json()["duplicate"] is True
    conflicting_sequence = client.post("/api/v1/readings", json=payload("r2", 1, temperature=42.8))
    assert conflicting_sequence.status_code == 409
    assert len(client.get("/api/v1/readings").json()["readings"]) == 1


def test_reused_record_id_with_different_payload_is_conflict(client):
    client.post("/api/v1/readings", json=payload("same-id", 1))
    conflict = client.post("/api/v1/readings", json=payload("same-id", 2, temperature=44.6))
    assert conflict.status_code == 409


def test_detects_gap_and_recovers_late_reading(client):
    client.post("/api/v1/readings", json=payload("r1", 1))
    client.post("/api/v1/readings", json=payload("r3", 3))
    gaps = client.get("/api/v1/gaps?status=open").json()["gaps"]
    assert [gap["sequence"] for gap in gaps] == [2]

    # Offline-buffered/out-of-order sequence 2 arrives after sequence 3.
    client.post("/api/v1/readings", json=payload("r2", 2))
    gaps = client.get("/api/v1/gaps").json()["gaps"]
    assert len(gaps) == 1
    assert gaps[0]["status"] == "recovered"
    assert gaps[0]["recovered_at"] is not None


def test_out_of_order_reading_does_not_create_false_gap(client):
    client.post("/api/v1/readings", json=payload("r1", 1))
    client.post("/api/v1/readings", json=payload("r3", 3))
    client.post("/api/v1/readings", json=payload("r2", 2))
    assert client.get("/api/v1/gaps?status=open").json()["gaps"] == []


def test_temperature_crossing_creates_one_active_alert(client):
    start = datetime.now(timezone.utc)
    post_sustained_out_of_range(client, "r", start, temperature=48.38)
    client.post("/api/v1/readings", json=payload("r26", 26, temperature=49.1, timestamp=start + timedelta(seconds=125)))
    alerts = client.get("/api/v1/alerts").json()["alerts"]
    assert len(alerts) == 1
    assert alerts[0]["status"] == "active"
    assert alerts[0]["alert_type"] == "above_maximum"
    assert alerts[0]["max_observed_f"] == 49.1


def test_in_range_reading_resolves_active_alert(client):
    start = datetime.now(timezone.utc)
    post_sustained_out_of_range(client, "r", start, temperature=33.8)
    client.post("/api/v1/readings", json=payload("r26", 26, temperature=41.0, timestamp=start + timedelta(seconds=125)))
    assert client.get("/api/v1/alerts").json()["alerts"] == []
    history = client.get("/api/v1/alerts?active_only=false").json()["alerts"]
    assert len(history) == 1
    assert history[0]["status"] == "resolved"
    assert history[0]["resolved_at"] is not None


def test_thresholds_are_configurable_and_validated(client):
    response = client.put("/api/v1/settings/thresholds?device_id=ESP32-001",
                          json={"minimum_f": 37.4, "maximum_f": 44.6})
    assert response.status_code == 200
    assert client.get("/api/v1/settings/thresholds?device_id=ESP32-001").json()["minimum_f"] == 37.4
    invalid = client.put("/api/v1/settings/thresholds?device_id=ESP32-001",
                         json={"minimum_f": 44.6, "maximum_f": 44.6})
    assert invalid.status_code == 422


def test_email_failure_is_queued_and_can_be_retried(client, monkeypatch):
    monkeypatch.setenv("ALERT_EMAIL_TO", "operator@example.test")

    def fail_send(*args, **kwargs):
        raise RuntimeError("SMTP temporarily unavailable")

    monkeypatch.setattr(main, "send_email", fail_send)
    start = datetime.now(timezone.utc)
    post_sustained_out_of_range(client, "hot", start, temperature=48.2)
    notification = client.get("/api/v1/notifications").json()["notifications"][0]
    assert notification["status"] == "pending"
    assert notification["attempts"] == 1
    assert "temporarily unavailable" in notification["last_error"]

    delivered = []
    monkeypatch.setattr(main, "send_email", lambda subject, body, recipient: delivered.append(recipient))
    retry = client.post("/api/v1/notifications/retry")
    assert retry.json() == {"sent": 1, "failed": 0}
    assert delivered == ["operator@example.test"]
    notification = client.get("/api/v1/notifications").json()["notifications"][0]
    assert notification["status"] == "sent"
    assert notification["attempts"] == 2


def test_missing_sequence_is_supported_for_legacy_firmware(client):
    body = payload()
    body.pop("sequence")
    response = client.post("/api/v1/readings", json=body)
    assert response.status_code == 201
    assert response.json()["accepted"] is True
    assert client.get("/api/v1/gaps").json()["gaps"] == []


def test_rejects_naive_timestamp(client):
    body = payload()
    body["timestamp"] = "2026-11-06T18:30:00"
    response = client.post("/api/v1/readings", json=body)
    assert response.status_code == 422


def test_device_liveness_is_reported(client):
    client.post("/api/v1/readings", json=payload("r1", 1))
    devices = client.get("/api/v1/devices").json()
    assert devices["stale_after_seconds"] == main.SAMPLE_INTERVAL_SECONDS * 3
    assert devices["devices"][0]["is_stale"] is False
    assert devices["devices"][0]["last_received"] is not None


def test_notification_is_queued_even_when_email_config_is_missing(client, monkeypatch):
    monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("ALERT_EMAIL_FROM", raising=False)
    start = datetime.now(timezone.utc)
    post_sustained_out_of_range(client, "hot", start, temperature=10.0)
    notification = client.get("/api/v1/notifications").json()["notifications"][0]
    assert notification["status"] == "pending"
    assert notification["attempts"] >= 1
    assert "ALERT_EMAIL_TO" in notification["last_error"]




def test_short_spike_does_not_create_alert(client):
    start = datetime.now(timezone.utc)
    client.post("/api/v1/readings", json=payload("short-1", 1, 48.2, start))
    client.post("/api/v1/readings", json=payload("short-2", 2, 48.2, start + timedelta(seconds=30)))
    assert client.get("/api/v1/alerts").json()["alerts"] == []
    assert client.get("/api/v1/notifications").json()["notifications"] == []


def test_alert_is_created_only_after_configured_duration(client):
    start = datetime.now(timezone.utc)
    for offset in range(24):
        client.post("/api/v1/readings", json=payload(f"dwell-{offset+1}", offset+1, 48.2, start + timedelta(seconds=offset*5)))
    assert client.get("/api/v1/alerts").json()["alerts"] == []
    client.post("/api/v1/readings", json=payload("dwell-25", 25, 48.2, start + timedelta(seconds=120)))
    alerts = client.get("/api/v1/alerts").json()["alerts"]
    assert len(alerts) == 1
    assert alerts[0]["started_at"] == start.isoformat().replace("+00:00", "Z")
    assert alerts[0]["confirmed_at"] == (start + timedelta(seconds=120)).isoformat().replace("+00:00", "Z")


def test_temperature_returning_to_range_resets_pending_timer(client):
    start = datetime.now(timezone.utc)
    client.post("/api/v1/readings", json=payload("reset-1", 1, 48.2, start))
    client.post("/api/v1/readings", json=payload("reset-2", 2, 41.0, start + timedelta(seconds=60)))
    client.post("/api/v1/readings", json=payload("reset-3", 3, 48.2, start + timedelta(seconds=65)))
    client.post("/api/v1/readings", json=payload("reset-4", 4, 48.2, start + timedelta(seconds=120)))
    assert client.get("/api/v1/alerts").json()["alerts"] == []


def test_large_timestamp_gap_does_not_restart_temperature_alert_timer(client):
    start = datetime.now(timezone.utc)
    client.post("/api/v1/readings", json=payload("gap-temp-1", 1, 48.2, start))
    client.post("/api/v1/readings", json=payload("gap-temp-2", 2, 48.2, start + timedelta(seconds=5)))
    client.post("/api/v1/readings", json=payload("gap-temp-3", 3, 48.2, start + timedelta(seconds=180)))
    client.post("/api/v1/readings", json=payload("gap-temp-4", 4, 48.2, start + timedelta(seconds=240)))
    assert len(client.get("/api/v1/alerts").json()["alerts"]) == 1


def test_threshold_settings_have_no_sample_gap_setting(client):
    response = client.put("/api/v1/settings/thresholds?device_id=ESP32-001", json={
        "minimum_f": 35.6, "maximum_f": 46.4,
        "excursion_duration_seconds": 90,
    })
    assert response.status_code == 200
    settings = client.get("/api/v1/settings/thresholds?device_id=ESP32-001").json()
    assert settings["excursion_duration_seconds"] == 90
    assert "max_sample_gap_seconds" not in settings


def test_delivery_snapshots_excursion_duration_settings(client):
    client.put("/api/v1/settings/thresholds?device_id=ESP32-001", json={
        "minimum_f": 35.6, "maximum_f": 46.4,
        "excursion_duration_seconds": 75,
    })
    started = client.post("/api/v1/deliveries/start?device_id=ESP32-001", json={"delivery_id": "DEL-RULES"})
    assert started.json()["excursion_duration_seconds"] == 75
    assert "max_sample_gap_seconds" not in started.json()


def test_delivery_report_uses_duration_even_across_timestamp_gaps(client):
    client.post("/api/v1/deliveries/start?device_id=ESP32-001", json={"delivery_id": "DEL-DATA-GAP"})
    start = datetime.now(timezone.utc)
    client.post("/api/v1/readings", json=payload("gap-a", 1, 48.2, start))
    client.post("/api/v1/readings", json=payload("gap-b", 2, 48.2, start + timedelta(seconds=5)))
    client.post("/api/v1/readings", json=payload("gap-c", 3, 48.2, start + timedelta(seconds=180)))
    client.post("/api/v1/readings", json=payload("gap-d", 4, 48.2, start + timedelta(seconds=185)))
    report = client.post("/api/v1/deliveries/DEL-DATA-GAP/complete", json={"queue_drained": True}).json()
    assert report["result"] == "FAIL"
    assert len(report["temperature_excursions"]) == 1
    assert report["temperature_excursions"][0]["reading_count"] == 4
    assert report["temperature_excursions"][0]["qualifies_for_failure"] is True


def test_delivery_report_uses_original_timestamps_for_out_of_order_upload(client):
    client.post("/api/v1/deliveries/start?device_id=ESP32-001", json={"delivery_id": "DEL-OFFLINE"})
    start = datetime.now(timezone.utc)
    # Upload in reverse order to model buffered records arriving out of order.
    count = 25
    for offset in reversed(range(count)):
        sequence = offset + 1
        client.post("/api/v1/readings", json=payload(
            f"offline-{sequence}", sequence, 48.2, start + timedelta(seconds=offset * 5)
        ))
    report = client.post("/api/v1/deliveries/DEL-OFFLINE/complete", json={"queue_drained": True}).json()
    assert report["result"] == "FAIL"
    assert report["temperature_excursions"][0]["duration_seconds"] == 120


def test_migrates_delivery_and_alert_tables_for_excursion_rules(tmp_path, monkeypatch):
    import sqlite3

    db_path = tmp_path / "previous_delivery_schema.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE readings (record_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, sequence INTEGER,
            delivery_id TEXT, timestamp TEXT NOT NULL, temperature_c REAL NOT NULL,
            humidity_percent REAL NOT NULL, received_at TEXT NOT NULL);
        CREATE TABLE device_state (device_id TEXT PRIMARY KEY, last_sequence INTEGER,
            last_reading_at TEXT, last_received_at TEXT, minimum_c REAL NOT NULL, maximum_c REAL NOT NULL);
        CREATE TABLE deliveries (delivery_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, status TEXT NOT NULL,
            started_at TEXT NOT NULL, completed_at TEXT, start_sequence INTEGER, end_sequence INTEGER,
            queue_drained_confirmed INTEGER NOT NULL DEFAULT 0, threshold_min_c REAL NOT NULL, threshold_max_c REAL NOT NULL);
        CREATE TABLE alerts (id INTEGER PRIMARY KEY, device_id TEXT NOT NULL, delivery_id TEXT,
            alert_type TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL, resolved_at TEXT, min_observed_c REAL NOT NULL,
            max_observed_c REAL NOT NULL, threshold_min_c REAL NOT NULL, threshold_max_c REAL NOT NULL);
    """)
    conn.execute("INSERT INTO readings VALUES ('legacy-r', 'ESP32-001', 1, NULL, '2026-01-01T00:00:00Z', 5.0, 45.0, '2026-01-01T00:00:01Z')")
    conn.execute("INSERT INTO device_state VALUES ('ESP32-001', 1, NULL, NULL, 2.0, 8.0)")
    conn.execute("INSERT INTO deliveries VALUES ('LEGACY-D', 'ESP32-001', 'completed', '2026-01-01T00:00:00Z', NULL, 1, 1, 1, 2.0, 8.0)")
    conn.execute("INSERT INTO alerts VALUES (1, 'ESP32-001', 'LEGACY-D', 'above_maximum', 'resolved', '2026-01-01T00:00:00Z', '2026-01-01T00:02:00Z', '2026-01-01T00:03:00Z', 9.0, 10.0, 2.0, 8.0)")
    conn.commit(); conn.close()

    main.initialize_database()
    conn = sqlite3.connect(db_path)
    delivery_columns = {row[1] for row in conn.execute("PRAGMA table_info(deliveries)")}
    device_columns = {row[1] for row in conn.execute("PRAGMA table_info(device_state)")}
    alert_columns = {row[1] for row in conn.execute("PRAGMA table_info(alerts)")}
    reading_columns = {row[1] for row in conn.execute("PRAGMA table_info(readings)")}
    assert {"excursion_duration_seconds", "threshold_min_f", "threshold_max_f"} <= delivery_columns
    assert {"excursion_duration_seconds", "minimum_f", "maximum_f"} <= device_columns
    assert "max_sample_gap_seconds" not in delivery_columns
    assert "max_sample_gap_seconds" not in device_columns
    assert {"confirmed_at", "min_observed_f", "max_observed_f"} <= alert_columns
    assert "temperature_f" in reading_columns
    assert conn.execute("SELECT temperature_f FROM readings WHERE record_id='legacy-r'").fetchone()[0] == 41.0
    assert conn.execute("SELECT minimum_f, maximum_f FROM device_state WHERE device_id='ESP32-001'").fetchone() == (35.6, 46.4)
    assert conn.execute("SELECT threshold_min_f, threshold_max_f FROM deliveries WHERE delivery_id='LEGACY-D'").fetchone() == (35.6, 46.4)
    assert conn.execute("SELECT min_observed_f, max_observed_f FROM alerts WHERE id=1").fetchone() == (48.2, 50.0)
    conn.close()


def test_migrates_original_starter_database_schema(tmp_path, monkeypatch):
    import sqlite3

    db_path = tmp_path / "legacy.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE readings (record_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, delivery_id TEXT, timestamp TEXT NOT NULL, temperature_c REAL NOT NULL, humidity_percent REAL NOT NULL, received_at TEXT NOT NULL)")
    conn.commit()
    conn.close()
    main.initialize_database()
    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(readings)")}
    assert "sequence" in columns
    conn.close()


def test_recovery_transition_queues_a_distinct_recovery_email(client, monkeypatch):
    monkeypatch.setenv("ALERT_EMAIL_TO", "operator@example.test")
    monkeypatch.setattr(main, "send_email", lambda *args, **kwargs: None)
    start = datetime.now(timezone.utc)
    count = post_sustained_out_of_range(client, "hot", start, temperature=48.56)
    client.post("/api/v1/readings", json=payload(f"normal-{count+1}", count + 1, temperature=41.0, timestamp=start + timedelta(seconds=125)))
    events = [n["event_type"] for n in client.get("/api/v1/notifications").json()["notifications"]]
    assert set(events) == {"temperature_alert", "temperature_recovered"}
    assert len(events) == 2


def test_extreme_sequence_jump_is_rejected_safely(client):
    client.post("/api/v1/readings", json=payload("r1", 1))
    response = client.post("/api/v1/readings", json=payload("r2", main.MAX_GAP_EXPANSION + 3))
    assert response.status_code == 409
    assert len(client.get("/api/v1/readings").json()["readings"]) == 1


def test_start_delivery_assigns_readings_and_generates_pass_report(client):
    started = client.post(
        "/api/v1/deliveries/start?device_id=ESP32-001",
        json={"delivery_id": "DEL-100"},
    )
    assert started.status_code == 201
    assert started.json()["result"] == "IN_PROGRESS"

    first = client.post("/api/v1/readings", json=payload("d100-1", 1, 39.2))
    second = client.post("/api/v1/readings", json=payload("d100-2", 2, 42.8))
    assert first.status_code == second.status_code == 201
    stored = client.get("/api/v1/readings").json()["readings"]
    assert [row["delivery_id"] for row in stored] == ["DEL-100", "DEL-100"]

    completed = client.post("/api/v1/deliveries/DEL-100/complete", json={"queue_drained": True})
    assert completed.status_code == 200
    report = completed.json()
    assert report["result"] == "PASS"
    assert report["reading_count"] == 2
    assert report["expected_readings"] == 2
    assert report["coverage_complete"] is True
    assert report["minimum_temperature_f"] == 39.2
    assert report["maximum_temperature_f"] == 42.8


def test_only_one_active_delivery_per_device(client):
    first = client.post("/api/v1/deliveries/start?device_id=ESP32-001", json={"delivery_id": "DEL-A"})
    second = client.post("/api/v1/deliveries/start?device_id=ESP32-001", json={"delivery_id": "DEL-B"})
    assert first.status_code == 201
    assert second.status_code == 409


def test_delivery_report_fails_after_sustained_out_of_range_readings(client):
    client.post("/api/v1/deliveries/start?device_id=ESP32-001", json={"delivery_id": "DEL-HOT"})
    start = datetime.now(timezone.utc)
    post_sustained_out_of_range(client, "hot-delivery", start, temperature=48.56)
    report = client.post("/api/v1/deliveries/DEL-HOT/complete", json={"queue_drained": True}).json()
    assert report["result"] == "FAIL"
    assert len(report["temperature_excursions"]) == 1
    assert report["temperature_excursions"][0]["duration_seconds"] == 120
    assert report["excursion_duration_seconds"] == 120


def test_single_brief_out_of_range_temperature_passes_delivery(client):
    client.post("/api/v1/deliveries/start?device_id=ESP32-001", json={"delivery_id": "DEL-SPIKE"})
    start = datetime.now(timezone.utc)
    client.post("/api/v1/readings", json=payload("spike-1", 1, 9.2, start))
    client.post("/api/v1/readings", json=payload("spike-2", 2, 41.0, start + timedelta(seconds=5)))
    report = client.post("/api/v1/deliveries/DEL-SPIKE/complete", json={"queue_drained": True}).json()
    assert report["result"] == "PASS"
    assert len(report["temperature_excursions"]) == 1
    assert report["temperature_excursions"][0]["qualifies_for_failure"] is False


def test_excursion_duration_boundary_and_recovery(client):
    start = datetime.now(timezone.utc)
    client.post("/api/v1/deliveries/start?device_id=ESP32-001", json={"delivery_id": "DEL-BOUNDARY"})
    for seq, seconds, temp in [(1, 0, 48.2), (2, 115, 48.2), (3, 120, 41.0), (4, 125, 48.2)]:
        assert client.post("/api/v1/readings", json=payload(f"boundary-{seq}", seq, temp, start + timedelta(seconds=seconds))).status_code == 201
    report = client.post("/api/v1/deliveries/DEL-BOUNDARY/complete", json={"queue_drained": True}).json()
    assert report["result"] == "PASS"
    assert [e["duration_seconds"] for e in report["temperature_excursions"]] == [115, 0]
    assert all(not e["qualifies_for_failure"] for e in report["temperature_excursions"])


def test_delivery_uses_snapshotted_duration_after_settings_change(client):
    start = datetime.now(timezone.utc)
    assert client.put("/api/v1/settings/thresholds?device_id=ESP32-001", json={"minimum_f": 35.6, "maximum_f": 46.4, "excursion_duration_seconds": 30}).status_code == 200
    client.post("/api/v1/deliveries/start?device_id=ESP32-001", json={"delivery_id": "DEL-SNAPSHOT"})
    assert client.put("/api/v1/settings/thresholds?device_id=ESP32-001", json={"minimum_f": 35.6, "maximum_f": 46.4, "excursion_duration_seconds": 120}).status_code == 200
    for seq, seconds in [(1, 0), (2, 30)]:
        client.post("/api/v1/readings", json=payload(f"snapshot-{seq}", seq, 48.2, start + timedelta(seconds=seconds)))
    report = client.post("/api/v1/deliveries/DEL-SNAPSHOT/complete", json={"queue_drained": True}).json()
    assert report["excursion_duration_seconds"] == 30
    assert report["result"] == "FAIL"


def test_delivery_report_passes_when_sequence_gap_unresolved(client):
    client.post("/api/v1/deliveries/start?device_id=ESP32-001", json={"delivery_id": "DEL-GAP"})
    client.post("/api/v1/readings", json=payload("gap-delivery-1", 1, 41.0))
    client.post("/api/v1/readings", json=payload("gap-delivery-3", 3, 41.0))
    report = client.post("/api/v1/deliveries/DEL-GAP/complete", json={"queue_drained": True}).json()
    assert report["result"] == "PASS"
    assert report["coverage_complete"] is False
    assert [gap["sequence"] for gap in report["open_gaps"]] == [2]


def test_recovered_offline_reading_allows_delivery_pass(client):
    client.post("/api/v1/deliveries/start?device_id=ESP32-001", json={"delivery_id": "DEL-RECOVER"})
    client.post("/api/v1/readings", json=payload("recover-1", 1, 41.0))
    client.post("/api/v1/readings", json=payload("recover-3", 3, 41.0))
    client.post("/api/v1/readings", json=payload("recover-2", 2, 41.0))
    report = client.post("/api/v1/deliveries/DEL-RECOVER/complete", json={"queue_drained": True}).json()
    assert report["result"] == "PASS"
    assert report["coverage_complete"] is True
    assert len(report["recovered_gaps"]) == 1


def test_completed_delivery_is_listed_and_has_detail_readings(client):
    client.post("/api/v1/deliveries/start?device_id=ESP32-001", json={"delivery_id": "DEL-LIST"})
    client.post("/api/v1/readings", json=payload("list-1", 1, 41.0))
    client.post("/api/v1/deliveries/DEL-LIST/complete", json={"queue_drained": True})
    listing = client.get("/api/v1/deliveries").json()["deliveries"]
    detail = client.get("/api/v1/deliveries/DEL-LIST").json()
    assert listing[0]["delivery_id"] == "DEL-LIST"
    assert detail["readings"][0]["record_id"] == "list-1"


def test_delivery_without_any_readings_passes_when_no_out_of_range_readings_exist(client):
    client.post("/api/v1/deliveries/start?device_id=ESP32-001", json={"delivery_id": "DEL-EMPTY"})
    report = client.post("/api/v1/deliveries/DEL-EMPTY/complete", json={"queue_drained": True}).json()
    assert report["result"] == "PASS"
    assert report["reading_count"] == 0
    assert report["coverage_complete"] is False


def test_delivery_id_cannot_be_reused(client):
    first = client.post("/api/v1/deliveries/start?device_id=ESP32-001", json={"delivery_id": "DEL-UNIQUE"})
    assert first.status_code == 201
    client.post("/api/v1/deliveries/DEL-UNIQUE/complete", json={"queue_drained": True})
    again = client.post("/api/v1/deliveries/start?device_id=ESP32-001", json={"delivery_id": "DEL-UNIQUE"})
    assert again.status_code == 409


def test_delivery_without_queue_drain_confirmation_cannot_pass(client):
    client.post("/api/v1/deliveries/start?device_id=ESP32-001", json={"delivery_id": "DEL-NOT-DRAINED"})
    client.post("/api/v1/readings", json=payload("not-drained-1", 1, 41.0))
    report = client.post(
        "/api/v1/deliveries/DEL-NOT-DRAINED/complete",
        json={"queue_drained": False},
    ).json()
    assert report["sequence_coverage_complete"] is True
    assert report["queue_drained_confirmed"] is False
    assert report["coverage_complete"] is False
    assert report["result"] == "PASS"


def test_dashboard_is_served_from_fastapi(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Meridian" in response.text


def test_dashboard_javascript_is_served_from_fastapi(client):
    response = client.get("/js/dashboard.js")
    assert response.status_code == 200
    assert "api/v1/health" in response.text


def test_delivery_report_download_is_pdf(client):
    client.post("/api/v1/deliveries/start?device_id=ESP32-001", json={"delivery_id": "DEL-PDF"})
    client.post("/api/v1/readings", json=payload("pdf-1", 1, 41.0))
    client.post("/api/v1/deliveries/DEL-PDF/complete", json={"queue_drained": True})
    response = client.get("/api/v1/deliveries/DEL-PDF/report.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")
    assert "meridian-delivery-DEL-PDF.pdf" in response.headers["content-disposition"]
