// The sharing window of kit send: the control panel. kit itself serves the files, so this window only
// adds files (through kit's own file dialog, the built-in browser, or a drop), sets each link's
// expiry and download limit, switches between LAN and internet links, and shows progress.

const EXPIRIES = [["30m", "30 minutes"], ["2h", "2 hours"], ["8h", "8 hours"], ["24h", "24 hours"]];
const LIMITS = [["0", "No limit"], ["1", "Once"], ["2", "2"], ["3", "3"], ["5", "5"], ["10", "10"]];

const $ = (id) => document.getElementById(id);
const state = { shares: new Map(), dialog: false, browsePath: "", mode: "", status: "" };
window.state = state;   // handy in devtools, and what the test harness watches

const humanBytes = (n) => {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return (i === 0 || n >= 100 ? n.toFixed(0) : n.toFixed(1)) + " " + units[i];
};

const humanTime = (seconds) => {
  if (seconds >= 3600) return Math.floor(seconds / 3600) + "h " + String(Math.floor(seconds % 3600 / 60)).padStart(2, "0") + "m";
  if (seconds >= 60) return Math.floor(seconds / 60) + "m";
  return Math.max(0, Math.round(seconds)) + "s";
};

async function api(path, options) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || response.statusText);
  return response.json();
}

const post = (path, body) => api(path, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body || {}),
});

function showError(id, text) {
  const box = $(id);
  box.textContent = text;
  box.hidden = !text;
}

// --- option controls -----------------------------------------------------------------

function fillSelect(select, choices, value) {
  const wanted = String(value);
  const all = choices.some(([key]) => key === wanted) ? choices : [...choices, [wanted, wanted]];
  select.innerHTML = "";
  for (const [key, label] of all) {
    const option = document.createElement("option");
    option.value = key;
    option.textContent = label;
    select.appendChild(option);
  }
  select.value = wanted;
}

const limitValue = (share) => String(share.max || 0);

function optionsFrom(expireSelect, limitSelect) {
  const limit = Number(limitSelect.value || 0);
  return { expire: expireSelect.value, once: limit === 1, max: limit > 1 ? limit : null };
}

const newOptions = () => optionsFrom($("def-expire"), $("def-max"));

// --- adding files --------------------------------------------------------------------

async function addPaths(paths) {
  if (!paths.length) return { added: [], errors: [] };
  const result = await post("/api/add", { paths, ...newOptions() });
  for (const share of result.added) state.shares.set(share.token, share);
  showError("add-error", (result.errors || []).join(" · "));
  render();
  return result;
}

async function chooseFiles() {
  $("choose").disabled = true;
  showError("add-error", "");
  try {
    const { paths } = await post("/api/dialog", {});
    await addPaths(paths);
  } catch (error) {
    showError("add-error", String(error.message || error));
  } finally {
    $("choose").disabled = false;
  }
}

// A page is never told where a dropped file lives, so it hands kit a copy (over loopback, so it's quick).
function upload(file, onProgress) {
  const options = newOptions();
  const query = new URLSearchParams({
    name: file.name, expire: options.expire, once: options.once ? "1" : "", max: options.max || "",
  });
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", "/api/upload?" + query);
    request.upload.onprogress = (event) => { if (event.lengthComputable) onProgress(event.loaded / event.total); };
    request.onload = () => {
      let body = {};
      try { body = JSON.parse(request.responseText); } catch {}
      request.status === 200 ? resolve(body) : reject(new Error(body.error || request.statusText));
    };
    request.onerror = () => reject(new Error("kit isn't answering"));
    request.send(file);
  });
}

async function addDropped(files) {
  const problems = [];
  const hint = $("add-hint");
  const before = hint.textContent;
  for (const file of files) {
    if (!file.size) {                     // folders arrive like this too, and have nothing to send
      problems.push(`${file.name} is empty, or is a folder - use Browse for folders`);
      continue;
    }
    try {
      const share = await upload(file, (fraction) => {
        hint.textContent = `copying ${file.name} to kit… ${(fraction * 100).toFixed(0)}%`;
      });
      state.shares.set(share.token, share);
      render();
    } catch (error) {
      problems.push(`${file.name}: ${error.message || error}`);
    }
  }
  hint.textContent = before;
  showError("add-error", problems.join(" · "));
  render();
}

