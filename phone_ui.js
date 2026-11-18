"use strict";

const loginPanel = document.querySelector("#login-panel");
const loginForm = document.querySelector("#login-form");
const passwordInput = document.querySelector("#password");
const loginMessage = document.querySelector("#login-message");
const controls = document.querySelector("#controls");
const channels = document.querySelector("#channels");
const zoneGrid = document.querySelector("#zone-grid");
const rainPauseButton = document.querySelector("#rain-pause");
const initialZone = /^#zone-(1[0-6]|[1-9])$/.exec(location.hash);
let selectedChannel = initialZone ? Number(initialZone[1]) - 1 : null;
let currentStatus = null;
let historyView = location.hash === "#activity";
let tapoView = location.hash === "#tapo";
let tapoRequestId = 0;
let tapoTimer;
const tapoPanel = document.querySelector("#tapo-panel");
const openTapo = document.querySelector("#open-tapo");
const tapoStatus = document.querySelector("#tapo-status");
const tapoRefresh = document.querySelector("#tapo-refresh");
const tapoRows = document.querySelector("#tapo-table tbody");
let tapoGraphChannel = null;
let tapoGraphRequestId = 0;
const tapoGraph = document.querySelector("#tapo-graph");
let tapoSampleData = null;
let tapoRateSaving = false;
const healthDialog = document.querySelector("#health-dialog");
let healthRequestId = 0;
document.querySelector("#health-close").addEventListener("click", () => { healthRequestId += 1; healthDialog.close(); });
document.querySelector("#health-refresh").addEventListener("click", loadHealth);
document.querySelector("#open-health").addEventListener("click", () => { healthDialog.showModal(); loadHealth(); });

async function loadHealth() {
  const id = ++healthRequestId;
  const status = document.querySelector("#health-status");
  const checks = document.querySelector("#health-checks");
  checks.replaceChildren();
  status.textContent = "Checking system health...";
  try {
    const {health} = await request("/api/system-health");
    if (id !== healthRequestId || !healthDialog.open) return;
    status.textContent = `Checked: ${new Date(health.checked_at).toLocaleString()}`;
    for (const row of health.checks) {
      const label = document.createElement("dt");
      label.textContent = row.label;
      const value = document.createElement("dd");
      value.textContent = row.value;
      checks.append(label, value);
    }
  } catch (error) {
    if (id === healthRequestId) status.textContent = `System health unavailable: ${error.message}`;
  }
}

function showYearlyEnergy(years) {
  const target = document.querySelector("#tapo-yearly-energy");
  target.replaceChildren();
  if (!Array.isArray(years)) {
    target.textContent = "Yearly tally unavailable. Restart the updated controller to enable it.";
    return;
  }
  if (!years.length) {
    target.textContent = "Waiting for recorded counters.";
    return;
  }
  for (const year of years) {
    const row = document.createElement("p");
    const cost = Number(year.unpriced_kwh) > 0
      ? `$${year.cost} priced portion; ${year.unpriced_kwh} kWh unpriced` : `$${year.cost} estimated`;
    row.textContent = `${year.year}: ${year.kwh} kWh — ${cost}. `
      + `Tracking ${new Date(year.since).toLocaleString()} to ${new Date(year.last).toLocaleString()}.`
      + (year.incomplete ? " Incomplete: gaps or counter resets; known usage only." : "");
    target.append(row);
  }
}

function showTapoRecordedDetails(show) {
  document.querySelector("#tapo-graph-overview").classList.toggle("hidden", show);
  document.querySelector("#tapo-recorded-details").classList.toggle("hidden", !show);
  document.querySelector(show ? "#tapo-details-back" : "#tapo-details-open").focus();
}
document.querySelector("#tapo-details-open").addEventListener("click", () => showTapoRecordedDetails(true));
document.querySelector("#tapo-details-back").addEventListener("click", () => showTapoRecordedDetails(false));

function tapoGraphTime(value, includeDate = false) {
  const date = new Date(value);
  const time = `${date.getHours() % 12 || 12}:${String(date.getMinutes()).padStart(2, "0")} ${date.getHours() < 12 ? "am" : "pm"}`;
  return includeDate ? `${date.getDate()}/${date.getMonth() + 1} ${time}` : time;
}

function voltageInterruptions(periods) {
  const interruptions = [];
  let current = null;
  for (const [index, period] of periods.entries()) {
    if (period.state === "Voltage") {
      if (current) {
        current.return = period.start;
        interruptions.push(current);
        current = null;
      }
    } else {
      if (!current) current = {start: period.start, return: null, atRangeStart: index === 0, zero: false, unknown: false};
      current.zero ||= period.state === "Zero";
      current.unknown ||= period.state === "Unknown";
    }
  }
  if (current) interruptions.push(current);
  return interruptions;
}

function addVoltageObservations(card, rows, start, duration) {
  const section = document.createElement("section");
  section.className = "voltage-observations";
  const legend = document.createElement("p");
  legend.textContent = "Supply observations: red >0 V · blue 0 V measured · white unknown. Times below are sample-based; unknown does not prove supply OFF.";
  const band = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  band.setAttribute("role", "img");
  band.setAttribute("aria-label", "Voltage observations. White gaps longer than 10 minutes have start times above and voltage return times below. Times are sample-based.");
  const observations = tapoSampleData.voltage_periods || [];
  const measure = document.createElement("canvas").getContext("2d");
  measure.font = "12px sans-serif";
  const lanes = {start: [], return: []};
  const labels = [];
  for (const [index, period] of observations.entries()) {
    if (period.state !== "Unknown" || period.seconds <= 600) continue;
    const boundaries = [];
    if (index > 0) boundaries.push(["start", period.start]);
    if (index + 1 < observations.length && observations[index + 1].state === "Voltage") {
      boundaries.push(["return", period.end]);
    }
    for (const [edge, timestamp] of boundaries) {
      const text = tapoGraphTime(timestamp, duration > 86400000);
      const x = 75 + (Date.parse(timestamp) - start) / duration * 310;
      const width = measure.measureText(text).width + 4;
      const left = Math.min(Math.max(2, x - width / 2), 398 - width);
      let lane = lanes[edge].findIndex(right => left >= right + 6);
      if (lane === -1) { lane = lanes[edge].length; lanes[edge].push(0); }
      lanes[edge][lane] = left + width;
      labels.push({edge, timestamp, text, x, left, width, lane});
    }
  }
  const bandTop = 2 + lanes.start.length * 18;
  const bandBottom = bandTop + 16;
  band.setAttribute("viewBox", `0 0 400 ${bandBottom + 2 + lanes.return.length * 18}`);
  const value = row => !row.error && typeof row.voltage_v === "number" && Number.isFinite(row.voltage_v) && row.voltage_v >= 0;
  for (const period of observations) {
    const marker = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    for (const [name, content] of Object.entries({
      x: 75 + (Date.parse(period.start) - start) / duration * 310, y: bandTop,
      width: period.seconds * 1000 / duration * 310, height: 16,
      fill: {Voltage: "#ff3030", Zero: "#2288ff", Unknown: "#ffffff"}[period.state],
    })) marker.setAttribute(name, String(content));
    const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
    title.textContent = voltagePeriodLabel(period);
    marker.append(title);
    band.append(marker);
  }
  for (const label of labels) {
    const ns = "http://www.w3.org/2000/svg";
    const y = label.edge === "start" ? bandTop - (label.lane + 1) * 18 + 9
      : bandBottom + label.lane * 18 + 9;
    const leader = document.createElementNS(ns, "line");
    for (const [name, content] of Object.entries({
      x1: label.x, y1: label.edge === "start" ? bandTop : bandBottom,
      x2: label.left + label.width / 2, y2: y, stroke: "#aaaaaa",
    })) leader.setAttribute(name, String(content));
    leader.classList.add("voltage-gap-leader");
    band.prepend(leader);
    const background = document.createElementNS(ns, "rect");
    for (const [name, content] of Object.entries({
      x: label.left, y: y - 8, width: label.width, height: 16,
      fill: card.style.backgroundColor || "#193326",
    })) background.setAttribute(name, String(content));
    band.append(background);
    const text = document.createElementNS(ns, "text");
    for (const [name, content] of Object.entries({
      x: label.left + 2, y, fill: "white", "font-size": 12,
      "font-family": "sans-serif", "dominant-baseline": "central",
    })) text.setAttribute(name, String(content));
    text.classList.add("voltage-gap-time", `voltage-gap-${label.edge}`);
    text.textContent = label.text;
    const title = document.createElementNS(ns, "title");
    title.textContent = `${label.edge === "start" ? "Readings missing from" : "Voltage observed again"} ${tapoGraphTime(label.timestamp, true)}`;
    text.append(title);
    band.append(text);
  }
  const slider = document.createElement("input");
  slider.type = "range";
  slider.min = "0";
  slider.max = String(Math.max(0, rows.length - 1));
  slider.value = slider.max;
  slider.disabled = rows.length === 0;
  slider.setAttribute("aria-label", "Inspect voltage snapshot by time");
  const detail = document.createElement("p");
  detail.setAttribute("role", "status");
  const interruptions = document.createElement("div");
  interruptions.className = "voltage-interruptions";
  interruptions.setAttribute("aria-label", "Missing or zero voltage and return times, newest first");
  const periods = voltageInterruptions(observations).reverse();
  for (const period of periods) {
    const row = document.createElement("p");
    const kind = period.zero && period.unknown ? "0 V / missing" : period.zero ? "0 V measured" : "Missing";
    const startText = period.atRangeStart ? "at range start" : "from";
    const returned = period.return ? `voltage seen ${tapoGraphTime(period.return, true)}` : "no voltage return in range";
    row.textContent = `${kind} ${startText} ${tapoGraphTime(period.start, true)} → ${returned}`;
    interruptions.append(row);
  }
  if (!periods.length) interruptions.textContent = observations.length
    ? "No missing or zero-voltage periods in this range." : "Voltage observation periods unavailable.";
  const inspect = () => {
    const row = rows[Number(slider.value)];
    if (!row) {
      detail.textContent = "No recorded snapshots.";
      return;
    }
    const stamp = value(row) ? row.updated || row.timestamp : row.timestamp;
    detail.textContent = `${tapoGraphTime(stamp, true)}: `
      + (value(row) ? `${row.voltage_v.toFixed(3)} V measured` : "Unknown; no valid voltage reading");
  };
  slider.addEventListener("input", inspect);
  band.addEventListener("click", event => {
    if (!rows.length) return;
    const rect = band.getBoundingClientRect();
    const position = (event.clientX - rect.left) / rect.width * 400;
    const fraction = Math.min(1, Math.max(0, (position - 75) / 310));
    const target = start + fraction * duration;
    let nearest = 0;
    for (let index = 1; index < rows.length; index += 1) {
      if (Math.abs(Date.parse(rows[index].timestamp) - target) < Math.abs(Date.parse(rows[nearest].timestamp) - target)) nearest = index;
    }
    slider.value = String(nearest);
    inspect();
  });
  inspect();
  section.append(band, legend, interruptions, slider, detail);
  card.append(section);
}

