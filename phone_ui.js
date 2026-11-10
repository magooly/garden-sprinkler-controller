"use strict";

const loginPanel = document.querySelector("#login-panel");
const loginForm = document.querySelector("#login-form");
const passwordInput = document.querySelector("#password");
const loginMessage = document.querySelector("#login-message");
const controls = document.querySelector("#controls");
const channels = document.querySelector("#channels");
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
  loadHistory(0);
}

function signedOut() {
  historyRequestId += 1;
  window.clearTimeout(refreshTimer);
  csrfToken = "";
  loginPanel.classList.remove("hidden");
  controls.classList.add("hidden");
  logoutButton.classList.add("hidden");
  channels.replaceChildren();
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

function renderStatus(status) {
  const focused = document.activeElement;
  const focusChannel = focused?.dataset.durationChannel;
  const focusStart = focusChannel !== undefined ? focused.selectionStart : null;
  const focusEnd = focusChannel !== undefined ? focused.selectionEnd : null;
  channels.replaceChildren();
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
          item.textContent = `Channel ${zone.channel + 1}, ${zone.name} — ${zone.running.source} — ${end}`;
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
      state.textContent = "Unassigned Tapo plug — output unavailable";
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
    heading.append(identity, state);
    card.append(heading);
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
      durationSelect.disabled = Boolean(status.fault || status.operation_held || zone.available === false || zone.running || !status.manual_timers_supported);
      const durationInput = document.createElement("input");
      durationInput.type = "text";
      durationInput.inputMode = "numeric";
      durationInput.placeholder = "Minutes (1–1440)";
      durationInput.setAttribute("aria-label", `Custom minutes for ${zone.name}`);
      durationInput.dataset.durationChannel = String(zone.channel);
      durationInput.value = duration.custom;
      durationInput.classList.toggle("hidden", duration.choice !== "custom");
      durationInput.disabled = durationSelect.disabled;
      const durationSummary = document.createElement("span");
      durationSummary.className = "duration-summary";
      const updateDurationSummary = () => {
        const value = duration.choice === "custom" ? duration.custom.trim() : duration.choice;
        const minutes = /^[0-9]+$/.test(value) && Number(value) >= 1 && Number(value) <= 1440
          ? `${Number(value)} minutes` : "the custom minutes entered";
        durationSummary.textContent = `If started: Channel ${zone.channel + 1}, ${zone.name} — ${minutes}. Select a duration, then press RUN.`;
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
        status.fault || status.operation_held || zone.available === false || zone.running || !status.manual_timers_supported
        || (status.rain_paused && !zone.rain_pause_exempt),
      );
      runButton.addEventListener("click", () => {
        const value = duration.choice === "custom" ? duration.custom.trim() : duration.choice;
        if (!/^[0-9]+$/.test(value) || Number(value) < 1 || Number(value) > 1440) {
          showNotice("Enter a manual duration from 1 to 1440 minutes.");
          return;
        }
        if (zone.max_manual_minutes !== null && Number(value) > zone.max_manual_minutes) {
          showNotice(`This channel's manual maximum is ${zone.max_manual_minutes} minutes.`);
          return;
        }
        sendCommand("run", zone.channel, Number(value));
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
          + "Controller restart or a hardware fault will stop it.",
        )) {
          sendCommand("continuous", zone.channel);
        }
      });
      const stopButton = document.createElement("button");
      stopButton.className = "quiet";
      stopButton.type = "button";
      stopButton.textContent = "STOP";
      stopButton.disabled = !zone.running;
      stopButton.addEventListener("click", () => sendCommand("stop", zone.channel));
      actions.append(runButton, stopButton);
      if (zone.pump_exempt === true || zone.channel >= 8) {
        actions.append(continuousButton);
      }
      card.append(actions);
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
      reminderInput.disabled = Boolean(status.fault || !status.continuous_reminders_supported);
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
      card.append(reminderControls);
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
  const requestId = ++historyRequestId;
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

async function sendCommand(action, channel, durationMinutes) {
  const body = { action };
  if (channel !== undefined) {
    body.channel = channel;
  }
  if (durationMinutes !== undefined) {
    body.duration_minutes = durationMinutes;
  }
  try {
    const result = await request("/api/command", {
      method: "POST",
      body: JSON.stringify(body),
    });
    renderStatus(result.status);
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
  if (window.confirm("Stop all watering now?")) {
    await sendCommand("stop_all");
  }
});

request("/api/session")
  .then((result) => {
    csrfToken = result.csrf;
    signedIn();
  })
  .catch(() => {
    signedOut();
  });
