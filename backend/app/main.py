"""Meridian local API: idempotent ingestion, gap tracking and temperature alerts."""
from __future__ import annotations

import asyncio
import io
import os
import smtplib
import sqlite3
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

ROOT_DIR = Path(__file__).resolve().parents[2]
FRONTEND_DIR = ROOT_DIR / "frontend"
DEFAULT_DB = ROOT_DIR / "backend" / "data" / "meridian.sqlite3"
DB_PATH = Path(os.getenv("MERIDIAN_DB_PATH", str(DEFAULT_DB)))
SAMPLE_INTERVAL_SECONDS = int(os.getenv("MERIDIAN_SAMPLE_INTERVAL_SECONDS", "5"))
MAX_GAP_EXPANSION = 10000  # Prevent a corrupt counter from causing an unbounded insert loop.
DEFAULT_MIN_TEMP_F = float(os.getenv("MERIDIAN_MIN_TEMP_F", "35"))
DEFAULT_MAX_TEMP_F = float(os.getenv("MERIDIAN_MAX_TEMP_F", "45"))
DEFAULT_EXCURSION_DURATION_SECONDS = int(os.getenv("MERIDIAN_EXCURSION_DURATION_SECONDS", "120"))

async def notification_worker():
    while True:
        await asyncio.sleep(60)
        await asyncio.to_thread(process_notification_queue)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    initialize_database()
    worker = asyncio.create_task(notification_worker())
    try:
        yield
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


app = FastAPI(
    lifespan=lifespan,
    title="Meridian Cold-Chain Monitor API",
    version="0.4.0",
    description="Local API for ESP32 environmental readings, coverage gaps and alerts.",
)


class ReadingIn(BaseModel):
    record_id: str = Field(min_length=1, max_length=150)
    device_id: str = Field(min_length=1, max_length=100)
    timestamp: datetime
    temperature_f: float = Field(ge=-40, le=176)
    humidity_percent: float = Field(ge=0, le=100)
    sequence: Optional[int] = Field(default=None, ge=0, le=4294967295)
    delivery_id: Optional[str] = Field(default=None, max_length=100)

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone, preferably UTC")
        return value.astimezone(timezone.utc)


class ThresholdUpdate(BaseModel):
    minimum_f: float = Field(ge=-40, le=176)
    maximum_f: float = Field(ge=-40, le=176)
    excursion_duration_seconds: int = Field(default=DEFAULT_EXCURSION_DURATION_SECONDS, ge=1, le=86400)

    @field_validator("maximum_f")
    @classmethod
    def maximum_must_exceed_minimum(cls, value: float, info):
        minimum = info.data.get("minimum_f")
        if minimum is not None and value <= minimum:
            raise ValueError("maximum_f must be greater than minimum_f")
        return value


class DeliveryStart(BaseModel):
    delivery_id: str = Field(min_length=1, max_length=100)


