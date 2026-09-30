// The kit notify window: a list of what's been sent and received while it's open, and a box to send
// from to any of the devices kit's discovery finds. kit does the sending and receiving; this page
// waits on /api/events for changes and shows them.

const $ = (id) => document.getElementById(id);
const state = { version: -1, devices: [], all: true, picked: new Set(), rows: new Map(), discoverable: true };
window.state = state;   // handy in devtools

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function api(path, options) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  if (!response.ok) {
    const error = new Error((await response.json().catch(() => ({}))).error || response.statusText);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

const post = (path, body) => api(path, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body || {}),
});

function showError(text) {
  $("send-error").textContent = text;
  $("send-error").hidden = !text;
}

const clock = (seconds) => new Date(seconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// --- who a message goes to -------------------------------------------------------------

// Two machines can share a hostname; the address tells them apart.
function labelFor(device) {
  const twins = state.devices.filter((d) => d.name === device.name).length > 1;
  return twins ? `${device.name} (${device.ip})` : device.name;
}

function recipients() {
  return state.all ? state.devices : state.devices.filter((d) => state.picked.has(d.id));
}

function renderPicker() {
  const list = $("to-devices");
  list.innerHTML = "";
  if (!state.devices.length) {
    list.appendChild(element("div", "none", state.discoverable ? "No other devices found yet." : "Discovery is off."));
  }
  for (const device of state.devices) {
    const label = element("label", "check");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = state.all || state.picked.has(device.id);
    box.addEventListener("change", () => {
      if (state.all) {                    // unticking one out of "all": everyone else stays picked
        state.all = false;
        state.picked = new Set(state.devices.map((d) => d.id));
      }
      box.checked ? state.picked.add(device.id) : state.picked.delete(device.id);
      if (state.devices.every((d) => state.picked.has(d.id))) state.all = true;
      renderPicker();
    });
    label.append(box, element("span", "", labelFor(device)), element("span", "ip", device.ip));
    list.appendChild(label);
  }
  $("to-all").checked = state.all;

  const chosen = recipients();
  let text = "To: All devices";
  if (!state.all) text = chosen.length === 0 ? "To: nobody" : chosen.length === 1 ? `To: ${labelFor(chosen[0])}` : `To: ${chosen.length} devices`;
  $("to-button").textContent = text + " ▾";
  $("send").disabled = chosen.length === 0;
}

function toggleMenu(open) {
  const menu = $("to-menu");
  const show = open === undefined ? menu.hidden : open;
  menu.hidden = !show;
  $("to-button").setAttribute("aria-expanded", String(show));
}

// --- the list of messages --------------------------------------------------------------

function renderRecipients(row, entry) {
  const badges = row.querySelector(".recipients");
  badges.innerHTML = "";
  const failures = [];
  for (const r of entry.to) {
    const mark = r.status === "sent" ? "✓ " : r.status === "failed" ? "✗ " : "… ";
    const badge = element("span", `badge ${r.status}`, mark + r.name);
    if (r.error) {
      badge.title = r.error;
      failures.push(`${r.name}: ${r.error}`);
    }
    badges.appendChild(badge);
  }
  const failure = row.querySelector(".failure");
  failure.textContent = failures.join(" · ");
  failure.hidden = !failures.length;
}

function newRow(entry) {
  const row = element("div", `msg ${entry.dir}`);
  const line = element("div", "line");
  if (entry.dir === "in") {
    const who = element("span", "who", "← " + entry.from);
    if (entry.from !== entry.ip) who.title = entry.ip;
    line.appendChild(who);
  } else {
    line.appendChild(element("span", "who", "→ You"));
  }
  line.appendChild(element("span", "when", clock(entry.time)));
  row.appendChild(line);
  if (entry.title) row.appendChild(element("div", "title", entry.title));
  row.appendChild(element("div", "text", entry.message));
  if (entry.dir === "out") {
    row.appendChild(element("div", "recipients"));
    const failure = element("div", "failure");
    failure.hidden = true;
    row.appendChild(failure);
  }
  return row;
}

function renderEntries(entries) {
  const list = $("list");
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  const present = new Set(entries.map((e) => e.id));
  for (const [id, row] of state.rows) {   // the oldest ones, once there are too many to keep
    if (!present.has(id)) { row.remove(); state.rows.delete(id); }
  }
  for (const entry of entries) {
    let row = state.rows.get(entry.id);
    if (!row) {
      row = newRow(entry);
      state.rows.set(entry.id, row);
      list.appendChild(row);
    }
    if (entry.dir === "out") renderRecipients(row, entry);
  }
  $("empty").hidden = entries.length > 0;
  if (atBottom) list.scrollTop = list.scrollHeight;
}

// --- keeping up with kit ---------------------------------------------------------------

function apply(snapshot) {
  const me = snapshot.me;
  state.discoverable = me.discoverable;
  $("me").textContent = `${me.name} · ${me.address} · ` +
    (me.discoverable ? "discoverable on this network" : "reachable, but not discoverable");
  $("no-passphrase").hidden = me.discoverable;

  state.devices = snapshot.devices;
  const count = state.devices.length;
  $("found").textContent = !me.discoverable ? "" :
    count ? `${count} device${count === 1 ? "" : "s"} found${snapshot.searching ? " · looking…" : ""}` :
    snapshot.searching ? "looking for devices…" : "no other devices found";
  $("search-state").textContent = snapshot.searching ? "looking…" : "";
  $("refresh").disabled = !me.discoverable || snapshot.searching;
  renderPicker();
  renderEntries(snapshot.entries);
}

async function listen() {
  let failures = 0;
  for (;;) {
    try {
      const snapshot = await api(`/api/events?since=${state.version}`);
      failures = 0;
      state.version = snapshot.version;
      apply(snapshot);
    } catch (error) {
      console.error("kit notify: update failed", error);
      failures += 1;
      if (error.status === 401 || failures >= 3) {
        $("stopped").hidden = false;
        return;
      }
      await sleep(1000);
    }
  }
}

async function send(event) {
  event.preventDefault();
  const message = $("message").value.trim();
  if (!message) return;
  const to = state.all ? "all" : recipients().map((d) => d.id);
  $("send").disabled = true;
  showError("");
  try {
    await post("/api/send", { message, to });
    $("message").value = "";
    const list = $("list");
    list.scrollTop = list.scrollHeight;   // your own message: always show it
  } catch (error) {
    showError(String(error.message || error));
  } finally {
    renderPicker();
    $("message").focus();
  }
}

$("composer").addEventListener("submit", send);
$("message").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("composer").requestSubmit();
  }
});
$("to-button").addEventListener("click", () => toggleMenu());
$("to-all").addEventListener("change", () => {
  state.all = $("to-all").checked;
  state.picked = new Set();
  renderPicker();
});
$("refresh").addEventListener("click", () => post("/api/refresh").catch(() => {}));
document.addEventListener("click", (event) => {
  if (!$("picker").contains(event.target)) toggleMenu(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") toggleMenu(false);
});
// Closing the window is what stops kit notify; a reload comes straight back, which kit waits a moment for.
window.addEventListener("pagehide", () => navigator.sendBeacon("/api/bye"));

renderPicker();
$("message").focus();
listen();
