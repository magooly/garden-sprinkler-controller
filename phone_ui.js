"use strict";

const loginPanel = document.querySelector("#login-panel");
const loginForm = document.querySelector("#login-form");
const passwordInput = document.querySelector("#password");
const loginMessage = document.querySelector("#login-message");
const controls = document.querySelector("#controls");
const channels = document.querySelector("#channels");
const notice = document.querySelector("#notice");
const updated = document.querySelector("#updated");
const logoutButton = document.querySelector("#logout");
const stopAllButton = document.querySelector("#stop-all");
let csrfToken = "";
let refreshTimer;

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
}

function signedOut() {
  window.clearTimeout(refreshTimer);
  csrfToken = "";
  loginPanel.classList.remove("hidden");
  controls.classList.add("hidden");
  logoutButton.classList.add("hidden");
  channels.replaceChildren();
}

function formatRemaining(seconds) {
  const total = Math.ceil(seconds);
  const minutes = Math.floor(total / 60);
  const remainder = total % 60;
  return minutes ? `${minutes}m ${remainder}s remaining` : `${remainder}s remaining`;
}

function renderStatus(status) {
  channels.replaceChildren();
  if (status.fault) {
    showNotice(`Controller fault: ${status.fault}`);
  } else if (status.rain_paused) {
    showNotice("Rain Pause is enabled. Exempt zones can still be started.");
  } else {
    showNotice("");
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
    if (zone.master) {
      state.textContent = zone.pump_on ? "Master pump · ON" : "Master pump · OFF";
    } else if (zone.running) {
      state.textContent = `${zone.running.source} · ${formatRemaining(zone.running.remaining)}`;
    } else {
      state.textContent = "Off";
    }
    heading.append(identity, state);
    card.append(heading);

    if (!zone.master) {
      const actions = document.createElement("div");
      actions.className = "channel-actions";
      const runButton = document.createElement("button");
      runButton.className = "primary";
      runButton.type = "button";
      runButton.textContent = "RUN 10 MIN";
      runButton.disabled = Boolean(
        status.fault || zone.running || (status.rain_paused && !zone.rain_pause_exempt),
      );
      runButton.addEventListener("click", () => sendCommand("run", zone.channel));
      const stopButton = document.createElement("button");
      stopButton.className = "quiet";
      stopButton.type = "button";
      stopButton.textContent = "STOP";
      stopButton.disabled = !zone.running;
      stopButton.addEventListener("click", () => sendCommand("stop", zone.channel));
      actions.append(runButton, stopButton);
      card.append(actions);
    }
    channels.append(card);
  }
  updated.textContent = `Updated ${new Date().toLocaleTimeString()}`;
}

async function refresh() {
  if (!csrfToken) {
    return;
  }
  try {
    const result = await request("/api/status");
    renderStatus(result.status);
  } catch (error) {
    showNotice(error.message);
    updated.textContent = "Status unavailable";
  } finally {
    if (csrfToken) {
      refreshTimer = window.setTimeout(refresh, 5000);
    }
  }
}

async function sendCommand(action, channel) {
  const body = { action };
  if (channel !== undefined) {
    body.channel = channel;
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
