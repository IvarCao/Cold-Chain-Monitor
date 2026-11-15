const API = ""; // Same origin: FastAPI serves both dashboard and API.
let temperatureChart;
let deliveryChart;
let activeDeliveryId = null;

async function checkHealth() {
  const badge = document.getElementById("connection");
  try {
    const response = await fetch(`${API}/api/v1/health`);
    if (!response.ok) throw new Error("API unavailable");
    badge.textContent = "Backend online";
    badge.className = "status status-ok";
  } catch {
    badge.textContent = "Backend unavailable";
    badge.className = "status status-unknown";
  }
}

function renderChart(readings) {
  const labels = readings.map(r => new Date(r.timestamp).toLocaleTimeString());
  const values = readings.map(r => r.temperature_f);
  if (temperatureChart) temperatureChart.destroy();
  temperatureChart = new Chart(document.getElementById("temperature-chart"), {
    type: "line",
    data: { labels, datasets: [{ label: "Temperature (°F)", data: values,
      borderColor: "#176b87", backgroundColor: "rgba(23,107,135,.12)", fill: true, tension: .2, pointRadius: 2 }] },
    options: { responsive: true, maintainAspectRatio: false,
      scales: { x: { title: { display: true, text: "Reading time (browser local time)" } },
                y: { title: { display: true, text: "°F" } } } }
  });
}

function renderTable(readings) {
  const body = document.getElementById("readings-body");
  body.replaceChildren();
  if (!readings.length) {
    const row = body.insertRow(); const cell = row.insertCell();
    cell.colSpan = 5; cell.textContent = "No readings yet."; return;
  }
  [...readings].reverse().slice(0, 20).forEach(r => {
    const row = body.insertRow();
    [new Date(r.timestamp).toISOString(), r.device_id,
     `${Number(r.temperature_f).toFixed(1)} °F`,
     `${Number(r.humidity_percent).toFixed(1)} %`, r.record_id].forEach(value => {
      row.insertCell().textContent = value;
    });
  });
}

async function refreshAlertsAndCoverage() {
  const alertsBox = document.getElementById("alerts-list");
  const gapsBox = document.getElementById("gaps-list");
  try {
    const [alertsResponse, gapsResponse, devicesResponse] = await Promise.all([
      fetch(`${API}/api/v1/alerts`), fetch(`${API}/api/v1/gaps?status=open`), fetch(`${API}/api/v1/devices`)
    ]);
    if (!alertsResponse.ok || !gapsResponse.ok || !devicesResponse.ok) throw new Error("Could not retrieve alerts or coverage status");
    const alerts = (await alertsResponse.json()).alerts || [];
    const gaps = (await gapsResponse.json()).gaps || [];
    const devices = (await devicesResponse.json()).devices || [];
    const staleDevices = devices.filter(device => device.is_stale);
    const alertCount = document.getElementById("alert-count");
    alertCount.textContent = `${alerts.length} active`;
    alertCount.className = alerts.length ? "status status-danger" : "status status-ok";
    alertsBox.replaceChildren();
    if (!alerts.length) {
      const p = document.createElement("p"); p.className = "note";
      p.textContent = "No active temperature alerts."; alertsBox.appendChild(p);
    } else alerts.forEach(alert => {
      const article = document.createElement("article"); article.className = "message message-danger";
      const heading = document.createElement("strong"); heading.textContent = `${alert.device_id}: ${alert.alert_type.replaceAll("_", " ")}`;
      const detail = document.createElement("p");
      detail.textContent = `Delivery: ${alert.delivery_id || "Unassigned"} | Observed range: ${Number(alert.min_observed_f).toFixed(1)}–${Number(alert.max_observed_f).toFixed(1)} °F | Out-of-range since: ${new Date(alert.started_at).toLocaleString()} | Confirmed: ${alert.confirmed_at ? new Date(alert.confirmed_at).toLocaleString() : "—"}`;
      article.append(heading, detail); alertsBox.appendChild(article);
    });
    const gapCount = document.getElementById("gap-count");
    gapCount.textContent = `${gaps.length} gaps · ${staleDevices.length} stale devices`;
    gapCount.className = (gaps.length || staleDevices.length) ? "status status-warning" : "status status-ok";
    gapsBox.replaceChildren();
    staleDevices.forEach(device => {
      const article = document.createElement("article"); article.className = "message message-warning";
      article.textContent = `${device.device_id}: no recent contact${device.seconds_since_contact === null ? "" : ` for ${Math.round(device.seconds_since_contact)} seconds`}. Monitoring coverage is unknown.`;
      gapsBox.appendChild(article);
    });
    gaps.forEach(gap => {
      const article = document.createElement("article"); article.className = "message message-warning";
      article.textContent = `${gap.device_id}: missing sequence ${gap.sequence} (detected ${new Date(gap.detected_at).toLocaleString()})`;
      gapsBox.appendChild(article);
    });
    if (!gaps.length && !staleDevices.length) {
      const p = document.createElement("p"); p.className = "note";
      p.textContent = "No unresolved sequence gaps or stale devices reported. Devices without sequence numbers cannot be checked for individual missing samples.";
      gapsBox.appendChild(p);
    }
  } catch (error) {
    alertsBox.textContent = error.message;
    gapsBox.textContent = error.message;
  }
}