function voltagePeriodLabel(period) {
  const state = period.state === "Unknown"
    ? period.seconds <= 120 ? "Brief unknown" : "Sustained unknown"
    : period.state === "Voltage" ? "Voltage observed >0 V" : "0 V measured";
  return `${tapoGraphTime(period.start, true)} — ${tapoGraphTime(period.end, true)}: ${state} (${(period.seconds / 60).toFixed(1)} minutes)`;
}

function drawTapoSamples() {
  const cards = document.querySelector("#tapo-sample-graphs");
  cards.replaceChildren();
  if (!tapoSampleData) return;
  const metrics = [
    ["power_w", "Power (W)", "W", "#17283b"],
    ["voltage_v", "Voltage (V)", "V", "#193326"],
    ["current_a", "Current (A)", "A", "#30223b"],
    ["state", "Reported state (ON=1, OFF=0)", "", "#173438"],
    ["today_kwh", "Today cumulative counter (kWh)", "kWh", "#392d1c"],
    ["month_kwh", "Month cumulative counter (kWh)", "kWh", "#29313d"],
  ];
  const rows = tapoSampleData.samples || [];
  const latest = rows.reduce((last, row) => !last || Date.parse(row.timestamp) > Date.parse(last.timestamp) ? row : last, null);
  for (const [metric, heading, unit, color] of metrics) {
  const card = document.createElement("section");
  card.className = "tapo-sample-card";
  card.style.backgroundColor = color;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 400 180");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${heading}: recorded measurements over selected period`);
  card.append(svg);
  const value = row => row.error || row[metric] === null || row[metric] === undefined
    ? null : metric === "state" ? Number(row.state) : row[metric];
  const valid = rows.map(value).filter(v => v !== null && Number.isFinite(v));
  const max = Math.max(1, ...valid);
  const start = Date.parse(tapoSampleData.start);
  const duration = Date.parse(tapoSampleData.end) - start;
  const ns = "http://www.w3.org/2000/svg";
  let previous = null;
  for (const row of rows) {
    const number = value(row);
    if (number === null || !Number.isFinite(number)) {
      previous = null;
      continue;
    }
    const x = 75 + (Date.parse(row.timestamp) - start) / duration * 310;
    const y = 140 - number / max * 115;
    if (previous) {
      const line = document.createElementNS(ns, "line");
      for (const [key, val] of Object.entries({x1:previous.x, y1:previous.y, x2:x, y2:y, stroke:"#f6b73c", "stroke-width":2})) {
        line.setAttribute(key, String(val));
      }
      svg.append(line);
    }
    const dot = document.createElementNS(ns, "circle");
    dot.setAttribute("cx", String(x)); dot.setAttribute("cy", String(y));
    dot.setAttribute("r", "3"); dot.setAttribute("fill", "#f6b73c");
    const title = document.createElementNS(ns, "title");
    title.textContent = `${tapoGraphTime(row.updated || row.timestamp, true)}: ${number}`;
    dot.append(title); svg.append(dot);
    previous = {x,y};
  }
  for (const [x,y,text] of [[3,25,max.toFixed(3)],[3,140,"0"],
    [75,170,tapoGraphTime(start, duration > 86400000)],[230,170,tapoGraphTime(start + duration / 2, duration > 86400000)],
    [385,170,tapoGraphTime(start + duration, duration > 86400000)]]) {
    const label = document.createElementNS(ns, "text");
    label.setAttribute("x", String(x)); label.setAttribute("y", String(y)); label.setAttribute("fill", "white");
    label.setAttribute("font-size", y === 170 ? "12" : "14");
    label.setAttribute("text-anchor", x === 385 ? "end" : x === 230 ? "middle" : "start");
    label.textContent = text; svg.append(label);
  }
  if (metric === "voltage_v" && tapoSampleData.off_peak) {
    addVoltageObservations(card, rows, start, duration);
  }
  const title = document.createElement("h4");
  title.textContent = heading;
  const reading = document.createElement("p");
  reading.className = "tapo-sample-reading";
  const latestValue = latest ? value(latest) : null;
  reading.textContent = latestValue === null || !Number.isFinite(latestValue) ? "Unavailable"
    : metric === "state" ? latestValue ? "ON" : "OFF" : `${latestValue.toFixed(3)} ${unit}`;
  const recorded = document.createElement("p");
  recorded.className = "tapo-sample-recorded";
  recorded.textContent = latest
    ? `${latest.error ? "Sample failed" : "Recorded"}: ${tapoGraphTime(latest.error ? latest.timestamp : latest.updated || latest.timestamp, true)}`
    : "No recorded sample";
  if (latest) {
    const stamp = latest.error ? latest.timestamp : latest.updated || latest.timestamp;
    recorded.textContent += ` (${Math.max(0, Math.floor((Date.now() - Date.parse(stamp)) / 60000))} min ago)`;
  }
  const count = document.createElement("p");
  count.className = "tapo-sample-recorded";
  count.textContent = `${valid.length} snapshots; gaps are not zero`;
  card.append(title, reading);
  if (["today_kwh", "month_kwh"].includes(metric) && tapoSampleData.rate_cents_per_kwh !== null
      && tapoSampleData.rate_cents_per_kwh !== undefined) {
    const cost = document.createElement("p");
    cost.className = "tapo-sample-cost";
    const amount = tapoSampleData.estimated_costs?.[metric];
    cost.textContent = amount === null || amount === undefined ? "Estimated cost unavailable" : `Estimated cost: $${amount}`;
    card.append(cost);
  }
  card.append(recorded, count);
  cards.append(card);
  }
  document.querySelector("#tapo-sample-status").textContent =
    `${rows.length} snapshots; current polling interval ${tapoSampleData.sample_interval_seconds / 60} minute(s). Older readings retain their original cadence. `
    + "Recording starts when enabled; earlier measurements cannot be reconstructed. "
    + (tapoSampleData.samples_warning || "");
}

function closeTapoGraph() {
  tapoGraphChannel = null;
  tapoGraphRequestId += 1;
  tapoGraph.classList.add("hidden");
  document.querySelector("#tapo-graph-overview").classList.remove("hidden");
  document.querySelector("#tapo-recorded-details").classList.add("hidden");
  document.querySelector(".tapo-table-scroll").classList.remove("hidden");
  tapoRefresh.classList.remove("hidden");
}
const historyPanel = document.querySelector("#history-panel");
const openHistory = document.querySelector("#open-history");
const backToZones = document.querySelector("#back-to-zones");
const exitMessage = document.querySelector("#exit-message");
let exitDeadline = 0;
let exitTimer;
const homeUrl = location.pathname + location.search;

function resetExitPrompt() {
  exitDeadline = 0;
  window.clearTimeout(exitTimer);
  exitMessage.classList.add("hidden");
}

function initializeNavigation() {
  if (history.state?.sprinklerNavigation !== "active") {
    // Keep one home guard behind the current view, not one entry per zone.
    history.replaceState({sprinklerNavigation: "base"}, "", homeUrl);
    history.pushState({sprinklerNavigation: "active"}, "", tapoView ? `${homeUrl}#tapo` : historyView ? `${homeUrl}#activity` : selectedChannel === null
      ? homeUrl : `${homeUrl}#zone-${selectedChannel + 1}`);
  }
}