// --- the file browser ----------------------------------------------------------------

let browseRequest = 0;

async function loadBrowse(path) {
  const mine = ++browseRequest;   // a slow folder must not land on top of a newer one
  showError("browse-error", "");
  let data;
  try {
    data = await api("/api/browse?path=" + encodeURIComponent(path || ""));
  } catch (error) {
    if (mine === browseRequest) showError("browse-error", String(error.message || error));
    return;
  }
  if (mine !== browseRequest) return;
  state.browsePath = data.path;
  // Don't take the path box away from someone in the middle of typing in it.
  if (document.activeElement !== $("browse-path")) $("browse-path").value = data.path;
  $("browse-up").disabled = !data.parent;

  const places = $("places");
  places.innerHTML = "";
  for (const place of data.places) {
    const button = document.createElement("button");
    button.className = "link-ish";
    button.textContent = place.label;
    button.addEventListener("click", () => loadBrowse(place.path));
    places.appendChild(button);
  }

  const listing = $("listing");
  listing.innerHTML = "";
  if (!data.entries.length) {
    listing.innerHTML = '<div class="hint" style="padding:10px">This folder is empty.</div>';
  }
  for (const entry of data.entries) {
    const row = document.createElement("button");
    row.className = "entry" + (entry.dir ? " folder" : "");
    row.innerHTML = `<span class="what"></span><span class="name" style="flex:1"></span>
                     <span class="size"></span><span class="when"></span>`;
    row.querySelector(".what").textContent = entry.dir ? "▸" : "·";
    row.querySelector(".name").textContent = entry.name;
    row.querySelector(".size").textContent = entry.dir ? "" : humanBytes(entry.size);
    row.querySelector(".when").textContent = new Date(entry.modified * 1000).toLocaleDateString();
    row.addEventListener("click", async () => {
      if (entry.dir) return loadBrowse(entry.path);
      row.disabled = true;
      const result = await addPaths([entry.path]).catch((error) => {
        showError("browse-error", String(error.message || error));
        return { added: [] };
      });
      row.querySelector(".what").textContent = result.added.length ? "✓" : "·";
      row.disabled = false;
    });
    listing.appendChild(row);
  }
  if (data.truncated) {
    const note = document.createElement("div");
    note.className = "hint";
    note.style.padding = "10px";
    note.textContent = "Only the first " + data.entries.length + " items are listed.";
    listing.appendChild(note);
  }
}

// --- history -------------------------------------------------------------------------

async function loadHistory() {
  const list = $("history-list");
  let data;
  try {
    data = await api("/api/history");
  } catch (error) {
    list.textContent = String(error.message || error);
    return;
  }
  list.innerHTML = "";
  if (!data.entries.length) {
    list.innerHTML = '<div class="hint">Nothing yet.</div>';
    return;
  }
  for (const entry of data.entries) {
    const row = document.createElement("div");
    row.className = "hist";
    row.innerHTML = `<span class="name" style="flex:1"></span><span class="size"></span><span class="when"></span>`;
    row.querySelector(".name").textContent = entry.name;
    row.querySelector(".size").textContent =
      humanBytes(entry.size) + " · " + (entry.downloads ? `sent ${entry.downloads}×` : "not sent");
    row.querySelector(".when").textContent = new Date(entry.started).toLocaleString();
    row.title = [entry.folder, entry.note].filter(Boolean).join(" — ");
    list.appendChild(row);
  }
}

const historyOpen = () => !$("history").hidden;

// --- share cards ---------------------------------------------------------------------