function setDeliveryMessage(message, isError = false) {
  const el = document.getElementById("delivery-message");
  el.textContent = message;
  el.style.color = isError ? "#991b1b" : "";
}

async function refreshDeliveries() {
  try {
    const response = await fetch(`${API}/api/v1/deliveries`);
    if (!response.ok) throw new Error("Could not retrieve deliveries");
    const deliveries = (await response.json()).deliveries || [];
    const active = deliveries.find(delivery => delivery.status === "active") || null;
    activeDeliveryId = active ? active.delivery_id : null;
    const status = document.getElementById("delivery-status");
    const completeButton = document.getElementById("complete-delivery");
    status.textContent = active ? `Active: ${active.delivery_id}` : "No active delivery";
    status.className = active ? "status status-warning" : "status status-unknown";
    completeButton.disabled = !active;
    const body = document.getElementById("deliveries-body");
    body.replaceChildren();
    if (!deliveries.length) {
      const row = body.insertRow(); const cell = row.insertCell(); cell.colSpan = 6;
      cell.textContent = "No deliveries yet.";
      return;
    }
    deliveries.forEach(delivery => {
      const row = body.insertRow();
      [delivery.delivery_id, new Date(delivery.started_at).toLocaleString(),
       delivery.completed_at ? new Date(delivery.completed_at).toLocaleString() : "In progress",
       delivery.reading_count, delivery.result].forEach(value => row.insertCell().textContent = value ?? "—");
      const actionCell = row.insertCell();
      const button = document.createElement("button");
      button.textContent = "View report";
      button.addEventListener("click", () => viewDelivery(delivery.delivery_id));
      actionCell.appendChild(button);
      if (delivery.status === "completed") {
        const pdfLink = document.createElement("a");
        pdfLink.className = "button-link";
        pdfLink.textContent = "PDF";
        pdfLink.href = `${API}/api/v1/deliveries/${encodeURIComponent(delivery.delivery_id)}/report.pdf`;
        pdfLink.style.marginLeft = "6px";
        actionCell.appendChild(pdfLink);
      }
    });
  } catch (error) {
    setDeliveryMessage(error.message, true);
  }
}