function chooseZone(channel) {
  closeTapoGraph();
  resetExitPrompt();
  historyView = false;
  tapoView = false;
  tapoRequestId += 1;
  window.clearTimeout(tapoTimer);
  historyRequestId += 1;
  selectedChannel = channel;
  history.replaceState({sprinklerNavigation: "active"}, "", channel === null
    ? homeUrl : `${homeUrl}#zone-${channel + 1}`);
  if (currentStatus) renderStatus(currentStatus);
  window.scrollTo(0, 0);
}

backToZones.addEventListener("click", () => {
  if (tapoGraphChannel !== null) {
    closeTapoGraph();
    loadTapo();
    return;
  }
  const wasHistory = historyView;
  const wasTapo = tapoView;
  const channel = selectedChannel;
  chooseZone(null);
  zoneGrid.querySelector(`[data-zone-channel="${channel}"]`)?.focus();
  if (wasHistory) openHistory.focus();
  if (wasTapo) openTapo.focus();
});
openHistory.addEventListener("click", () => {
  closeTapoGraph();
  resetExitPrompt();
  selectedChannel = null;
  historyView = true;
  tapoView = false;
  tapoRequestId += 1;
  window.clearTimeout(tapoTimer);
  history.replaceState({sprinklerNavigation: "active"}, "", `${homeUrl}#activity`);
  if (currentStatus) renderStatus(currentStatus);
  window.scrollTo(0, 0);
  loadHistory(0);
});
window.addEventListener("popstate", (event) => {
  if (event.state?.sprinklerNavigation !== "base") return;
  if (tapoGraphChannel !== null) {
    closeTapoGraph();
    history.pushState({sprinklerNavigation: "active"}, "", `${homeUrl}#tapo`);
    loadTapo();
    return;
  }
  if (selectedChannel === null && !historyView && !tapoView && performance.now() < exitDeadline) {
    resetExitPrompt();
    history.back();
    return;
  }
  const wasZone = selectedChannel !== null || historyView || tapoView;
  selectedChannel = null;
  historyView = false;
  tapoView = false;
  tapoRequestId += 1;
  window.clearTimeout(tapoTimer);
  historyRequestId += 1;
  history.pushState({sprinklerNavigation: "active"}, "", homeUrl);
  if (currentStatus) renderStatus(currentStatus);
  window.scrollTo(0, 0);
  resetExitPrompt();
  if (!wasZone) {
    exitDeadline = performance.now() + 2000;
    exitMessage.textContent = "Press Back again to leave. Watering will continue.";
    exitMessage.classList.remove("hidden");
    exitTimer = window.setTimeout(resetExitPrompt, 2000);
  }
});
const runningSummary = document.querySelector("#running-summary");
const notice = document.querySelector("#notice");
const updated = document.querySelector("#updated");
const logoutButton = document.querySelector("#logout");
const stopAllButton = document.querySelector("#stop-all");
const historyStatus = document.querySelector("#history-status");
const historyList = document.querySelector("#history-list");
const historyNewer = document.querySelector("#history-newer");
const historyOlder = document.querySelector("#history-older");
const historyRefresh = document.querySelector("#history-refresh");
const historyFilter = document.querySelector("#history-filter");
const backupFile = document.querySelector("#backup-file");
const backupStatus = document.querySelector("#backup-status");
const backupExport = document.querySelector("#backup-export");
const backupRestore = document.querySelector("#backup-restore");
const resumeOperation = document.querySelector("#resume-operation");
const backupPasswordDialog = document.querySelector("#backup-password-dialog");
const backupPasswordForm = document.querySelector("#backup-password-form");
const backupPassword = document.querySelector("#backup-password");
const backupPasswordConfirm = document.querySelector("#backup-password-confirm");
const backupPasswordShow = document.querySelector("#backup-password-show");
let csrfToken = "";
let refreshTimer;
let historyOffset = 0;
let historyRequestId = 0;
let historyAvailable = false;
let historyFiltersAvailable = false;
const durations = new Map();
const reminderDrafts = new Map();

openTapo.addEventListener("click", () => {
  closeTapoGraph();
  resetExitPrompt();
  selectedChannel = null;
  historyView = false;
  historyRequestId += 1;
  tapoView = true;
  history.replaceState({sprinklerNavigation: "active"}, "", `${homeUrl}#tapo`);
  if (currentStatus) renderStatus(currentStatus);
  window.scrollTo(0, 0);
  loadTapo(true);
});
tapoRefresh.addEventListener("click", () => loadTapo(true));

async function loadTapo(refresh = false) {
  if (!tapoView || tapoGraphChannel !== null) return;
  const id = ++tapoRequestId;
  window.clearTimeout(tapoTimer);
  tapoRefresh.disabled = true;
  tapoStatus.textContent = "Reading... Previous values remain visible.";
  try {
    const {details} = await request(`/api/tapo-details${refresh ? "?refresh=1" : ""}`);
    if (id !== tapoRequestId || !tapoView || tapoGraphChannel !== null) return;
    tapoRows.replaceChildren();
    for (const row of details.rows) {
      const tr = document.createElement("tr");
      tr.tabIndex = 0;
      tr.className = "tapo-row-link";
      tr.setAttribute("aria-label", `Zone ${row.channel + 1}: ${row.name}. Open 24-hour controller history`);
      const open = () => openTapoGraph(row.channel);
      tr.addEventListener("click", open);
      tr.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          open();
        }
      });
      const stale = Boolean(row.error) || (row.updated && Date.now() - Date.parse(row.updated) > 60000);
      const values = [
        row.channel + 1, row.name,
        row.error || row.state === null ? "Unavailable" : row.state ? "ON" : "OFF",
        ...["power_w", "today_kwh", "month_kwh", "voltage_v", "current_a"]
          .map(key => row.error || row[key] === null ? "Unavailable" : row[key].toFixed(3)),
        row.updated ? new Date(row.updated).toLocaleString() : "Never",
        row.station_fault ? `Station fault — ${row.station_fault}. Explicit retry required.`
          : row.error ? `${row.updated ? "Stale" : "Unavailable"}: ${row.error}`
          : stale ? "Stale" : row.updated ? "Read" : details.reading ? "Reading..." : "Unavailable",
        row.off_peak ? `Requested ${row.requested_state ? "ON" : "OFF"}; ${row.availability}; `
          + `last confirmed ${row.last_confirmed_state === null ? "unknown" : row.last_confirmed_state ? "ON" : "OFF"}`
          + (row.confirmed_at ? ` at ${new Date(row.confirmed_at).toLocaleString()}` : "")
          : "Individual station fault protection",
      ];
      for (const value of values) {
        const td = document.createElement("td");
        td.textContent = String(value);
        tr.append(td);
      }
      tapoRows.append(tr);
    }
    tapoStatus.textContent = details.reading ? "Reading... Previous values remain visible."
      : details.rows.length ? "Read-only readings. Refresh reads at most once per 10 seconds." : "No Tapo plugs assigned.";
    tapoRefresh.disabled = details.reading;
    tapoTimer = window.setTimeout(() => loadTapo(), details.reading ? 750 : 15000);
  } catch (error) {
    if (id !== tapoRequestId || !tapoView) return;
    tapoStatus.textContent = `Details unavailable: ${error.message}. Displayed readings are stale.`;
    tapoRefresh.disabled = false;
  }
}

document.querySelector("#tapo-graph-back").addEventListener("click", () => {
  closeTapoGraph();
  loadTapo();
});
document.querySelector("#tapo-graph-refresh").addEventListener("click", () => openTapoGraph(tapoGraphChannel));
document.querySelector("#tapo-range").addEventListener("change", () => openTapoGraph(tapoGraphChannel));

function shiftRateDecimal(value, places) {
  const [whole, fraction = ""] = value.split(".");
  const digits = (whole || "0") + fraction;
  const point = (whole || "0").length + places;
  const shifted = point <= 0 ? "0." + "0".repeat(-point) + digits
    : point >= digits.length ? digits + "0".repeat(point - digits.length)
    : digits.slice(0, point) + "." + digits.slice(point);
  return shifted.replace(/^0+(?=\d)/, "").replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
}

document.querySelector("#tapo-rate-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (tapoRateSaving || tapoGraphChannel === null || !tapoSampleData?.electricity_rate_supported) return;
  const channel = tapoGraphChannel;
  const view = tapoGraphRequestId;
  const status = document.querySelector("#tapo-rate-status");
  const button = document.querySelector("#tapo-rate-save");
  const rate = document.querySelector("#tapo-rate").value.trim();
  if (rate.length > 14 || (rate && !/^(?:[0-9]+(?:\.[0-9]{1,6})?|\.[0-9]{1,6})$/.test(rate))) {
    status.textContent = "Enter non-negative dollars/kWh, e.g. 0.26268, with up to six decimal places.";
    return;
  }
  button.disabled = true;
  tapoRateSaving = true;
  status.textContent = "Saving rate...";
  try {
    await request("/api/tapo-rate", {method: "POST", body: JSON.stringify({
      channel, rate: rate ? shiftRateDecimal(rate, 2) : "", expected_rate: tapoSampleData.rate_cents_per_kwh,
    })});
    if (channel !== tapoGraphChannel || view !== tapoGraphRequestId || !tapoView) return;
    await openTapoGraph(channel);
  } catch (error) {
    if (channel === tapoGraphChannel && view === tapoGraphRequestId && tapoView) {
      status.textContent = `Rate not saved: ${error.message}`;
      button.disabled = false;
    }
  } finally {
    tapoRateSaving = false;
  }
});