function buildCard(share) {
  const card = document.createElement("div");
  card.className = "card";
  card.id = "s-" + share.token;
  card.innerHTML = `
    <div class="row">
      <span class="name"></span><span class="size"></span>
      <span class="spacer"></span>
      <span class="status waiting"><span class="dot"></span><span class="label"></span></span>
    </div>
    <input class="link" readonly aria-label="Share link">
    <div class="row" style="margin-top:8px">
      <button class="copy">Copy link</button>
      <button class="qr-toggle">QR code</button>
      <button class="notify-toggle">Notify…</button>
      <span class="copied" hidden>copied</span>
      <span class="spacer"></span>
      <label class="opt">lasts <select class="expire"></select></label>
      <label class="opt">downloads <select class="max"></select></label>
      <button class="remove link-ish">Remove</button>
    </div>
    <div class="qr" hidden><img alt="QR code for this link"></div>
    <div class="notify" hidden>
      <div class="notify-list"></div>
      <div class="row notify-actions">
        <button class="primary notify-send" disabled>Send link</button>
        <button class="ghost notify-again">Look again</button>
        <span class="meta notify-state"></span>
      </div>
      <div class="notify-results"></div>
    </div>
    <div class="transfers"></div>`;

  card.querySelector(".name").textContent = share.name;
  card.querySelector(".size").textContent = humanBytes(share.size);
  const input = card.querySelector(".link");
  input.value = share.link || "";

  card.querySelector(".copy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(input.value);
    } catch {
      input.select();
      document.execCommand("copy");
    }
    const flag = card.querySelector(".copied");
    flag.hidden = false;
    setTimeout(() => { flag.hidden = true; }, 1500);
  });

  const qr = card.querySelector(".qr");
  card.querySelector(".qr-toggle").addEventListener("click", () => {
    qr.hidden = !qr.hidden;
    const image = qr.querySelector("img");
    if (!qr.hidden && !image.getAttribute("src")) {
      image.src = "/api/qr?token=" + encodeURIComponent(share.token) + "&v=" + encodeURIComponent(image.dataset.link || "");
    }
  });

  setUpNotify(card, share);

  const expire = card.querySelector(".expire");
  const max = card.querySelector(".max");
  const change = async () => {
    try {
      const updated = await post("/api/options", { token: share.token, ...optionsFrom(expire, max) });
      state.shares.set(updated.token, { ...state.shares.get(updated.token), ...updated });
      render();
    } catch (error) {
      showError("add-error", String(error.message || error));
      await refresh().catch(() => {});
    }
  };
  expire.addEventListener("change", change);
  max.addEventListener("change", change);

  card.querySelector(".remove").addEventListener("click", async () => {
    await post("/api/remove", { token: share.token }).catch(() => {});
    await refresh().catch(() => {});
    if (historyOpen()) loadHistory();
  });
  return card;
}

function render() {
  const files = $("files");
  for (const share of state.shares.values()) {
    let card = document.getElementById("s-" + share.token);
    if (!card) {
      card = buildCard(share);
      files.appendChild(card);
      fillSelect(card.querySelector(".expire"), EXPIRIES, share.expires_in >= 3600
        ? Math.round(share.expires_in / 3600) + "h" : Math.max(1, Math.round(share.expires_in / 60)) + "m");
      fillSelect(card.querySelector(".max"), LIMITS, limitValue(share));
    }
    const status = card.querySelector(".status");
    const label = card.querySelector(".label");
    const live = share.active || [];
    status.className = "status " + (share.dead ? "dead" : live.length ? "live" : share.downloads ? "done" : "waiting");
    label.textContent = share.dead
      ? share.dead
      : live.length ? `sending to ${live.length}`
      : share.downloads ? `sent ${share.downloads}× · ${humanTime(share.expires_in)} left`
      : `waiting · ${humanTime(share.expires_in)} left`;
    const ready = !!share.link && !share.dead;
    card.querySelector(".link").value = share.link
      || (state.status === "starting" ? "getting a link…" : "no link - " + (state.status || "stopped"));
    card.querySelector(".copy").disabled = !ready;
    card.querySelector(".qr-toggle").disabled = !ready;
    card.querySelector(".notify-toggle").disabled = !ready;
    if (!ready) card.querySelector(".notify").hidden = true;
    card.querySelector(".remove").disabled = !!share.dead;
    showTransfers(card, share);
    const expire = card.querySelector(".expire");
    const max = card.querySelector(".max");
    expire.disabled = max.disabled = !!share.locked;
    if (max.value !== limitValue(share) && !max.matches(":focus")) max.value = limitValue(share);
    const qr = card.querySelector(".qr");
    const image = qr.querySelector("img");
    if (!ready) qr.hidden = true;
    if ((image.dataset.link || "") !== (share.link || "")) {   // a new mode means a new link, and a new code
      image.dataset.link = share.link || "";
      image.removeAttribute("src");
      qr.hidden = true;
    }
  }
  $("empty").hidden = state.shares.size > 0;
}

// --- notify: send a link to the kit machines on this network ----------------------------

