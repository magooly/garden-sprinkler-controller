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

document.querySelector("#tapo-metric").addEventListener("change", drawTapoSamples);

function drawTapoSamples() {
  const svg = document.querySelector("#tapo-sample-graph");
  svg.replaceChildren();
  if (!tapoSampleData) return;
  const metric = document.querySelector("#tapo-metric").value;
  const value = row => row.error || row[metric] === null || row[metric] === undefined
    ? null : metric === "state" ? Number(row.state) : row[metric];
  const rows = tapoSampleData.samples || [];
  const valid = rows.map(value).filter(v => v !== null && Number.isFinite(v));
  const max = Math.max(1, ...valid);
  const start = Date.parse(tapoSampleData.start);
  const ns = "http://www.w3.org/2000/svg";
  let previous = null;
  for (const row of rows) {
    const number = value(row);
    if (number === null || !Number.isFinite(number)) {
      previous = null;
      continue;
    }
    const x = 50 + (Date.parse(row.timestamp) - start) / 86400000 * 735;
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
    title.textContent = `${new Date(row.updated || row.timestamp).toLocaleString()}: ${number}`;
    dot.append(title); svg.append(dot);
    previous = {x,y};
  }
  for (const [x,y,text] of [[3,25,max.toFixed(3)],[3,140,"0"],[50,170,"24h ago"],[710,170,"Now"]]) {
    const label = document.createElementNS(ns, "text");
    label.setAttribute("x", String(x)); label.setAttribute("y", String(y)); label.setAttribute("fill", "white");
    label.textContent = text; svg.append(label);
  }
  document.querySelector("#tapo-sample-status").textContent =
    `${valid.length} available ${document.querySelector("#tapo-metric").selectedOptions[0].textContent} readings out of ${rows.length} intervals. `
    + "Recording starts when enabled; earlier measurements cannot be reconstructed. "
    + (tapoSampleData.samples_warning || "");
}

function closeTapoGraph() {
  tapoGraphChannel = null;
  tapoGraphRequestId += 1;
  tapoGraph.classList.add("hidden");
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
        row.state === null ? "Unavailable" : row.state ? "ON" : "OFF",
        ...["power_w", "today_kwh", "month_kwh", "voltage_v", "current_a"]
          .map(key => row[key] === null ? "Unavailable" : row[key].toFixed(3)),
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

async function openTapoGraph(channel) {
  if (channel === null || !tapoView) return;
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
  graph.replaceChildren();
  document.querySelector("#tapo-requested-timeline").replaceChildren();
  document.querySelector("#tapo-requested-panel").classList.add("hidden");
  tapoSampleData = null;
  drawTapoSamples();
  document.querySelector("#tapo-sample-rows").replaceChildren();
  document.querySelector("#tapo-sample-status").textContent = "";
  periods.replaceChildren();
  events.replaceChildren();
  document.querySelector("#tapo-timeline-range").textContent = "";
  status.textContent = "Loading controller history...";
  refreshButton.disabled = true;
  try {
    const {timeline} = await request(`/api/tapo-timeline?channel=${channel}`);
    if (id !== tapoGraphRequestId || !tapoView || tapoGraphChannel !== channel) return;
    status.textContent = `${timeline.name} — confirmed/recorded ON: ${(timeline.on_seconds / 3600).toFixed(2)} hours. ${timeline.description} ${timeline.warning}`;
    tapoSampleData = timeline;
    drawTapoSamples();
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
    const local = value => new Date(value).toLocaleString();
    document.querySelector("#tapo-timeline-range").textContent = `${local(timeline.start)} — ${local(timeline.end)}`;
    for (const row of timeline.segments) {
      const label = `${local(row.start)} — ${local(row.end)}: ${row.state} (${(row.seconds / 60).toFixed(1)} minutes)`;
      const band = document.createElement("span");
      band.style.width = `${row.seconds / 86400 * 100}%`;
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
        band.style.width = `${row.seconds / 86400 * 100}%`;
        band.style.backgroundColor = {ON:"#238636",OFF:"#3178b6",Unknown:"#777777"}[row.state];
        band.title = `Requested ${row.state}: ${local(row.start)} — ${local(row.end)}`;
        requestedGraph.append(band);
      }
    }
  } catch (error) {
    if (id === tapoGraphRequestId && tapoView) status.textContent = `History unavailable: ${error.message}`;
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
        ? "Continuous · ON until stopped"
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
      next.textContent = `Next scheduled start (controller local time): ${zone.next_start
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
      pauseHelp.textContent = "Each group applies across all weekdays. Pausing stops that group's scheduled run, not manual watering. Unpausing waits for its next start.";
      const groups = document.createElement("div");
      groups.className = "pause-groups";
      for (let group = 0; group < 4; group += 1) {
        const paused = zone.paused_windows?.[group];
        const pause = document.createElement("button");
        pause.type = "button";
        pause.className = paused ? "danger" : "quiet";
        pause.textContent = `Group ${group + 1}: ${paused ? "Paused" : "Not Paused"}`;
        pause.setAttribute("aria-pressed", String(Boolean(paused)));
        pause.disabled = typeof paused !== "boolean" || Boolean(
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