async function openTapoGraph(channel) {
  if (channel === null || !tapoView) return;
  if (tapoGraphChannel !== channel) {
    document.querySelector("#tapo-graph-overview").classList.remove("hidden");
    document.querySelector("#tapo-recorded-details").classList.add("hidden");
  }
  tapoGraphChannel = channel;
  tapoRequestId += 1;
  window.clearTimeout(tapoTimer);
  const id = ++tapoGraphRequestId;
  tapoGraph.classList.remove("hidden");
  document.querySelector(".tapo-table-scroll").classList.add("hidden");
  tapoRefresh.classList.add("hidden");
  const status = document.querySelector("#tapo-graph-status");
  const refreshButton = document.querySelector("#tapo-graph-refresh");
  const graph = document.querySelector("#tapo-timeline");
  const periods = document.querySelector("#tapo-periods");
  const events = document.querySelector("#tapo-events");
  document.querySelector("#tapo-rate-save").disabled = true;
  document.querySelector("#tapo-rate-status").textContent = "Loading rate...";
  graph.replaceChildren();
  document.querySelector("#tapo-requested-timeline").replaceChildren();
  document.querySelector("#tapo-requested-panel").classList.add("hidden");
  tapoSampleData = null;
  drawTapoSamples();
  document.querySelector("#tapo-sample-rows").replaceChildren();
  document.querySelector("#tapo-sample-status").textContent = "";
  document.querySelector("#tapo-daily-energy").replaceChildren();
  periods.replaceChildren();
  events.replaceChildren();
  document.querySelector("#tapo-timeline-range").textContent = "";
  status.textContent = "Loading controller history...";
  document.querySelector("#tapo-yearly-energy").textContent = "Loading yearly tally...";
  refreshButton.disabled = true;
  try {
    const {timeline} = await request(`/api/tapo-timeline?channel=${channel}&hours=${document.querySelector("#tapo-range").value}`);
    if (id !== tapoGraphRequestId || !tapoView || tapoGraphChannel !== channel) return;
    document.querySelector("#tapo-graph-heading").textContent =
      `${document.querySelector("#tapo-range").selectedOptions[0].textContent} controller switching history`;
    status.textContent = `${timeline.name} — confirmed/recorded ON: ${(timeline.on_seconds / 3600).toFixed(2)} hours. ${timeline.description} ${timeline.warning}`;
    tapoSampleData = timeline;
    showYearlyEnergy(timeline.yearly_energy);
    const rate = timeline.rate_cents_per_kwh;
    document.querySelector("#tapo-rate").value = rate == null ? "" : shiftRateDecimal(rate, -2);
    document.querySelector("#tapo-rate-save").disabled = !timeline.electricity_rate_supported;
    document.querySelector("#tapo-rate-status").textContent = !timeline.electricity_rate_supported
      ? "Restart the updated controller to enable electricity rates."
      : rate === null ? "No rate set; costs are hidden." : `Saved rate: $${shiftRateDecimal(rate, -2)}/kWh.`;
    drawTapoSamples();
    for (const row of timeline.daily_energy || []) {
      const item = document.createElement("li");
      item.textContent = `${row.date}: ${row.kwh === null ? "Incomplete" : row.kwh.toFixed(3) + " kWh"} — ${row.status}`;
      document.querySelector("#tapo-daily-energy").append(item);
    }
    const sampleRows = document.querySelector("#tapo-sample-rows");
    for (const row of timeline.samples || []) {
      const tr = document.createElement("tr");
      const values = [
        new Date(row.timestamp).toLocaleString(), row.updated ? new Date(row.updated).toLocaleString() : "Unavailable",
        row.state === null ? "Unknown" : row.state ? "ON" : "OFF",
        ...["power_w", "today_kwh", "month_kwh", "voltage_v", "current_a"]
          .map(key => row[key] === null ? "Unavailable" : row[key].toFixed(3)),
        row.error || "Read",
      ];
      for (const text of values) {
        const td = document.createElement("td"); td.textContent = String(text); tr.append(td);
      }
      sampleRows.append(tr);
    }
    const local = value => tapoGraphTime(value, true);
    document.querySelector("#tapo-timeline-range").textContent = `${local(timeline.start)} — ${local(timeline.end)}`;
    if (timeline.off_peak) {
      const heading = document.createElement("li");
      heading.textContent = "Voltage / contact periods (sample-based estimates). Brief unknown: up to 2 minutes; sustained unknown: longer. Unknown does not prove supply OFF. Changes can occur between polls.";
      periods.append(heading);
      for (const period of timeline.voltage_periods || []) {
        const item = document.createElement("li");
        item.textContent = voltagePeriodLabel(period);
        periods.append(item);
      }
    }
    for (const row of timeline.segments) {
      const label = `${local(row.start)} — ${local(row.end)}: ${row.state} (${(row.seconds / 60).toFixed(1)} minutes)`;
      const band = document.createElement("span");
      band.style.width = `${row.seconds / ((Date.parse(timeline.end) - Date.parse(timeline.start)) / 1000) * 100}%`;
      band.style.backgroundColor = {ON:"#238636",OFF:"#3178b6",Unknown:"#777777"}[row.state];
      band.title = label;
      graph.append(band);
      const item = document.createElement("li");
      item.textContent = label;
      periods.append(item);
    }
    for (const row of timeline.events) {
      const item = document.createElement("li");
      item.textContent = `${local(row.timestamp)}: ${row.event} — ${row.reason}`;
      events.append(item);
    }
    const requestedGraph = document.querySelector("#tapo-requested-timeline");
    requestedGraph.replaceChildren();
    document.querySelector("#tapo-requested-panel").classList.toggle("hidden", !timeline.off_peak);
    if (timeline.off_peak) {
      for (const row of timeline.requested_segments || []) {
        const band = document.createElement("span");
        band.style.width = `${row.seconds / ((Date.parse(timeline.end) - Date.parse(timeline.start)) / 1000) * 100}%`;
        band.style.backgroundColor = {ON:"#238636",OFF:"#3178b6",Unknown:"#777777"}[row.state];
        band.title = `Requested ${row.state}: ${local(row.start)} — ${local(row.end)}`;
        requestedGraph.append(band);
      }
    }
  } catch (error) {
    if (id === tapoGraphRequestId && tapoView) {
      status.textContent = `History unavailable: ${error.message}`;
      document.querySelector("#tapo-rate-status").textContent = "Load history successfully before editing the rate.";
      document.querySelector("#tapo-yearly-energy").textContent = `Yearly tally unavailable: ${error.message}`;
    }
  } finally {
    if (id === tapoGraphRequestId) refreshButton.disabled = false;
  }
}

function showNotice(message) {
  notice.textContent = message;
  notice.classList.toggle("visible", Boolean(message));
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body) {
    headers.set("Content-Type", "application/json");
  }
  if (csrfToken && options.method === "POST" && path !== "/api/login") {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(path, {
    ...options,
    headers,
    credentials: "same-origin",
    cache: "no-store",
  });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/login") {
      signedOut();
    }
    throw new Error(result.error || `Request failed (${response.status}).`);
  }
  return result;
}

function signedIn() {
  loginPanel.classList.add("hidden");
  controls.classList.remove("hidden");
  logoutButton.classList.remove("hidden");
  showNotice("");
  refresh();
  if (historyView) loadHistory(0);
  if (tapoView) loadTapo(true);
}

function signedOut() {
  healthRequestId += 1;
  healthDialog.close();
  document.querySelector("#health-checks").replaceChildren();
  closeTapoGraph();
  resetExitPrompt();
  historyRequestId += 1;
  tapoRequestId += 1;
  tapoView = false;
  window.clearTimeout(tapoTimer);
  tapoRows.replaceChildren();
  tapoPanel.classList.add("hidden");
  document.body.classList.remove("tapo-view");
  window.clearTimeout(refreshTimer);
  csrfToken = "";
  loginPanel.classList.remove("hidden");
  controls.classList.add("hidden");
  logoutButton.classList.add("hidden");
  channels.replaceChildren();
  zoneGrid.replaceChildren();
  selectedChannel = null;
  historyView = false;
  currentStatus = null;
  document.body.classList.remove("zone-view");
  document.body.classList.remove("history-view");
  historyPanel.classList.add("hidden");
  backToZones.classList.add("hidden");
  rainPauseButton.disabled = true;
  runningSummary.textContent = "";
  historyList.replaceChildren();
  historyStatus.textContent = "";
  historyAvailable = false;
  backupExport.disabled = true;
  backupRestore.disabled = true;
  historyNewer.disabled = true;
  historyOlder.disabled = true;
  historyRefresh.disabled = true;
}