function setUpNotify(card, share) {
  const panel = card.querySelector(".notify");
  const list = card.querySelector(".notify-list");
  const sendButton = card.querySelector(".notify-send");
  const stateText = card.querySelector(".notify-state");
  const results = card.querySelector(".notify-results");

  const picked = () => [...list.querySelectorAll("input[data-id]")].filter((box) => box.checked);
  const updateSend = () => { sendButton.disabled = picked().length === 0; };

  async function lookAround() {
    list.innerHTML = "";
    results.innerHTML = "";
    sendButton.disabled = true;
    stateText.textContent = "looking for machines…";
    let data;
    try {
      data = await api("/api/devices");
    } catch (error) {
      stateText.textContent = String(error.message || error);
      return;
    }
    if (!data.passphrase) {
      stateText.textContent = "";
      list.innerHTML = '<div class="hint">No machines can be found until notify.passphrase is set, the same on ' +
        'every machine: <code>kit config set notify.passphrase &lt;same value everywhere&gt;</code></div>';
      return;
    }
    if (!data.devices.length) {
      stateText.textContent = "no other machines answered - are they running kit notify?";
      return;
    }
    stateText.textContent = `${data.devices.length} found`;
    const all = document.createElement("label");
    all.className = "check";
    all.innerHTML = '<input type="checkbox" checked> All machines';
    const allBox = all.querySelector("input");
    list.appendChild(all);
    for (const device of data.devices) {
      const row = document.createElement("label");
      row.className = "check";
      row.innerHTML = '<input type="checkbox" checked><span class="who"></span><span class="ip"></span>';
      row.querySelector("input").dataset.id = device.id;
      row.querySelector(".who").textContent = device.name;
      row.querySelector(".ip").textContent = device.ip;
      row.querySelector("input").addEventListener("change", () => {
        allBox.checked = picked().length === data.devices.length;
        updateSend();
      });
      list.appendChild(row);
    }
    allBox.addEventListener("change", () => {
      for (const box of list.querySelectorAll("input[data-id]")) box.checked = allBox.checked;
      updateSend();
    });
    updateSend();
  }

  card.querySelector(".notify-toggle").addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) lookAround();
  });
  card.querySelector(".notify-again").addEventListener("click", lookAround);

  sendButton.addEventListener("click", async () => {
    const boxes = list.querySelectorAll("input[data-id]");
    const ids = picked().map((box) => box.dataset.id);
    sendButton.disabled = true;
    stateText.textContent = "sending…";
    try {
      const reply = await post("/api/notify", { token: share.token, to: ids.length === boxes.length ? "all" : ids });
      results.innerHTML = "";
      for (const result of reply.results) {
        const badge = document.createElement("span");
        badge.className = "badge " + (result.ok ? "sent" : "failed");
        badge.textContent = (result.ok ? "✓ " : "✗ ") + result.name;
        if (result.error) badge.title = result.error;
        results.appendChild(badge);
      }
      const failed = reply.results.filter((r) => !r.ok).length;
      stateText.textContent = failed ? `${failed} didn't get it - hover a ✗ for why` : "sent";
    } catch (error) {
      stateText.textContent = String(error.message || error);
    } finally {
      updateSend();
    }
  });
}

// One row per download in progress, as kit reports them.
function showTransfers(card, share) {
  const box = card.querySelector(".transfers");
  const seen = new Set();
  for (const transfer of share.active || []) {
    const id = "t-" + share.token + "-" + transfer.id;
    seen.add(id);
    let row = document.getElementById(id);
    if (!row) {
      row = document.createElement("div");
      row.className = "transfer";
      row.id = id;
      row.innerHTML = `<div class="row"><span class="who"></span><span class="spacer"></span><span class="meta pace"></span></div><div class="progress"><i></i></div>`;
      row.querySelector(".who").textContent = "sending to " + transfer.who;
      box.appendChild(row);
    }
    const fraction = transfer.size ? transfer.done / transfer.size : 1;
    row.querySelector("i").style.width = (fraction * 100).toFixed(1) + "%";
    row.querySelector(".pace").textContent =
      `${humanBytes(transfer.done)} of ${humanBytes(transfer.size)} · ${humanBytes(transfer.rate)}/s`;
  }
  for (const row of [...box.children]) if (!seen.has(row.id)) row.remove();
}