async function startDelivery() {
  const input = document.getElementById("delivery-id");
  const deliveryId = input.value.trim();
  const deviceId = document.getElementById("delivery-device-id").value.trim();
  if (!deviceId) { setDeliveryMessage("Enter the ESP32 device ID first.", true); return; }
  if (!deliveryId) { setDeliveryMessage("Enter a delivery ID first.", true); return; }
  if (activeDeliveryId) { setDeliveryMessage(`Complete ${activeDeliveryId} before starting another delivery.`, true); return; }
  try {
    const response = await fetch(`${API}/api/v1/deliveries/start?device_id=${encodeURIComponent(deviceId)}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ delivery_id: deliveryId })
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Could not start delivery");
    input.value = "";
    setDeliveryMessage(`Delivery ${deliveryId} started. New ESP32 readings will be associated with it.`);
    await refreshDeliveries();
  } catch (error) { setDeliveryMessage(error.message, true); }
}

async function completeDelivery() {
  if (!activeDeliveryId) return;
  const deliveryId = activeDeliveryId;
  if (!window.confirm(`Complete ${deliveryId}? Make sure the ESP32 has reconnected and uploaded its offline queue first.`)) return;
  try {
    const response = await fetch(`${API}/api/v1/deliveries/${encodeURIComponent(deliveryId)}/complete`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ queue_drained: true })
    });
    const report = await response.json();
    if (!response.ok) throw new Error(report.detail || "Could not complete delivery");
    setDeliveryMessage(`Delivery ${deliveryId} completed. Report result: ${report.result}.`);
    await refreshDeliveries();
    await viewDelivery(deliveryId);
  } catch (error) { setDeliveryMessage(error.message, true); }
}

function metric(label, value) {
  const card = document.createElement("div"); card.className = "report-metric";
  const small = document.createElement("small"); small.textContent = label;
  const strong = document.createElement("strong"); strong.textContent = value;
  card.append(small, strong); return card;
}

async function viewDelivery(deliveryId) {
  try {
    const response = await fetch(`${API}/api/v1/deliveries/${encodeURIComponent(deliveryId)}`);
    const report = await response.json();
    if (!response.ok) throw new Error(report.detail || "Could not load delivery report");
    const section = document.getElementById("delivery-detail");
    section.hidden = false;
    document.getElementById("delivery-detail-title").textContent = `Delivery ${report.delivery_id}`;
    const result = document.getElementById("delivery-result");
    result.textContent = report.result;
    result.className = `status ${report.result === "PASS" ? "status-pass" : report.result === "FAIL" ? "status-fail" : "status-warning"}`;
    const pdfLink = document.getElementById("download-delivery-pdf");
    pdfLink.href = `${API}/api/v1/deliveries/${encodeURIComponent(deliveryId)}/report.pdf`;
    pdfLink.hidden = report.status !== "completed";
    const summary = document.getElementById("delivery-summary");
    summary.replaceChildren(
      metric("Minimum temperature", report.minimum_temperature_f == null ? "—" : `${Number(report.minimum_temperature_f).toFixed(1)} °F`),
      metric("Maximum temperature", report.maximum_temperature_f == null ? "—" : `${Number(report.maximum_temperature_f).toFixed(1)} °F`),
      metric("Average temperature", report.average_temperature_f == null ? "—" : `${Number(report.average_temperature_f).toFixed(1)} °F`),
      metric("Readings received", String(report.reading_count)),
      metric("Allowed range", `${Number(report.threshold_min_f).toFixed(1)}–${Number(report.threshold_max_f).toFixed(1)} °F`)
    );
    const warnings = document.getElementById("delivery-warnings");
    warnings.replaceChildren();
    if (report.temperature_excursions.length) {
      report.temperature_excursions.forEach(excursion => {
        const note = document.createElement("article"); note.className = excursion.qualifies_for_failure ? "message message-danger" : "message message-warning";
        note.textContent = `${excursion.qualifies_for_failure ? "Qualifying excursion" : "Brief excursion (does not fail delivery)"} — ${excursion.type.replaceAll("_", " ")} reading(s): ${new Date(excursion.started_at).toLocaleString()} to ${new Date(excursion.ended_at).toLocaleString()} (${excursion.duration_seconds} seconds, required ${report.excursion_duration_seconds} seconds; ${excursion.reading_count} reading(s); ${excursion.minimum_temperature_f.toFixed(1)}–${excursion.maximum_temperature_f.toFixed(1)} °F).`;
        warnings.appendChild(note);
      });
    }
    if (report.open_gaps.length) {
      const note = document.createElement("article"); note.className = "message message-warning";
      note.textContent = `Telemetry gaps detected (informational only; they do not change PASS/FAIL): ${report.open_gaps.map(g => g.sequence).join(", ")}.`;
      warnings.appendChild(note);
    }
    const readings = report.readings || [];
    if (deliveryChart) deliveryChart.destroy();
    deliveryChart = new Chart(document.getElementById("delivery-chart"), {
      type: "line", data: { labels: readings.map(r => new Date(r.timestamp).toLocaleString()),
        datasets: [{ label: "Temperature (°F)", data: readings.map(r => r.temperature_f), borderColor: "#176b87", tension: .2 }] },
      options: { responsive: true, maintainAspectRatio: false }
    });
    const table = document.getElementById("delivery-readings-body"); table.replaceChildren();
    readings.forEach(reading => {
      const row = table.insertRow();
      [new Date(reading.timestamp).toISOString(), reading.sequence ?? "—",
       `${Number(reading.temperature_f).toFixed(1)} °F`, `${Number(reading.humidity_percent).toFixed(1)} %`]
        .forEach(value => row.insertCell().textContent = value);
    });
    section.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) { setDeliveryMessage(error.message, true); }
}

async function loadMonitoringRules() {
  const deviceId = document.getElementById("rules-device-id").value.trim();
  if (!deviceId) return;
  const message = document.getElementById("rules-message");
  try {
    const response = await fetch(`${API}/api/v1/settings/thresholds?device_id=${encodeURIComponent(deviceId)}`);
    const settings = await response.json();
    if (!response.ok) throw new Error(settings.detail || "Could not load monitoring rules");
    document.getElementById("rules-minimum").value = settings.minimum_f;
    document.getElementById("rules-maximum").value = settings.maximum_f;
    document.getElementById("rules-duration").value = settings.excursion_duration_seconds;
    message.textContent = `Loaded rules for ${deviceId}.`;
  } catch (error) { message.textContent = error.message; }
}

async function saveMonitoringRules() {
  const deviceId = document.getElementById("rules-device-id").value.trim();
  const message = document.getElementById("rules-message");
  const rules = {
    minimum_f: Number(document.getElementById("rules-minimum").value),
    maximum_f: Number(document.getElementById("rules-maximum").value),
    excursion_duration_seconds: Number(document.getElementById("rules-duration").value),
  };
  if (!deviceId) { message.textContent = "Enter a device ID."; return; }
  if (!Number.isFinite(rules.minimum_f) || !Number.isFinite(rules.maximum_f) ||
      !Number.isInteger(rules.excursion_duration_seconds)) {
    message.textContent = "Enter valid numeric values for all monitoring rules."; return;
  }
  try {
    const response = await fetch(`${API}/api/v1/settings/thresholds?device_id=${encodeURIComponent(deviceId)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(rules)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Could not save monitoring rules");
    message.textContent = `Saved for ${deviceId}: ${rules.minimum_f}–${rules.maximum_f} °F; alert and delivery excursion duration ${rules.excursion_duration_seconds} seconds.`;
  } catch (error) { message.textContent = error.message; }
}

async function refreshDashboard() {
  await checkHealth();
  await refreshAlertsAndCoverage();
  await refreshDeliveries();
  try {
    const response = await fetch(`${API}/api/v1/readings?limit=200`);
    if (!response.ok) throw new Error("Could not retrieve readings");
    const readings = (await response.json()).readings || [];
    document.getElementById("count").textContent = readings.length;
    if (readings.length) {
      const latest = readings[readings.length - 1];
      document.getElementById("temperature").textContent = Number(latest.temperature_f).toFixed(1);
      document.getElementById("humidity").textContent = Number(latest.humidity_percent).toFixed(1);
      document.getElementById("device").textContent = latest.device_id;
      document.getElementById("last-seen").textContent = `Last reading: ${new Date(latest.timestamp).toLocaleString()}`;
      document.getElementById("coverage-note").textContent = "Showing up to 200 stored readings. Delivery reports separately evaluate their own monitoring coverage.";
    } else {
      ["temperature", "humidity", "device"].forEach(id => document.getElementById(id).textContent = "—");
      document.getElementById("last-seen").textContent = "No readings yet";
      document.getElementById("coverage-note").textContent = "No data available.";
    }
    renderChart(readings); renderTable(readings);
  } catch (error) { document.getElementById("coverage-note").textContent = error.message; }
}

document.getElementById("refresh").addEventListener("click", refreshDashboard);
document.getElementById("start-delivery").addEventListener("click", startDelivery);
document.getElementById("complete-delivery").addEventListener("click", completeDelivery);
document.getElementById("save-rules").addEventListener("click", saveMonitoringRules);
document.getElementById("rules-device-id").addEventListener("change", () => {
  document.getElementById("delivery-device-id").value = document.getElementById("rules-device-id").value;
  loadMonitoringRules();
});
document.getElementById("delivery-device-id").addEventListener("change", () => {
  document.getElementById("rules-device-id").value = document.getElementById("delivery-device-id").value;
  loadMonitoringRules();
});
loadMonitoringRules();
refreshDashboard();
setInterval(refreshDashboard, 10000);