function formatRemaining(seconds) {
  const total = Math.ceil(seconds);
  const minutes = Math.floor(total / 60);
  const remainder = total % 60;
  return minutes ? `${minutes}m ${remainder}s remaining` : `${remainder}s remaining`;
}

function parseManualDuration(value) {
  const match = /^([0-9]+)(?:\.([0-9]+))?\s*([hH]?)$/.exec(value.trim());
  if (!match) throw new Error("Enter minutes, such as 30, or hours, such as 2.5h.");
  const fraction = match[2] || "";
  const scale = 10n ** BigInt(fraction.length);
  const amount = BigInt(match[1] + fraction) * (match[3] ? 60n : 1n);
  if (amount % scale !== 0n) {
    throw new Error("Duration must convert to whole minutes; no rounding is applied.");
  }
  const minutes = amount / scale;
  if (minutes < 1n || minutes > 1440n) {
    throw new Error("Duration must be 1 to 1440 minutes (up to 24h).");
  }
  return Number(minutes);
}

function formatManualDuration(minutes) {
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"}`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return `${hours} hour${hours === 1 ? "" : "s"}${remainder
    ? ` ${remainder} minute${remainder === 1 ? "" : "s"}` : ""} (${minutes} minutes)`;
}

function renderStatus(status) {
  const stationFaults = Object.entries(status.channel_faults || {});
  document.querySelector("#group-health").textContent =
    status.fault ? `Shared controller fault — both groups stopped; output states may be unknown: ${status.fault}`
    : `Garden: ${status.garden_fault ? `FAULT — ${status.garden_fault}. Check hardware and restart.` : "enabled"}`
    + ` · Appliances: ${stationFaults.length ? stationFaults.map(([channel, error]) =>
      `station ${Number(channel) + 1} fault — ${error}`).join("; ") : "enabled"}`;
  document.querySelector("#stop-garden").disabled = !status.group_controls_supported;
  document.querySelector("#stop-appliances").disabled = !status.group_controls_supported
    || !status.channels.some(zone => zone.channel >= 8);
  document.body.classList.toggle("zone-view", selectedChannel !== null);
  document.body.classList.toggle("history-view", historyView);
  document.body.classList.toggle("tapo-view", tapoView);
  tapoPanel.classList.toggle("hidden", !tapoView);
  historyPanel.classList.toggle("hidden", !historyView);
  backToZones.classList.toggle("hidden", selectedChannel === null && !historyView && !tapoView);
  const focused = document.activeElement;
  const reminderSettingsOpen = channels.querySelector(".reminder-settings")?.open === true;
  const focusTile = focused?.dataset.zoneChannel;
  const focusDurationSelect = focused?.matches("select[id^='duration-']") ? focused.id : null;
  const focusChannel = focused?.dataset.durationChannel;
  const focusStart = focusChannel !== undefined ? focused.selectionStart : null;
  const focusEnd = focusChannel !== undefined ? focused.selectionEnd : null;
  // Replacing a focused native select dismisses Android's duration picker.
  const preserveDurationPicker = Boolean(focusDurationSelect && !focused.disabled
    && currentStatus && focused.id === `duration-${selectedChannel}`
    && JSON.stringify(currentStatus.channels.find(zone => zone.channel === selectedChannel))
      === JSON.stringify(status.channels.find(zone => zone.channel === selectedChannel))
    && ["fault", "operation_held", "rain_paused", "restore_restart_required",
      "manual_timers_supported", "continuous_supported", "continuous_reminders_supported", "revision"]
      .every(key => currentStatus[key] === status[key]));
  if (!preserveDurationPicker) channels.replaceChildren();
  currentStatus = status;
  rainPauseButton.disabled = Boolean(status.fault || status.restore_restart_required);
  rainPauseButton.textContent = status.rain_paused ? "Rain Pause: ON" : "Rain Pause: OFF";
  rainPauseButton.setAttribute("aria-pressed", String(status.rain_paused));
  rainPauseButton.classList.toggle("danger", status.rain_paused);
  document.querySelector("#backup-panel").open ||= Boolean(status.operation_held);
  zoneGrid.replaceChildren();
  for (let channel = 0; channel < 16; channel += 1) {
    const zone = status.channels.find(item => item.channel === channel);
    const tile = document.createElement("button");
    tile.type = "button";
    tile.className = "zone-tile";
    tile.dataset.zoneChannel = String(channel);
    tile.setAttribute("aria-controls", "channels");
    tile.setAttribute("aria-expanded", String(selectedChannel === channel));
    const unavailable = !zone || zone.available === false;
    const running = Boolean(zone && (zone.master ? zone.pump_on : zone.running));
    const paused = Boolean(zone && !zone.master && status.rain_paused && !zone.rain_pause_exempt);
    tile.classList.toggle("running", running && !status.fault && !zone?.fault);
    tile.classList.toggle("unavailable", unavailable || Boolean(zone?.fault));
    tile.classList.toggle("paused", paused && !running && !unavailable && !status.fault);
    const number = document.createElement("span");
    number.textContent = `Channel ${channel + 1}`;
    const name = document.createElement("span");
    name.className = "zone-tile-name";
    name.textContent = zone?.name || "Not configured";
    const state = document.createElement("span");
    state.className = "zone-tile-status";
    state.textContent = status.fault ? "State unknown" : unavailable ? "Unavailable"
      : running ? (zone.master ? "Pump ON" : zone.running.remaining === null
        ? "ON until stopped" : `ON · ${formatRemaining(zone.running.remaining).replace(" remaining", "")}`)
        : status.operation_held ? "Restore hold" : paused ? "Rain paused" : "Off";
    if (zone?.tapo?.off_peak) {
      state.textContent = `Requested ${zone.tapo.requested_state ? "ON" : "OFF"} · ${zone.tapo.availability}`;
    }
    if (zone?.fault) state.textContent = `FAULT · state unconfirmed · ${zone.fault}`;
    if (status.fault) state.textContent = "State unknown — shared controller fault";
    tile.setAttribute("aria-label", `${number.textContent}, ${name.textContent}, ${state.textContent}`);
    tile.append(number, name, state);
    tile.addEventListener("click", () => {
      if (!currentStatus) return;
      chooseZone(channel);
    });
    zoneGrid.append(tile);
  }
  if (status.fault) {
    runningSummary.textContent = "Output states unknown — controller fault.";
  } else {
    const active = status.channels.filter(zone => zone.master ? zone.pump_on : zone.running);
    runningSummary.replaceChildren();
    if (!active.length) {
      runningSummary.textContent = "No channels currently reported running.";
    } else {
      const list = document.createElement("ul");
      for (const zone of active) {
        const item = document.createElement("li");
        if (zone.master) {
          item.textContent = "Master pump — automatic — end depends on pump-using channels";
        } else {
          const end = zone.running.remaining === null ? "until stopped" : `until about ${new Date(
            Date.now() + zone.running.remaining * 1000,
          ).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
          item.textContent = `Channel ${zone.channel + 1}, ${zone.name} — ${zone.running.source} — ${end}`
            + (zone.tapo?.off_peak ? ` — requested ON; ${zone.tapo.availability}; confirmed `
              + (zone.tapo.state === null ? "unknown" : zone.tapo.state ? "ON" : "OFF") : "");
        }
        list.append(item);
      }
      runningSummary.append(list);
    }
  }
  if (status.fault) {
    showNotice(`Controller fault: ${status.fault}`);
  } else if (status.operation_held) {
    showNotice("Restore safety hold: all outputs stopped. Restart services, review devices and settings, then Resume operation.");
  } else if (status.history_warning) {
    showNotice(`Activity history warning: ${status.history_warning}`);
  } else if (!status.manual_timers_supported) {
    showNotice("Restart the updated controller service before using manual timers.");
  } else if (status.rain_paused) {
    showNotice("Rain Pause is enabled. Exempt zones can still be started.");
  } else {
    showNotice("");
  }
  historyAvailable = status.history_supported === true;
  backupExport.disabled = status.backup_supported !== true || Boolean(status.restore_restart_required);
  backupRestore.disabled = status.backup_supported !== true || Boolean(status.fault || status.restore_restart_required);
  resumeOperation.classList.toggle("hidden", !status.operation_held);
  resumeOperation.disabled = Boolean(status.fault || status.restore_restart_required);
  if (status.backup_supported !== true) {
    backupStatus.textContent = "Restart the updated phone service and controller service to enable full backup and restore.";
  } else if (backupStatus.textContent.startsWith("Restart the updated phone service")) {
    backupStatus.textContent = "";
  }
  historyFiltersAvailable = status.history_filters_supported === true;
  const selectedFilter = historyFilter.value;
  const filters = [["all", "All activity"], ["system", "System"], ["faults", "Faults"],
    ...Array.from({ length: 16 }, (_, channel) => {
      const zone = status.channels.find(item => item.channel === channel);
      return [String(channel), `Channel ${channel + 1}${zone ? `: ${zone.name}` : ""}`];
    })];
  if (historyFilter.options.length !== filters.length ||
      filters.some(([value, label], index) => historyFilter.options[index]?.value !== value ||
        historyFilter.options[index]?.textContent !== label)) {
    historyFilter.replaceChildren();
    for (const [value, label] of filters) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      historyFilter.append(option);
    }
    historyFilter.value = filters.some(([value]) => value === selectedFilter) ? selectedFilter : "all";
  }
  historyFilter.disabled = !historyFiltersAvailable;
  historyRefresh.disabled = !historyAvailable;
  if (!historyAvailable) {
    historyStatus.textContent = "Restart the updated controller to enable activity history.";
    historyNewer.disabled = true;
    historyOlder.disabled = true;
  }
  for (const zone of status.channels) {
    if (zone.channel !== selectedChannel || preserveDurationPicker) continue;
    const card = document.createElement("article");
    card.className = "channel";
    const heading = document.createElement("div");
    heading.className = "channel-heading";
    const identity = document.createElement("div");
    const name = document.createElement("h2");
    name.textContent = zone.name;
    const number = document.createElement("span");
    number.className = "channel-number";
    number.textContent = `Channel ${zone.channel + 1}`;
    identity.append(number, name);
    const state = document.createElement("p");
    state.className = "channel-state";
    if (status.fault) {
      state.textContent = "Output state unknown — controller fault";
    } else if (zone.available === false) {
      state.textContent = zone.tapo ? "Unassigned Tapo plug — output unavailable" : "Output unavailable";
    } else if (zone.master) {
      state.textContent = zone.pump_on ? "Master pump · ON" : "Master pump · OFF";
    } else if (zone.running) {
      const end = zone.running.remaining === null ? "" : new Date(
        Date.now() + zone.running.remaining * 1000,
      ).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      state.textContent = zone.running.remaining === null
        ? zone.running.source === "Routine" ? "24/7 routine · ON · no daily stop" : "Continuous · ON until stopped"
        : `${zone.running.source} · ON until about ${end} · ${formatRemaining(zone.running.remaining)}`;
    } else {
      state.textContent = "Off";
    }
    if (zone.tapo?.off_peak) {
      const plug = zone.tapo;
      state.textContent = `Requested ${plug.requested_state ? "ON" : "OFF"} · ${plug.availability} · `
        + `last confirmed ${plug.last_confirmed_state === null ? "unknown" : plug.last_confirmed_state ? "ON" : "OFF"}`
        + (plug.confirmed_at ? ` at ${new Date(plug.confirmed_at).toLocaleString()}` : "")
        + (plug.pending ? " · request not yet confirmed" : "");
    }
    if (zone.fault) {
      state.textContent = `Station/group fault — ${zone.fault}. Output state may be unknown. `
        + (zone.channel < 8 ? "Appliances remain enabled. Check garden hardware and restart."
          : "Other stations remain enabled. Retry confirms OFF; it will not restart the missed run.");
    }
    if (status.fault) state.textContent = "Output state unknown — shared controller fault";
    heading.append(identity, state);
    card.append(heading);
    if (zone.master) {
      const explanation = document.createElement("p");
      explanation.textContent = "The master pump is automatic. It follows pump-using zones on channels 1-7 and has no independent run control.";
      card.append(explanation);
    }
    if (!zone.master && !status.fault && zone.available !== false && status.next_schedule_supported) {
      const next = document.createElement("p");
      next.className = "channel-state";
      next.textContent = zone.continuous_schedule
        ? `24/7 routine: ${zone.enabled ? "enabled — no daily OFF/ON cycle" : "stopped — resume explicitly"}`
        : `Next scheduled start (controller local time): ${zone.next_start
          ? zone.next_start.replace("T", " ") : "none"}`;
      if (status.rain_paused && !zone.rain_pause_exempt) {
        next.textContent += " — blocked while Rain Pause is on";
      }
      card.append(next);
    }

    if (!zone.master) {
      if (zone.running?.source === "Continuous" && zone.running.elapsed !== null
          && zone.running.elapsed !== undefined && zone.continuous_reminder_minutes !== null
          && zone.continuous_reminder_minutes !== undefined
          && zone.running.elapsed >= zone.continuous_reminder_minutes * 60) {
        const reminder = document.createElement("p");
        reminder.className = "notice visible";
        reminder.textContent = `Reminder: ${zone.name} has been running continuously for at least ${zone.continuous_reminder_minutes} minutes. It has not been stopped.`;
        card.append(reminder);
      }
      const duration = durations.get(zone.channel) || { choice: "10", custom: "" };
      durations.set(zone.channel, duration);
      const durationControls = document.createElement("div");
      durationControls.className = "duration-controls";
      const durationLabel = document.createElement("label");
      durationLabel.textContent = "Manual run duration";
      durationLabel.htmlFor = `duration-${zone.channel}`;
      const durationSelect = document.createElement("select");
      durationSelect.id = durationLabel.htmlFor;
      for (const [value, label] of [
        ["10", "10 minutes"], ["30", "30 minutes"], ["60", "1 hour"], ["custom", "Custom"],
      ]) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        durationSelect.append(option);
      }
      durationSelect.value = duration.choice;
      durationSelect.disabled = Boolean(status.fault || zone.fault || status.operation_held || zone.available === false || zone.running || !status.manual_timers_supported);
      const durationInput = document.createElement("input");
      durationInput.type = "text";
      durationInput.inputMode = "text";
      durationInput.placeholder = "Minutes or hours: 30 or 2.5h";
      durationInput.setAttribute("aria-label", `Custom duration for ${zone.name}: minutes or hours, e.g. 30 or 2.5h`);
      durationInput.dataset.durationChannel = String(zone.channel);
      durationInput.value = duration.custom;
      durationInput.classList.toggle("hidden", duration.choice !== "custom");
      durationInput.disabled = durationSelect.disabled;
      const durationSummary = document.createElement("span");
      durationSummary.className = "duration-summary";
      const updateDurationSummary = () => {
        const value = duration.choice === "custom" ? duration.custom.trim() : duration.choice;
        try {
          const minutes = parseManualDuration(value);
          durationSummary.textContent = `If started: Channel ${zone.channel + 1}, ${zone.name} — ${formatManualDuration(minutes)}. Select a duration, then press RUN.`;
        } catch (error) {
          durationSummary.textContent = error.message;
        }
      };
      durationInput.addEventListener("input", () => {
        duration.custom = durationInput.value;
        updateDurationSummary();
      });
      durationSelect.addEventListener("change", () => {
        duration.choice = durationSelect.value;
        durationInput.classList.toggle("hidden", duration.choice !== "custom");
        updateDurationSummary();
      });
      updateDurationSummary();
      durationControls.append(durationLabel, durationSelect, durationInput, durationSummary);
      if (zone.max_manual_minutes !== null) {
        const maximum = document.createElement("span");
        maximum.textContent = `Maximum manual run: ${zone.max_manual_minutes} minutes`;
        durationControls.append(maximum);
      }
      card.append(durationControls);
      const actions = document.createElement("div");
      actions.className = "channel-actions";
      const runButton = document.createElement("button");
      runButton.className = "primary";
      runButton.type = "button";
      runButton.textContent = "RUN";
      runButton.disabled = Boolean(
        status.fault || zone.fault || status.operation_held || zone.available === false || zone.running || !status.manual_timers_supported
        || (status.rain_paused && !zone.rain_pause_exempt),
      );
      const runBlocked = document.createElement("p");
      runBlocked.className = "notice";
      runBlocked.setAttribute("role", "status");
      runBlocked.textContent = status.fault ? `RUN unavailable: controller fault — ${status.fault}`
        : zone.fault ? `RUN unavailable: ${zone.fault}. ${zone.channel >= 8 ? "Retry station to confirm OFF first." : "Check garden hardware and restart."}`
        : status.operation_held ? "RUN unavailable: restore safety hold. Go Back to all zones to review the backup and resume controls."
          : zone.available === false ? "RUN unavailable: this output is not assigned or configured."
            : !status.manual_timers_supported ? "RUN unavailable: restart the updated controller service to enable manual timers."
              : status.rain_paused && !zone.rain_pause_exempt
                ? "RUN unavailable: Rain Pause is ON for this zone. Go Back to all zones and turn Rain Pause OFF to allow watering. Schedule-group pause buttons below do not change Rain Pause."
                : zone.running ? "This zone is already running. Use STOP to end its current run." : "";
      runBlocked.classList.toggle("visible", Boolean(runBlocked.textContent));
      card.append(runBlocked);
      runButton.addEventListener("click", () => {
        const value = duration.choice === "custom" ? duration.custom.trim() : duration.choice;
        let minutes;
        try {
          minutes = parseManualDuration(value);
        } catch (error) {
          showNotice(error.message);
          return;
        }
        if (zone.max_manual_minutes !== null && minutes > zone.max_manual_minutes) {
          showNotice(`This channel's manual maximum is ${zone.max_manual_minutes} minutes.`);
          return;
        }
        sendCommand("run", zone.channel, minutes);
      });
      const continuousButton = document.createElement("button");
      continuousButton.className = "continuous";
      continuousButton.type = "button";
      const continuousRunning = zone.running?.source === "Continuous";
      continuousButton.textContent = continuousRunning
        ? "Now running until stopped" : "RUN UNTIL STOPPED";
      continuousButton.classList.toggle("running", continuousRunning);
      continuousButton.disabled = runButton.disabled || !status.continuous_supported
        || zone.max_manual_minutes !== null;
      continuousButton.addEventListener("click", () => {
        if (window.confirm(
          `Keep channel ${zone.channel + 1} (${zone.name}) ON until stopped?\n\n`
          + "There is no automatic time limit. Closing the page or disconnecting "
          + "your phone will NOT stop it. Pump and Rain Pause exclusions still apply. "
          + "Controller restart or a fault affecting this station/group may stop it.",
        )) {
          sendCommand("continuous", zone.channel);
        }
      });
      const stopButton = document.createElement("button");
      stopButton.className = "quiet";
      stopButton.type = "button";
      stopButton.textContent = "STOP";
      stopButton.disabled = !zone.running && !zone.tapo?.off_peak && !zone.fault;
      stopButton.addEventListener("click", () => sendCommand("stop", zone.channel));
      actions.append(runButton, stopButton);
      if (zone.fault && zone.channel >= 8) {
        const retry = document.createElement("button");
        retry.type = "button";
        retry.textContent = "Retry station (confirm OFF)";
        retry.disabled = Boolean(status.fault || status.operation_held);
        retry.addEventListener("click", () => sendCommand("retry_station", zone.channel));
        actions.append(retry);
      }
      if (zone.pump_exempt === true || zone.channel >= 8) {
        actions.append(continuousButton);
      }
      card.append(actions);
      const schedules = document.createElement("section");
      const scheduleTitle = document.createElement("h3");
      scheduleTitle.textContent = "Scheduled watering";
      const pauseHelp = document.createElement("p");
      pauseHelp.className = "channel-state";
      pauseHelp.textContent = zone.continuous_schedule
        ? "Group 1 pauses the entire 24/7 routine. Unpausing resumes immediately. Other groups and timed windows are ignored."
        : "Each group applies across all weekdays. Pausing stops that group's scheduled run, not manual watering. Unpausing waits for its next start.";
      if (zone.channel >= 8) {
        const continuousSchedule = document.createElement("button");
        continuousSchedule.type = "button";
        continuousSchedule.className = "quiet";
        continuousSchedule.textContent = zone.continuous_schedule
          ? zone.enabled ? "Turn off 24/7 routine" : "Resume 24/7 routine" : "Enable saved 24/7 routine";
        continuousSchedule.disabled = !status.continuous_schedule_supported || Boolean(
          status.fault || status.restore_restart_required || zone.fault || zone.available === false,
        );
        continuousSchedule.addEventListener("click", async () => {
          const enabled = !zone.continuous_schedule || !zone.enabled;
          if (!window.confirm(enabled
            ? "Enable saved 24/7 operation? There is no daily OFF cycle. It can start immediately and resumes after restart unless stopped or paused."
            : "Disable the saved 24/7 routine? Timed windows are preserved but the routine is disabled.")) return;
          continuousSchedule.disabled = true;
          try {
            const result = await request("/api/continuous-schedule", {
              method: "POST",
              body: JSON.stringify({channel: zone.channel, enabled, revision: status.revision}),
            });
            renderStatus(result.status);
          } catch (error) {
            await refresh();
            showNotice(`Cannot change 24/7 routine: ${error.message}`);
          }
        });
        schedules.append(continuousSchedule);
      }
      const groups = document.createElement("div");
      groups.className = "pause-groups";
      for (let group = 0; group < 4; group += 1) {
        const paused = zone.paused_windows?.[group];
        const pause = document.createElement("button");
        pause.type = "button";
        pause.className = paused ? "danger" : "quiet";
        pause.textContent = `Group ${group + 1}: ${paused ? "Paused" : "Not Paused"}`;
        pause.setAttribute("aria-pressed", String(Boolean(paused)));
        pause.disabled = (zone.continuous_schedule && group > 0) || typeof paused !== "boolean" || Boolean(
          status.fault || status.restore_restart_required || zone.available === false,
        );
        pause.addEventListener("click", async () => {
          pause.disabled = true;
          try {
            const result = await request("/api/window-pause", {
              method: "POST",
              body: JSON.stringify({channel: zone.channel, group, paused: !paused, revision: status.revision}),
            });
            renderStatus(result.status);
          } catch (error) {
            await refresh();
            showNotice(`Cannot change schedule pause: ${error.message}`);
          }
        });
        groups.append(pause);
      }
      schedules.append(scheduleTitle, pauseHelp, groups);
      card.append(schedules);
      const reminderControls = document.createElement("div");
      reminderControls.className = "duration-controls reminder-controls";
      const reminderLabel = document.createElement("label");
      reminderLabel.textContent = "Continuous reminder in minutes (blank = off)";
      reminderLabel.htmlFor = `reminder-${zone.channel}`;
      const reminderInput = document.createElement("input");
      reminderInput.id = reminderLabel.htmlFor;
      reminderInput.type = "text";
      reminderInput.inputMode = "numeric";
      reminderInput.dataset.durationChannel = `reminder-${zone.channel}`;
      reminderInput.value = reminderDrafts.has(zone.channel)
        ? reminderDrafts.get(zone.channel) : String(zone.continuous_reminder_minutes ?? "");
      reminderInput.disabled = Boolean(status.fault || zone.available === false || !status.continuous_reminders_supported);
      reminderInput.addEventListener("input", () => reminderDrafts.set(zone.channel, reminderInput.value));
      const saveReminder = document.createElement("button");
      saveReminder.type = "button";
      saveReminder.className = "quiet";
      saveReminder.textContent = "Save reminder";
      saveReminder.disabled = reminderInput.disabled;
      saveReminder.addEventListener("click", async () => {
        const raw = reminderInput.value.trim();
        if (raw && (!/^[0-9]+$/.test(raw) || Number(raw) < 1 || Number(raw) > 1440)) {
          showNotice("Reminder must be blank or 1 to 1440 minutes.");
          return;
        }
        try {
          const result = await request("/api/reminder", {
            method: "POST",
            body: JSON.stringify({channel: zone.channel, minutes: raw ? Number(raw) : null, revision: status.revision}),
          });
          reminderDrafts.delete(zone.channel);
          renderStatus(result.status);
          showNotice("Reminder saved. Reminders do not stop the channel.");
        } catch (error) {
          showNotice(`Cannot save reminder: ${error.message}`);
        }
      });
      reminderControls.append(reminderLabel, reminderInput, saveReminder);
      const reminderSettings = document.createElement("details");
      reminderSettings.className = "reminder-settings";
      reminderSettings.open = reminderSettingsOpen;
      const reminderTitle = document.createElement("summary");
      reminderTitle.textContent = "Reminder settings";
      reminderSettings.append(reminderTitle, reminderControls);
      card.append(reminderSettings);
      if (zone.tapo) {
        const connection = document.createElement("p");
        connection.className = "channel-state";
        connection.textContent = zone.tapo.assigned
          ? `Tapo P110M · ${zone.tapo.host} · MAC ${zone.tapo.mac}`
          : "Pair and assign this plug on the controller computer. Schedules are blocked until assigned.";
        card.append(connection);
      }
    }
    channels.append(card);
  }
  if (selectedChannel !== null && !status.channels.some(zone => zone.channel === selectedChannel)) {
    const message = document.createElement("p");
    message.className = "panel";
    message.textContent = `Channel ${selectedChannel + 1} is not configured. Configure the second board or Tapo plug on the controller computer before use.`;
    channels.append(message);
  }
  if (focusTile !== undefined) {
    zoneGrid.querySelector(`[data-zone-channel="${focusTile}"]`)?.focus({ preventScroll: true });
  } else if (focusDurationSelect && !preserveDurationPicker) {
    document.getElementById(focusDurationSelect)?.focus({ preventScroll: true });
  }
  if (focusChannel !== undefined) {
    const restored = channels.querySelector(`[data-duration-channel="${focusChannel}"]`);
    if (restored && !restored.disabled && !restored.classList.contains("hidden")) {
      restored.focus();
      restored.setSelectionRange(focusStart, focusEnd);
    }
  }
  updated.textContent = `Updated ${new Date().toLocaleTimeString()}`;
}