// --- start --------------------------------------------------------------------------

function applyState(data) {
  state.dialog = !!data.dialog;
  state.mode = data.mode;
  state.status = data.status;
  for (const share of data.shares) {
    const existing = state.shares.get(share.token);
    state.shares.set(share.token, { ...(existing || {}), ...share });
  }
  const live = data.shares.filter((share) => !share.dead).length;
  $("head").textContent = live
    ? `${live} link${live === 1 ? "" : "s"} live · kit does the sending`
    : data.shares.length ? "every link is closed - add another file, or stop sharing"
    : "add a file to get a link";
  for (const button of document.querySelectorAll("[data-mode]")) {
    button.setAttribute("aria-pressed", String(button.dataset.mode === data.mode));
  }
  const reach = $("reach");
  reach.className = "meta" + (data.status && data.status !== "starting" ? " bad" : "");
  reach.textContent = data.status === "starting"
    ? (data.mode === "internet" ? "opening an internet link… this takes a few seconds" : "starting…")
    : data.status ? data.status
    : "links reach " + data.reach;
  render();
  return data;
}

const refresh = async () => applyState(await api("/api/state"));

// Quick while something is moving, slow when nothing is.
async function poll() {
  await refresh().catch(() => {});
  const busy = state.status === "starting" || [...state.shares.values()].some((s) => (s.active || []).length);
  setTimeout(poll, busy ? 1000 : 4000);
}

async function main() {
  const data = await refresh();
  fillSelect($("def-expire"), EXPIRIES, data.defaults.expire || "2h");
  fillSelect($("def-max"), LIMITS, data.defaults.once ? "1" : String(data.defaults.max || 0));
  $("choose").disabled = !data.dialog;
  if (!data.dialog) {
    $("add-hint").textContent =
      "This system has no file dialog kit can open - use Browse, or drop files onto this window.";
  }
  if (data.kind === "public") $("mode-internet").textContent = "Internet (public address)";
  setTimeout(poll, 1000);
}

// --- window wiring -------------------------------------------------------------------

$("choose").addEventListener("click", chooseFiles);

$("browse-toggle").addEventListener("click", () => {
  const panel = $("browse");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) loadBrowse(state.browsePath);
});
$("browse-close").addEventListener("click", () => { $("browse").hidden = true; });
$("browse-go").addEventListener("click", () => loadBrowse($("browse-path").value));
$("browse-path").addEventListener("keydown", (event) => {
  if (event.key === "Enter") loadBrowse($("browse-path").value);
});
$("browse-up").addEventListener("click", async () => {
  const data = await api("/api/browse?path=" + encodeURIComponent(state.browsePath)).catch(() => null);
  if (data && data.parent) loadBrowse(data.parent);
});

$("history-toggle").addEventListener("click", () => {
  const panel = $("history");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) loadHistory();
});
$("history-clear").addEventListener("click", async () => {
  await post("/api/history/clear", {}).catch(() => {});
  loadHistory();
});

const hasFiles = (event) => event.dataTransfer && [...event.dataTransfer.types].includes("Files");
let dragDepth = 0;
window.addEventListener("dragenter", (event) => {
  if (!hasFiles(event)) return;
  dragDepth += 1;
  $("dropzone").classList.add("on");
});
window.addEventListener("dragover", (event) => {
  if (!hasFiles(event)) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
});
window.addEventListener("dragleave", () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) $("dropzone").classList.remove("on");
});
window.addEventListener("drop", async (event) => {
  if (!hasFiles(event)) return;
  event.preventDefault();
  dragDepth = 0;
  $("dropzone").classList.remove("on");
  await addDropped([...event.dataTransfer.files]);
});

for (const button of document.querySelectorAll("[data-mode]")) {
  button.addEventListener("click", async () => {
    if (button.getAttribute("aria-pressed") === "true" && !state.status) return;
    try {
      applyState(await post("/api/mode", { mode: button.dataset.mode }));
    } catch (error) {
      showError("add-error", String(error.message || error));
    }
  });
}

$("stop").addEventListener("click", async () => {
  $("stop").disabled = true;
  await post("/api/stop", {}).catch(() => {});
  await refresh().catch(() => {});
});

main().catch((error) => { $("head").textContent = "couldn't start: " + error.message; });