class DeliveryComplete(BaseModel):
    queue_drained: bool


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize_database() -> None:
    with closing(get_connection()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS readings (
                record_id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                sequence INTEGER,
                delivery_id TEXT,
                timestamp TEXT NOT NULL,
                temperature_f REAL NOT NULL,
                humidity_percent REAL NOT NULL,
                received_at TEXT NOT NULL,
                UNIQUE(device_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS idx_readings_timestamp ON readings(timestamp);
            CREATE INDEX IF NOT EXISTS idx_readings_device_timestamp ON readings(device_id, timestamp);

            CREATE TABLE IF NOT EXISTS deliveries (
                delivery_id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                started_at TEXT NOT NULL,
                completed_at TEXT,
                start_sequence INTEGER,
                end_sequence INTEGER,
                queue_drained_confirmed INTEGER NOT NULL DEFAULT 0,
                threshold_min_f REAL NOT NULL,
                threshold_max_f REAL NOT NULL,
                excursion_duration_seconds INTEGER NOT NULL DEFAULT 120
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_delivery_per_device
                ON deliveries(device_id) WHERE status='active';

            CREATE TABLE IF NOT EXISTS device_state (
                device_id TEXT PRIMARY KEY,
                last_sequence INTEGER,
                last_reading_at TEXT,
                last_received_at TEXT,
                minimum_f REAL NOT NULL DEFAULT 35.6,
                maximum_f REAL NOT NULL DEFAULT 46.4,
                excursion_duration_seconds INTEGER NOT NULL DEFAULT 120
            );
            CREATE TABLE IF NOT EXISTS data_gaps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                detected_at TEXT NOT NULL,
                recovered_at TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                UNIQUE(device_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                delivery_id TEXT,
                alert_type TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                last_observed_at TEXT NOT NULL,
                resolved_at TEXT,
                min_observed_f REAL NOT NULL,
                max_observed_f REAL NOT NULL,
                threshold_min_f REAL NOT NULL,
                threshold_max_f REAL NOT NULL,
                confirmed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS excursion_state (
                device_id TEXT PRIMARY KEY,
                pending_started_at TEXT,
                last_observed_at TEXT,
                delivery_id TEXT,
                alert_type TEXT,
                min_observed_f REAL,
                max_observed_f REAL,
                threshold_min_f REAL,
                threshold_max_f REAL
            );
            CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status, device_id);
            CREATE TABLE IF NOT EXISTS notification_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT NOT NULL UNIQUE,
                alert_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                recipient TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                FOREIGN KEY(alert_id) REFERENCES alerts(id)
            );
            """
        )
        # Convert existing Celsius databases in place to the Fahrenheit schema.
        # Renaming preserves relationships/indexes; values are converted exactly once.
        legacy_temperature_columns = {
            "readings": {"temperature_c": "temperature_f"},
            "deliveries": {"threshold_min_c": "threshold_min_f", "threshold_max_c": "threshold_max_f"},
            "device_state": {"minimum_c": "minimum_f", "maximum_c": "maximum_f"},
            "alerts": {"min_observed_c": "min_observed_f", "max_observed_c": "max_observed_f",
                        "threshold_min_c": "threshold_min_f", "threshold_max_c": "threshold_max_f"},
            "excursion_state": {"min_observed_c": "min_observed_f", "max_observed_c": "max_observed_f",
                                "threshold_min_c": "threshold_min_f", "threshold_max_c": "threshold_max_f"},
        }
        for table, columns in legacy_temperature_columns.items():
            existing_columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for old_name, new_name in columns.items():
                if old_name in existing_columns and new_name not in existing_columns:
                    conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old_name} TO {new_name}")
                    conn.execute(f"UPDATE {table} SET {new_name} = ({new_name} * 9.0 / 5.0) + 32.0")
                    existing_columns.remove(old_name)
                    existing_columns.add(new_name)

        # Migrate databases created by the original starter backend.
        reading_columns = {row["name"] for row in conn.execute("PRAGMA table_info(readings)").fetchall()}
        if "sequence" not in reading_columns:
            conn.execute("ALTER TABLE readings ADD COLUMN sequence INTEGER")
        device_columns = {row["name"] for row in conn.execute("PRAGMA table_info(device_state)").fetchall()}
        if "last_received_at" not in device_columns:
            conn.execute("ALTER TABLE device_state ADD COLUMN last_received_at TEXT")
        delivery_columns = {row["name"] for row in conn.execute("PRAGMA table_info(deliveries)").fetchall()}
        if "queue_drained_confirmed" not in delivery_columns:
            conn.execute("ALTER TABLE deliveries ADD COLUMN queue_drained_confirmed INTEGER NOT NULL DEFAULT 0")
        for column, declaration in (
            ("excursion_duration_seconds", "INTEGER NOT NULL DEFAULT 120"),
        ):
            if column not in delivery_columns:
                conn.execute(f"ALTER TABLE deliveries ADD COLUMN {column} {declaration}")
        device_columns = {row["name"] for row in conn.execute("PRAGMA table_info(device_state)").fetchall()}
        if "excursion_duration_seconds" not in device_columns:
            conn.execute("ALTER TABLE device_state ADD COLUMN excursion_duration_seconds INTEGER NOT NULL DEFAULT 120")
        alert_columns = {row["name"] for row in conn.execute("PRAGMA table_info(alerts)").fetchall()}
        if "confirmed_at" not in alert_columns:
            conn.execute("ALTER TABLE alerts ADD COLUMN confirmed_at TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_readings_device_sequence "
            "ON readings(device_id, sequence) WHERE sequence IS NOT NULL"
        )
        conn.commit()


def ensure_device(conn: sqlite3.Connection, device_id: str) -> sqlite3.Row:
    conn.execute(
        "INSERT OR IGNORE INTO device_state(device_id, minimum_f, maximum_f, excursion_duration_seconds) VALUES (?, ?, ?, ?)",
        (device_id, DEFAULT_MIN_TEMP_F, DEFAULT_MAX_TEMP_F, DEFAULT_EXCURSION_DURATION_SECONDS),
    )
    return conn.execute("SELECT * FROM device_state WHERE device_id=?", (device_id,)).fetchone()


def queue_notification(conn: sqlite3.Connection, alert: sqlite3.Row, event_type: str, temperature: float) -> None:
    recipient = os.getenv("ALERT_EMAIL_TO", "").strip()
    dedupe_key = f"alert:{alert['id']}:{event_type}"
    subject = f"Meridian {event_type.replace('_', ' ').upper()}: {alert['device_id']}"
    body = (
        f"Meridian cold-chain monitoring notification\n\n"
        f"Event: {event_type}\nDevice: {alert['device_id']}\n"
        f"Delivery: {alert['delivery_id'] or 'not specified'}\n"
        f"Observed temperature: {temperature:.2f} °F\n"
        f"Configured range: {alert['threshold_min_f']:.2f} to {alert['threshold_max_f']:.2f} °F\n"
        f"Started: {alert['started_at']}\n"
        f"Confirmed: {alert['confirmed_at'] or 'pending'}\n"
        f"Observed at: {alert['last_observed_at']}\n\n"
        "This is an educational prototype notification, not a validated safety determination."
    )
    conn.execute(
        "INSERT OR IGNORE INTO notification_queue "
        "(dedupe_key, alert_id, event_type, recipient, subject, body, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (dedupe_key, alert["id"], event_type, recipient, subject, body, utc_now()),
    )


def parse_reading_time(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Reading timestamps must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def is_out_of_range(temperature: float, low: float, high: float) -> bool:
    return temperature < low or temperature > high


def calculate_temperature_excursions(readings, low: float, high: float, required_seconds: int) -> list[dict]:
    """Group observed out-of-range episodes; qualify by measurement timestamp span."""
    ordered = sorted(readings, key=lambda row: parse_reading_time(row["timestamp"]))
    excursions: list[dict] = []
    episode: list = []

    def finish_episode():
        if not episode:
            return
        times = [parse_reading_time(item["timestamp"]) for item in episode]
        temps = [float(item["temperature_f"]) for item in episode]
        duration_seconds = round((times[-1] - times[0]).total_seconds(), 3)
        excursions.append({
            "qualifies_for_failure": duration_seconds >= required_seconds,
            "started_at": times[0].isoformat().replace("+00:00", "Z"),
            "ended_at": times[-1].isoformat().replace("+00:00", "Z"),
            "duration_seconds": duration_seconds,
            "type": "below_minimum" if max(temps) < low else "above_maximum" if min(temps) > high else "out_of_range",
            "minimum_temperature_f": min(temps),
            "maximum_temperature_f": max(temps),
            "reading_count": len(episode),
            "record_ids": [item["record_id"] for item in episode],
        })

    for row in ordered:
        if is_out_of_range(float(row["temperature_f"]), low, high):
            episode.append(row)
        else:
            finish_episode()
            episode = []
    finish_episode()
    return excursions


def evaluate_temperature(conn: sqlite3.Connection, reading: ReadingIn) -> None:
    state = ensure_device(conn, reading.device_id)
    low, high = float(state["minimum_f"]), float(state["maximum_f"])
    required_seconds = int(state["excursion_duration_seconds"])
    if reading.delivery_id:
        delivery_rules = conn.execute(
            "SELECT threshold_min_f, threshold_max_f, excursion_duration_seconds "
            "FROM deliveries WHERE delivery_id=? AND device_id=?",
            (reading.delivery_id, reading.device_id),
        ).fetchone()
        if delivery_rules:
            low, high = float(delivery_rules["threshold_min_f"]), float(delivery_rules["threshold_max_f"])
            required_seconds = int(delivery_rules["excursion_duration_seconds"])
    reading_time = parse_reading_time(reading.timestamp)
    timestamp_text = reading_time.isoformat().replace("+00:00", "Z")
    active = conn.execute(
        "SELECT * FROM alerts WHERE device_id=? AND status='active' ORDER BY id DESC LIMIT 1",
        (reading.device_id,),
    ).fetchone()
    excursion = conn.execute("SELECT * FROM excursion_state WHERE device_id=?", (reading.device_id,)).fetchone()

    # Delayed/out-of-order samples remain in delivery reports, but must not rewind
    # the live alert state machine or create misleading real-time notifications.
    if excursion and excursion["last_observed_at"]:
        last_time = parse_reading_time(excursion["last_observed_at"])
        if reading_time <= last_time:
            return

    outside = is_out_of_range(reading.temperature_f, low, high)
    pending_start = excursion["pending_started_at"] if excursion else None
    last_observed = excursion["last_observed_at"] if excursion else None
    if not outside:
        if active is not None:
            conn.execute(
                "UPDATE alerts SET status='resolved', resolved_at=?, last_observed_at=? WHERE id=?",
                (timestamp_text, timestamp_text, active["id"]),
            )
            resolved = conn.execute("SELECT * FROM alerts WHERE id=?", (active["id"],)).fetchone()
            queue_notification(conn, resolved, "temperature_recovered", reading.temperature_f)
        conn.execute("DELETE FROM excursion_state WHERE device_id=?", (reading.device_id,))
        return

    if pending_start is None:
        pending_start = timestamp_text
        conn.execute(
            "INSERT INTO excursion_state(device_id, pending_started_at, last_observed_at, delivery_id, alert_type, min_observed_f, max_observed_f, threshold_min_f, threshold_max_f) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(device_id) DO UPDATE SET "
            "pending_started_at=excluded.pending_started_at, last_observed_at=excluded.last_observed_at, "
            "delivery_id=excluded.delivery_id, alert_type=excluded.alert_type, min_observed_f=excluded.min_observed_f, "
            "max_observed_f=excluded.max_observed_f, threshold_min_f=excluded.threshold_min_f, threshold_max_f=excluded.threshold_max_f",
            (reading.device_id, pending_start, timestamp_text, reading.delivery_id,
             "below_minimum" if reading.temperature_f < low else "above_maximum",
             reading.temperature_f, reading.temperature_f, low, high),
        )
    else:
        conn.execute(
            "UPDATE excursion_state SET last_observed_at=?, min_observed_f=MIN(min_observed_f, ?), "
            "max_observed_f=MAX(max_observed_f, ?) WHERE device_id=?",
            (timestamp_text, reading.temperature_f, reading.temperature_f, reading.device_id),
        )

    duration = (reading_time - parse_reading_time(pending_start)).total_seconds()
    if active is None and duration >= required_seconds:
        pending = conn.execute("SELECT * FROM excursion_state WHERE device_id=?", (reading.device_id,)).fetchone()
        kind = pending["alert_type"] or ("below_minimum" if reading.temperature_f < low else "above_maximum")
        confirmed_at = timestamp_text
        cur = conn.execute(
            "INSERT INTO alerts(device_id, delivery_id, alert_type, status, started_at, last_observed_at, "
            "min_observed_f, max_observed_f, threshold_min_f, threshold_max_f, confirmed_at) "
            "VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)",
            (reading.device_id, pending["delivery_id"], kind, pending_start, timestamp_text,
             pending["min_observed_f"], pending["max_observed_f"], low, high, confirmed_at),
        )
        alert = conn.execute("SELECT * FROM alerts WHERE id=?", (cur.lastrowid,)).fetchone()
        queue_notification(conn, alert, "temperature_alert", reading.temperature_f)
    elif active is not None:
        conn.execute(
            "UPDATE alerts SET last_observed_at=?, min_observed_f=MIN(min_observed_f, ?), "
            "max_observed_f=MAX(max_observed_f, ?) WHERE id=?",
            (timestamp_text, reading.temperature_f, reading.temperature_f, active["id"]),
        )


def send_email(subject: str, body: str, recipient: str) -> None:
    """Send one email using SMTP environment configuration; raises on failure."""
    host = os.getenv("SMTP_HOST")
    sender = os.getenv("ALERT_EMAIL_FROM")
    if not host or not sender:
        raise RuntimeError("SMTP_HOST and ALERT_EMAIL_FROM must be configured")
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME")
    password = os.getenv("SMTP_PASSWORD")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(host, port, timeout=10) as smtp:
        smtp.starttls()
        if username:
            smtp.login(username, password or "")
        smtp.send_message(message)


def process_notification_queue(limit: int = 20) -> dict:
    sent = 0
    failed = 0
    with closing(get_connection()) as conn:
        pending = conn.execute(
            "SELECT * FROM notification_queue WHERE status='pending' ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        for item in pending:
            try:
                recipient = item["recipient"] or os.getenv("ALERT_EMAIL_TO", "").strip()
                if not recipient:
                    raise RuntimeError("ALERT_EMAIL_TO is not configured")
                send_email(item["subject"], item["body"], recipient)
                conn.execute(
                    "UPDATE notification_queue SET status='sent', recipient=?, attempts=attempts+1, sent_at=?, last_error=NULL WHERE id=?",
                    (recipient, utc_now(), item["id"]),
                )
                sent += 1
            except Exception as exc:  # missing SMTP configuration is retained as a retryable failure
                conn.execute(
                    "UPDATE notification_queue SET attempts=attempts+1, last_error=? WHERE id=?",
                    (str(exc)[:500], item["id"]),
                )
                failed += 1
        conn.commit()
    return {"sent": sent, "failed": failed}


@app.get("/api/v1/health")
def health():
    return {"status": "ok", "service": "meridian-api"}


@app.post("/api/v1/readings", status_code=201)
def create_reading(reading: ReadingIn):
    timestamp = reading.timestamp.isoformat().replace("+00:00", "Z")
    received_at = utc_now()
    with closing(get_connection()) as conn:
        state = ensure_device(conn, reading.device_id)
        # Firmware can omit delivery_id: while one delivery is active, the API
        # associates newly received readings with that delivery. Complete only
        # after the ESP32 has drained its offline queue so delayed readings are
        # not accidentally attributed to a later delivery.
        if reading.delivery_id is None:
            active_delivery = conn.execute(
                "SELECT delivery_id FROM deliveries WHERE device_id=? AND status='active'",
                (reading.device_id,),
            ).fetchone()
            if active_delivery:
                reading.delivery_id = active_delivery["delivery_id"]
        elif reading.delivery_id:
            known_delivery = conn.execute(
                "SELECT device_id FROM deliveries WHERE delivery_id=?", (reading.delivery_id,)
            ).fetchone()
            if known_delivery is None or known_delivery["device_id"] != reading.device_id:
                raise HTTPException(status_code=409, detail="Unknown delivery ID for this device.")

        if (reading.sequence is not None and state["last_sequence"] is not None
                and reading.sequence - state["last_sequence"] > MAX_GAP_EXPANSION + 1):
            raise HTTPException(status_code=409, detail="Sequence jump exceeds the configured gap-expansion limit.")
        try:
            cursor = conn.execute(
                "INSERT INTO readings(record_id, device_id, sequence, delivery_id, timestamp, temperature_f, humidity_percent, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (reading.record_id, reading.device_id, reading.sequence, reading.delivery_id,
                 timestamp, reading.temperature_f, reading.humidity_percent, received_at),
            )
        except sqlite3.IntegrityError:
            existing = conn.execute(
                "SELECT * FROM readings WHERE record_id=?", (reading.record_id,)
            ).fetchone()
            if existing is None and reading.sequence is not None:
                existing = conn.execute(
                    "SELECT * FROM readings WHERE device_id=? AND sequence=?",
                    (reading.device_id, reading.sequence),
                ).fetchone()
            if existing is not None:
                identical = (
                    existing["device_id"] == reading.device_id
                    and existing["sequence"] == reading.sequence
                    and existing["timestamp"] == timestamp
                    and abs(existing["temperature_f"] - reading.temperature_f) < 0.000001
                    and abs(existing["humidity_percent"] - reading.humidity_percent) < 0.000001
                    and existing["delivery_id"] == reading.delivery_id
                )
                if identical:
                    conn.commit()
                    return {"accepted": True, "duplicate": True, "record_id": reading.record_id,
                            "sequence": reading.sequence, "message": "Identical reading already stored; safe to acknowledge."}
            raise HTTPException(status_code=409, detail="Record ID or device sequence conflicts with different reading data.")

        if reading.delivery_id and reading.sequence is not None:
            delivery = conn.execute(
                "SELECT start_sequence, end_sequence FROM deliveries WHERE delivery_id=?",
                (reading.delivery_id,),
            ).fetchone()
            if delivery:
                conn.execute(
                    "UPDATE deliveries SET start_sequence=COALESCE(start_sequence, ?), "
                    "end_sequence=MAX(COALESCE(end_sequence, ?), ?) WHERE delivery_id=?",
                    (reading.sequence, reading.sequence, reading.sequence, reading.delivery_id),
                )

        if reading.sequence is not None:
            # Only forward jumps create gaps. Older, delayed readings can close an existing gap.
            previous_max = state["last_sequence"]
            if previous_max is not None and reading.sequence > previous_max + 1:
                for missing in range(previous_max + 1, reading.sequence):
                    conn.execute(
                        "INSERT OR IGNORE INTO data_gaps(device_id, sequence, detected_at) VALUES (?, ?, ?)",
                        (reading.device_id, missing, received_at),
                    )
            conn.execute(
                "UPDATE data_gaps SET status='recovered', recovered_at=? "
                "WHERE device_id=? AND sequence=? AND status='open'",
                (received_at, reading.device_id, reading.sequence),
            )
            conn.execute(
                "UPDATE device_state SET last_sequence=MAX(COALESCE(last_sequence, ?), ?), last_reading_at=?, last_received_at=? WHERE device_id=?",
                (reading.sequence, reading.sequence, timestamp, received_at, reading.device_id),
            )
        else:
            conn.execute("UPDATE device_state SET last_reading_at=?, last_received_at=? WHERE device_id=?",
                         (timestamp, received_at, reading.device_id))

        evaluate_temperature(conn, reading)
        conn.commit()

    # Email failures never roll back a sensor reading or alert; pending messages remain queued.
    process_notification_queue()
    return {"accepted": True, "duplicate": False, "record_id": reading.record_id,
            "sequence": reading.sequence, "message": "Reading stored."}


@app.get("/api/v1/readings")
def list_readings(limit: int = Query(default=200, ge=1, le=5000), device_id: Optional[str] = None):
    query = "SELECT * FROM readings"
    params: list = []
    if device_id:
        query += " WHERE device_id = ?"
        params.append(device_id)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    with closing(get_connection()) as conn:
        rows = conn.execute(query, params).fetchall()
    return {"readings": [dict(row) for row in reversed(rows)]}


def build_delivery_report(conn: sqlite3.Connection, delivery: sqlite3.Row) -> dict:
    readings = conn.execute(
        "SELECT * FROM readings WHERE delivery_id=? ORDER BY timestamp, sequence",
        (delivery["delivery_id"],),
    ).fetchall()
    gaps = []
    if delivery["start_sequence"] is not None and delivery["end_sequence"] is not None:
        gaps = [dict(row) for row in conn.execute(
            "SELECT sequence, status, detected_at, recovered_at FROM data_gaps "
            "WHERE device_id=? AND sequence BETWEEN ? AND ? ORDER BY sequence",
            (delivery["device_id"], delivery["start_sequence"], delivery["end_sequence"]),
        ).fetchall()]
    temperatures = [float(row["temperature_f"]) for row in readings]
    low, high = float(delivery["threshold_min_f"]), float(delivery["threshold_max_f"])
    required_duration = int(delivery["excursion_duration_seconds"])
    excursions = calculate_temperature_excursions(readings, low, high, required_duration)

    expected = None
    observed_sequences = set()
    if delivery["start_sequence"] is not None and delivery["end_sequence"] is not None:
        sequence_start = int(delivery["start_sequence"])
        sequence_end = int(delivery["end_sequence"])
        expected = sequence_end - sequence_start + 1
        observed_sequences = {
            int(row["sequence"]) for row in readings
            if row["sequence"] is not None and sequence_start <= int(row["sequence"]) <= sequence_end
        }
    unresolved = [gap for gap in gaps if gap["status"] == "open"]
    sequence_coverage_complete = (
        expected is not None and expected > 0 and len(observed_sequences) == expected and not unresolved
    )
    queue_drained_confirmed = bool(delivery["queue_drained_confirmed"])
    coverage_complete = sequence_coverage_complete and queue_drained_confirmed

    if delivery["status"] != "completed":
        result = "IN_PROGRESS"
    elif any(item["qualifies_for_failure"] for item in excursions):
        result = "FAIL"
    else:
        # Gaps, missing sequence numbers, and empty/partial telemetry do not
        # change the requested temperature-only PASS/FAIL decision.
        result = "PASS"

    return {
        "delivery_id": delivery["delivery_id"],
        "device_id": delivery["device_id"],
        "status": delivery["status"],
        "result": result,
        "started_at": delivery["started_at"],
        "completed_at": delivery["completed_at"],
        "threshold_min_f": low,
        "threshold_max_f": high,
        "excursion_duration_seconds": required_duration,
        "reading_count": len(readings),
        "minimum_temperature_f": min(temperatures) if temperatures else None,
        "maximum_temperature_f": max(temperatures) if temperatures else None,
        "average_temperature_f": round(sum(temperatures) / len(temperatures), 3) if temperatures else None,
        "sequence_start": delivery["start_sequence"],
        "sequence_end": delivery["end_sequence"],
        "expected_readings": expected,
        "observed_sequences": len(observed_sequences),
        "coverage_complete": coverage_complete,
        "sequence_coverage_complete": sequence_coverage_complete,
        "queue_drained_confirmed": queue_drained_confirmed,
        "open_gaps": unresolved,
        "recovered_gaps": [gap for gap in gaps if gap["status"] == "recovered"],
        "temperature_excursions": excursions,
        "coverage_note": "Missing or unsequenced data is unknown monitoring coverage, never evidence of safe temperature.",
    }


@app.post("/api/v1/deliveries/start", status_code=201)
def start_delivery(body: DeliveryStart, device_id: str = Query(min_length=1)):
    with closing(get_connection()) as conn:
        state = ensure_device(conn, device_id)
        existing = conn.execute(
            "SELECT delivery_id FROM deliveries WHERE delivery_id=?", (body.delivery_id,)
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="Delivery ID already exists.")
        active = conn.execute(
            "SELECT delivery_id FROM deliveries WHERE device_id=? AND status='active'", (device_id,)
        ).fetchone()
        if active:
            raise HTTPException(status_code=409, detail=f"Device already has active delivery {active['delivery_id']}.")
        start_sequence = state["last_sequence"] + 1 if state["last_sequence"] is not None else None
        # A not-yet-confirmed excursion from before this delivery must not
        # contribute time to the new delivery's excursion calculation.
        conn.execute("DELETE FROM excursion_state WHERE device_id=?", (device_id,))
        try:
            conn.execute(
                "INSERT INTO deliveries(delivery_id, device_id, status, started_at, start_sequence, threshold_min_f, threshold_max_f, excursion_duration_seconds) "
                "VALUES (?, ?, 'active', ?, ?, ?, ?, ?)",
                (body.delivery_id, device_id, utc_now(), start_sequence, state["minimum_f"], state["maximum_f"],
                 state["excursion_duration_seconds"]),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="This device already has an active delivery.")
        conn.commit()
        delivery = conn.execute("SELECT * FROM deliveries WHERE delivery_id=?", (body.delivery_id,)).fetchone()
        return build_delivery_report(conn, delivery)


@app.post("/api/v1/deliveries/{delivery_id}/complete")
def complete_delivery(delivery_id: str, body: DeliveryComplete):
    with closing(get_connection()) as conn:
        delivery = conn.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
        if delivery is None:
            raise HTTPException(status_code=404, detail="Delivery not found.")
        if delivery["status"] == "active":
            latest = conn.execute(
                "SELECT MAX(sequence) AS max_sequence FROM readings WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()["max_sequence"]
            conn.execute(
                "UPDATE deliveries SET status='completed', completed_at=?, end_sequence=?, "
                "queue_drained_confirmed=? WHERE delivery_id=?",
                (utc_now(), latest, int(body.queue_drained), delivery_id),
            )
            conn.commit()
        completed = conn.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
        return build_delivery_report(conn, completed)


@app.get("/api/v1/deliveries")
def list_deliveries(device_id: Optional[str] = None):
    query = "SELECT * FROM deliveries"
    params: list = []
    if device_id:
        query += " WHERE device_id=?"
        params.append(device_id)
    query += " ORDER BY started_at DESC LIMIT 200"
    with closing(get_connection()) as conn:
        rows = conn.execute(query, params).fetchall()
        return {"deliveries": [build_delivery_report(conn, row) for row in rows]}


@app.get("/api/v1/deliveries/{delivery_id}")
def get_delivery(delivery_id: str):
    with closing(get_connection()) as conn:
        delivery = conn.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
        if delivery is None:
            raise HTTPException(status_code=404, detail="Delivery not found.")
        report = build_delivery_report(conn, delivery)
        rows = conn.execute(
            "SELECT record_id, sequence, timestamp, temperature_f, humidity_percent "
            "FROM readings WHERE delivery_id=? ORDER BY timestamp, sequence", (delivery_id,),
        ).fetchall()
        report["readings"] = [dict(row) for row in rows]
        return report


@app.get("/api/v1/deliveries/{delivery_id}/report")
def get_delivery_report(delivery_id: str):
    with closing(get_connection()) as conn:
        delivery = conn.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
        if delivery is None:
            raise HTTPException(status_code=404, detail="Delivery not found.")
        return build_delivery_report(conn, delivery)


def render_delivery_pdf(report: dict) -> bytes:
    buffer = io.BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=letter, title=f"Meridian Delivery {report['delivery_id']} Report")
    styles = getSampleStyleSheet()
    story = [
        Paragraph("Meridian Cold-Chain Monitor", styles["Title"]),
        Paragraph(f"Delivery Report: {report['delivery_id']}", styles["Heading2"]),
        Paragraph(f"Result: <b>{report['result']}</b>", styles["Heading2"]),
        Spacer(1, 10),
    ]
    summary_rows = [
        ["Device", report["device_id"], "Status", report["status"]],
        ["Started", report["started_at"], "Completed", report["completed_at"] or "Not completed"],
        ["Allowed range", f"{report['threshold_min_f']:.1f}–{report['threshold_max_f']:.1f} °F", "Readings", str(report["reading_count"])],
        ["Minimum observed", "—" if report["minimum_temperature_f"] is None else f"{report['minimum_temperature_f']:.1f} °F",
         "Maximum observed", "—" if report["maximum_temperature_f"] is None else f"{report['maximum_temperature_f']:.1f} °F"],
        ["Average observed", "—" if report["average_temperature_f"] is None else f"{report['average_temperature_f']:.1f} °F",
         "Sequence gaps", str(len(report["open_gaps"]))],
        ["Required excursion", f"{report['excursion_duration_seconds']} seconds", "Decision rule", "Sustained excursion"],
    ]
    summary = Table(summary_rows, colWidths=[105, 175, 90, 150])
    summary.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.whitesmoke),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.lightgrey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 6),
    ]))
    story.extend([summary, Spacer(1, 14), Paragraph("Temperature exceptions", styles["Heading2"])])
    if report["temperature_excursions"]:
        exception_rows = [["First observed", "Last observed", "Type", "Duration (s)", "Qualifies", "Readings"]]
        for item in report["temperature_excursions"]:
            exception_rows.append([
                item["started_at"], item["ended_at"], item["type"].replace("_", " "),
                str(item["duration_seconds"]), "Yes" if item["qualifies_for_failure"] else "No", str(item["reading_count"]),
            ])
        exceptions = Table(exception_rows, repeatRows=1)
        exceptions.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef2")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.lightgrey),
            ("PADDING", (0, 0), (-1, -1), 5),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(exceptions)
    else:
        story.append(Paragraph("No recorded temperatures were outside the configured range.", styles["BodyText"]))
    story.extend([Spacer(1, 10), Paragraph("Telemetry gaps", styles["Heading2"])])
    if report["open_gaps"]:
        story.append(Paragraph(
            "Open sequence gaps were detected. Under the requested report rule, gaps do not change PASS/FAIL; only sustained out-of-range episodes reaching the configured duration cause FAIL.",
            styles["BodyText"],
        ))
        story.append(Paragraph(", ".join(str(gap["sequence"]) for gap in report["open_gaps"]), styles["BodyText"]))
    else:
        story.append(Paragraph("No unresolved sequence gaps were recorded.", styles["BodyText"]))
    story.extend([Spacer(1, 10), Paragraph("Reading details", styles["Heading2"])])
    reading_rows = [["Timestamp (UTC)", "Sequence", "Temperature (°F)", "Humidity (%)"]]
    for reading in report.get("readings", []):
        reading_rows.append([
            reading["timestamp"], str(reading["sequence"] if reading["sequence"] is not None else "—"),
            f"{reading['temperature_f']:.1f}", f"{reading['humidity_percent']:.1f}",
        ])
    if len(reading_rows) == 1:
        reading_rows.append(["No readings recorded", "—", "—", "—"])
    reading_table = Table(reading_rows, repeatRows=1, colWidths=[250, 70, 100, 90])
    reading_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef2")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.lightgrey),
        ("PADDING", (0, 0), (-1, -1), 4),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
    ]))
    story.extend([reading_table, Spacer(1, 14), Paragraph(
        "Educational prototype only. This report is not a validated pharmaceutical safety or regulatory-compliance determination. Missing telemetry is not represented as measured temperature.",
        styles["Italic"],
    )])
    document.build(story)
    return buffer.getvalue()


@app.get("/api/v1/deliveries/{delivery_id}/report.pdf")
def download_delivery_pdf(delivery_id: str):
    with closing(get_connection()) as conn:
        delivery = conn.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
        if delivery is None:
            raise HTTPException(status_code=404, detail="Delivery not found.")
        report = build_delivery_report(conn, delivery)
        readings = conn.execute(
            "SELECT record_id, sequence, timestamp, temperature_f, humidity_percent "
            "FROM readings WHERE delivery_id=? ORDER BY timestamp, sequence", (delivery_id,),
        ).fetchall()
        report["readings"] = [dict(row) for row in readings]
    pdf = render_delivery_pdf(report)
    filename = f"meridian-delivery-{delivery_id}.pdf"
    return Response(content=pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{filename}"'
    })


@app.get("/api/v1/devices")
def list_devices():
    with closing(get_connection()) as conn:
        rows = conn.execute(
            "SELECT d.device_id, d.last_reading_at AS last_reading, d.last_received_at AS last_received, d.last_sequence, "
            "COUNT(r.record_id) AS reading_count FROM device_state d "
            "LEFT JOIN readings r ON r.device_id=d.device_id GROUP BY d.device_id ORDER BY d.device_id"
        ).fetchall()
    now = datetime.now(timezone.utc)
    devices = []
    for row in rows:
        item = dict(row)
        last_received = item["last_received"]
        age = (now - datetime.fromisoformat(last_received.replace("Z", "+00:00"))).total_seconds() if last_received else None
        item["seconds_since_contact"] = round(age, 1) if age is not None else None
        item["is_stale"] = age is None or age > SAMPLE_INTERVAL_SECONDS * 3
        devices.append(item)
    return {"devices": devices, "stale_after_seconds": SAMPLE_INTERVAL_SECONDS * 3}


@app.get("/api/v1/alerts")
def list_alerts(active_only: bool = True, device_id: Optional[str] = None):
    query = "SELECT * FROM alerts WHERE 1=1"
    params: list = []
    if active_only:
        query += " AND status='active'"
    if device_id:
        query += " AND device_id=?"
        params.append(device_id)
    query += " ORDER BY started_at DESC LIMIT 200"
    with closing(get_connection()) as conn:
        rows = conn.execute(query, params).fetchall()
    return {"alerts": [dict(row) for row in rows]}


@app.get("/api/v1/gaps")
def list_gaps(status: Optional[str] = Query(default=None, pattern="^(open|recovered)$"), device_id: Optional[str] = None):
    query = "SELECT * FROM data_gaps WHERE 1=1"
    params: list = []
    if status:
        query += " AND status=?"
        params.append(status)
    if device_id:
        query += " AND device_id=?"
        params.append(device_id)
    query += " ORDER BY detected_at DESC, sequence DESC LIMIT 500"
    with closing(get_connection()) as conn:
        rows = conn.execute(query, params).fetchall()
    return {"gaps": [dict(row) for row in rows], "coverage_note": "A gap is unresolved missing telemetry, not evidence of safe temperature."}


@app.get("/api/v1/settings/thresholds")
def get_thresholds(device_id: str = Query(min_length=1)):
    with closing(get_connection()) as conn:
        state = ensure_device(conn, device_id)
        conn.commit()
    return {"device_id": device_id, "minimum_f": state["minimum_f"], "maximum_f": state["maximum_f"],
            "excursion_duration_seconds": state["excursion_duration_seconds"],
            "note": "Defaults are illustrative; configure validated limits for the intended use."}


@app.put("/api/v1/settings/thresholds")
def update_thresholds(thresholds: ThresholdUpdate, device_id: str = Query(min_length=1)):
    with closing(get_connection()) as conn:
        ensure_device(conn, device_id)
        conn.execute(
            "UPDATE device_state SET minimum_f=?, maximum_f=?, excursion_duration_seconds=? WHERE device_id=?",
            (thresholds.minimum_f, thresholds.maximum_f, thresholds.excursion_duration_seconds, device_id),
        )
        # Existing active delivery rules are immutable snapshots. Only reset a
        # pending, not-yet-confirmed device-level excursion for future readings.
        active_delivery = conn.execute(
            "SELECT 1 FROM deliveries WHERE device_id=? AND status='active'", (device_id,)
        ).fetchone()
        if not active_delivery:
            conn.execute("DELETE FROM excursion_state WHERE device_id=?", (device_id,))
        conn.commit()
    return {"device_id": device_id, "minimum_f": thresholds.minimum_f, "maximum_f": thresholds.maximum_f,
            "excursion_duration_seconds": thresholds.excursion_duration_seconds,
            }


@app.post("/api/v1/notifications/retry")
def retry_notifications():
    return process_notification_queue(limit=100)


@app.get("/api/v1/notifications")
def list_notifications():
    with closing(get_connection()) as conn:
        rows = conn.execute(
            "SELECT id, alert_id, event_type, recipient, status, attempts, last_error, created_at, sent_at "
            "FROM notification_queue ORDER BY id DESC LIMIT 100"
        ).fetchall()
    return {"notifications": [dict(row) for row in rows]}


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