async function refresh() {
  if (!csrfToken) {
    return;
  }
  const abort = new AbortController();
  const timeout = window.setTimeout(() => abort.abort(), 10000);
  try {
    const result = await request("/api/status", { signal: abort.signal });
    renderStatus(result.status);
  } catch (error) {
    showNotice(`Connection unavailable — output states are unknown. ${error.message}`);
    currentStatus = null;
    rainPauseButton.disabled = true;
    for (const tile of zoneGrid.querySelectorAll("button")) {
      tile.querySelector(".zone-tile-status").textContent = "State unknown";
      tile.setAttribute("aria-label", `${tile.querySelector(".zone-tile-name").textContent}, output state unknown`);
      tile.classList.remove("running", "paused");
      tile.disabled = true;
    }
    backupExport.disabled = true;
    backupRestore.disabled = true;
    updated.textContent = "Status unavailable";
    runningSummary.textContent = "Output states unknown — connection unavailable.";
    for (const state of channels.querySelectorAll(".channel-heading .channel-state")) {
      state.textContent = "Output state unknown — connection unavailable";
    }
    for (const next of channels.querySelectorAll(".channel > .channel-state")) {
      next.textContent = "Next schedule unavailable";
    }
    for (const control of channels.querySelectorAll("button, select, input")) {
      control.disabled = true;
    }
  } finally {
    window.clearTimeout(timeout);
    if (csrfToken) {
      refreshTimer = window.setTimeout(refresh, 5000);
    }
  }
}

async function loadHistory(offset) {
  if (!historyView) return;
  const requestId = ++historyRequestId;
  historyStatus.textContent = "Loading activity history…";
  try {
    const category = historyFiltersAvailable ? historyFilter.value : "all";
    const result = await request(`/api/history?offset=${offset}${category === "all" ? "" : `&filter=${encodeURIComponent(category)}`}`);
    if (requestId !== historyRequestId) return;
    const page = result.history;
    historyOffset = page.offset;
    historyList.replaceChildren();
    for (const event of page.events) {
      const item = document.createElement("li");
      const timestamp = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})$/.exec(event.timestamp);
      const time = timestamp
        ? `${timestamp[3]}/${timestamp[2]}${Number(timestamp[1]) !== new Date().getFullYear() ? `/${timestamp[1]}` : ""} ${timestamp[4]}:${timestamp[5]}`
        : `Invalid timestamp: ${event.timestamp}`;
      const subject = event.channel === null ? "System" : `Channel ${event.channel + 1}, ${event.name}`;
      item.textContent = `${time} — ${subject} — ${event.event} — ${event.reason}`;
      historyList.append(item);
    }
    historyStatus.textContent = page.events.length
      ? `Events ${offset + 1}–${offset + page.events.length} of ${page.total}`
      : "No recorded activity.";
    if (page.warning) {
      historyStatus.textContent += ` — ${page.warning}`;
    }
    historyNewer.disabled = offset === 0;
    historyOlder.disabled = offset + page.page_size >= page.total;
  } catch (error) {
    if (requestId !== historyRequestId) return;
    historyStatus.textContent = `Activity history unavailable: ${error.message}`;
    historyNewer.disabled = true;
    historyOlder.disabled = true;
  }
}

historyNewer.addEventListener("click", () => loadHistory(Math.max(0, historyOffset - 25)));
historyOlder.addEventListener("click", () => loadHistory(historyOffset + 25));
historyRefresh.addEventListener("click", () => loadHistory(0));
historyFilter.addEventListener("change", () => loadHistory(0));

function askBackupPassword(confirm) {
  return new Promise((resolve) => {
    backupPasswordForm.reset();
    backupPassword.type = "password";
    backupPasswordConfirm.type = "password";
    backupPassword.minLength = confirm ? 12 : 1;
    backupPasswordConfirm.required = confirm;
    document.querySelector("#backup-confirm-row").classList.toggle("hidden", !confirm);
    document.querySelector("#backup-password-title").textContent = confirm ? "Choose a backup password" : "Unlock backup";
    const error = document.querySelector("#backup-password-error");
    error.textContent = "";
    let result = null;
    backupPasswordForm.onsubmit = (event) => {
      event.preventDefault();
      if (confirm && backupPassword.value !== backupPasswordConfirm.value) {
        error.textContent = "Passwords do not match. Please enter them again.";
        return;
      }
      result = backupPassword.value;
      backupPasswordDialog.close();
    };
    backupPasswordDialog.onclose = () => {
      backupPasswordForm.reset();
      backupPasswordForm.onsubmit = null;
      backupPasswordDialog.onclose = null;
      resolve(result);
    };
    backupPasswordDialog.showModal();
    backupPassword.focus();
  });
}

backupPasswordShow.addEventListener("change", () => {
  backupPassword.type = backupPasswordShow.checked ? "text" : "password";
  backupPasswordConfirm.type = backupPassword.type;
});
document.querySelector("#backup-password-cancel").addEventListener("click", () => backupPasswordDialog.close());

resumeOperation.addEventListener("click", async () => {
  if (window.confirm("Have you checked device assignments and schedules? Resuming may start equipment immediately. Restored Rain Pause and exemptions will apply.")) {
    await sendCommand("resume_operation");
  }
});

backupExport.addEventListener("click", async () => {
  const password = await askBackupPassword(true);
  if (password === null) return;
  try {
    const result = await request("/api/backup", {
      method: "POST", body: JSON.stringify({password}),
    });
    const url = URL.createObjectURL(new Blob(
      [JSON.stringify(result.archive) + "\n"], { type: "application/json" },
    ));
    const link = document.createElement("a");
    link.href = url;
    link.download = "sprinkler-system.sprinkler-backup";
    link.click();
    URL.revokeObjectURL(url);
    backupStatus.textContent = `Encrypted backup downloaded. Keep a copy on another device and store its password separately. ${result.summary}`;
  } catch (error) {
    backupStatus.textContent = `Backup failed: ${error.message}`;
  }
});

backupRestore.addEventListener("click", () => backupFile.click());
backupFile.addEventListener("change", async () => {
  const file = backupFile.files[0];
  if (!file) return;
  try {
    if (file.size > 8 * 1024 * 1024) throw new Error("Backup file is too large.");
    const archive = JSON.parse(await file.text());
    const password = await askBackupPassword(false);
    if (password === null) return;
    const current = await request("/api/backup-preview", {
      method: "POST", body: JSON.stringify({archive, password}),
    });
    if (!window.confirm(
      `${current.summary}\n\nReplace the saved configuration and history?\n`
      + "All outputs will stop and remain held OFF until you explicitly resume. "
      + "A recovery copy uses the SAME backup password. Restart services afterward, "
      + "review devices and settings, then Resume operation. Service configuration "
      + "reference copies need path review on a replacement computer.",
    )) return;
    const result = await request("/api/restore", {
      method: "POST", body: JSON.stringify({archive, password, revision: current.revision}),
    });
    renderStatus(result.status);
    backupStatus.textContent = `Restored with all outputs held OFF. Restart controller and phone services, then sign in again (the restored phone password applies). Review devices, then Resume operation. Recovery backup: ${result.recovery_backup}. Service setup guide is alongside settings in .restored-service-config/RECOVERY.txt.`;
  } catch (error) {
    backupStatus.textContent = `Restore failed: ${error.message}`;
  } finally {
    backupFile.value = "";
  }
});

async function sendCommand(action, channel, durationMinutes, paused) {
  const body = { action };
  if (channel !== undefined) {
    body.channel = channel;
  }
  if (durationMinutes !== undefined) {
    body.duration_minutes = durationMinutes;
  }
  if (paused !== undefined) {
    body.paused = paused;
  }
  try {
    const result = await request("/api/command", {
      method: "POST",
      body: JSON.stringify(body),
    });
    renderStatus(result.status);
    if (channel !== undefined) {
      const zone = result.status.channels.find(zone => zone.channel === channel);
      if (zone?.fault) showNotice(`Station ${channel + 1} fault: ${zone.fault}. Other stations remain enabled.`);
    }
  } catch (error) {
    await refresh();
    showNotice(error.message);
  }
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const result = await request("/api/login", {
      method: "POST",
      body: JSON.stringify({ password: passwordInput.value }),
    });
    passwordInput.value = "";
    loginMessage.textContent = "";
    loginMessage.classList.remove("visible");
    csrfToken = result.csrf;
    signedIn();
  } catch (error) {
    loginMessage.textContent = error.message;
    loginMessage.classList.add("visible");
  }
});

logoutButton.addEventListener("click", async () => {
  try {
    await request("/api/logout", { method: "POST", body: "{}" });
  } catch (error) {
    showNotice(error.message);
  } finally {
    signedOut();
  }
});

stopAllButton.addEventListener("click", async () => {
  if (window.confirm("Stop EVERYTHING, including garden watering and appliances?")) {
    await sendCommand("stop_all");
  }
});

for (const [id, action, label] of [
  ["stop-garden", "stop_garden", "garden stations 1–8 only"],
  ["stop-appliances", "stop_appliances", "appliance stations 9–16 only"],
]) {
  document.querySelector(`#${id}`).addEventListener("click", async () => {
    if (window.confirm(`Stop ${label}? The other group will not be stopped.`)) {
      await sendCommand(action);
    }
  });
}

rainPauseButton.addEventListener("click", async () => {
  if (!currentStatus) return;
  const paused = !currentStatus.rain_paused;
  if (window.confirm(paused
    ? "Enable Rain Pause? Included channels will stop. Exempt channels can keep running."
    : "Turn Rain Pause off? Currently active schedules may start immediately.")) {
    await sendCommand("rain_pause", undefined, undefined, paused);
  }
});

initializeNavigation();

request("/api/session")
  .then((result) => {
    csrfToken = result.csrf;
    signedIn();
  })
  .catch(() => {
    signedOut();
  });
