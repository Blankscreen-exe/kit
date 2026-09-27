// The sending half of kit send. This page holds the peer connection and streams the file out of kit's
// local server, because browsers do WebRTC far better than anything we could add to Python.
//
// It is also the control panel: files are added here (through kit's own file dialog, the built-in
// browser, or a drop), and each link's expiry and download limit are set here.
//
// Numbers below come from measurement, not taste (see the tool's README):
//   - the file is read in ranged slices, never one fetch: a single fetch of a 1 GB file cost 1.6 GB of RSS
//   - 64 KB data-channel chunks, pause above 4 MB buffered, resume at 1 MB
//   - bufferedamountlow only fires when crossing the threshold, so draining polls as well as listens
const CHUNK = 65536;
const LOW = 1 << 20;
const HIGH = 4 << 20;
const HASH_BATCH = 64;        // chunks hashed before waiting, so the hash queue can't grow without bound
const STALL_MS = 30000;       // no progress for this long: give up on that transfer
const ICE = [{ urls: "stun:stun.l.google.com:19302" }, { urls: "stun:stun.cloudflare.com:3478" }];

const EXPIRIES = [["30m", "30 minutes"], ["2h", "2 hours"], ["8h", "8 hours"], ["24h", "24 hours"]];
const LIMITS = [["0", "No limit"], ["1", "Once"], ["2", "2"], ["3", "3"], ["5", "5"], ["10", "10"]];

const $ = (id) => document.getElementById(id);
// A fresh id every load: kit uses it to drop files an older window was holding.
const SESSION = Math.random().toString(36).slice(2) + Date.now().toString(36);

const state = {
  peer: null, slice: 8 << 20, shares: new Map(), transfers: new Map(),
  files: new Map(),       // token -> the File a drop handed us (kit never sees these bytes)
  dialog: false, browsePath: "",
};
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

const hex = (buffer) => [...new Uint8Array(buffer)].map((b) => b.toString(16).padStart(2, "0")).join("");

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

const report = (body) => post("/api/event", body).catch(() => ({ ok: true }));
//        ^ kit going away must not break a transfer in flight

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

async function addDropped(files) {
  const problems = [];
  for (const file of files) {
    if (!file.size) {                     // folders arrive like this too, and have nothing to send
      problems.push(`${file.name} is empty, or is a folder - use Browse for folders`);
      continue;
    }
    try {
      const share = await post("/api/add-window", { name: file.name, size: file.size, ...newOptions() });
      state.files.set(share.token, file);
      state.shares.set(share.token, share);
    } catch (error) {
      problems.push(`${file.name}: ${error.message || error}`);
    }
  }
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
      <span class="badge held" hidden>held by this window</span>
      <span class="spacer"></span>
      <span class="status waiting"><span class="dot"></span><span class="label"></span></span>
    </div>
    <input class="link" readonly aria-label="Share link">
    <div class="row" style="margin-top:8px">
      <button class="copy">Copy link</button>
      <button class="qr-toggle">QR code</button>
      <span class="copied" hidden>copied</span>
      <span class="spacer"></span>
      <label class="opt">lasts <select class="expire"></select></label>
      <label class="opt">downloads <select class="max"></select></label>
      <button class="remove link-ish">Remove</button>
    </div>
    <div class="qr" hidden><img alt="QR code for this link"></div>
    <div class="transfers"></div>`;

  card.querySelector(".name").textContent = share.name;
  card.querySelector(".size").textContent = humanBytes(share.size);
  const input = card.querySelector(".link");
  input.value = share.link;

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
    if (!qr.hidden && !image.src) image.src = "/api/qr?token=" + encodeURIComponent(share.token);
  });

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
    for (const transfer of state.transfers.values()) {
      if (transfer.token === share.token) transfer.cancelled = "you removed this link";
    }
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
    const live = [...state.transfers.values()].filter((t) => t.token === share.token);
    status.className = "status " + (share.dead ? "dead" : live.length ? "live" : share.downloads ? "done" : "waiting");
    label.textContent = share.dead
      ? share.dead
      : live.length ? `sending to ${live.length}`
      : share.downloads ? `sent ${share.downloads}× · ${humanTime(share.expires_in)} left`
      : `waiting · ${humanTime(share.expires_in)} left`;
    card.querySelector(".link").value = share.link;
    card.querySelector(".held").hidden = share.source !== "window";
    card.querySelector(".copy").disabled = !!share.dead;
    card.querySelector(".qr-toggle").disabled = !!share.dead;
    card.querySelector(".remove").disabled = !!share.dead;
    const expire = card.querySelector(".expire");
    const max = card.querySelector(".max");
    expire.disabled = max.disabled = !!share.locked;
    if (max.value !== limitValue(share) && !max.matches(":focus")) max.value = limitValue(share);
    if (share.dead) {
      const qr = card.querySelector(".qr");
      qr.hidden = true;
    }
  }
  $("empty").hidden = state.shares.size > 0;
}

function transferRow(transfer) {
  const card = document.getElementById("s-" + transfer.token);
  if (!card) return null;
  let row = document.getElementById("t-" + transfer.id);
  if (!row) {
    row = document.createElement("div");
    row.className = "transfer";
    row.id = "t-" + transfer.id;
    row.innerHTML = `<div class="row"><span class="who"></span><span class="spacer"></span><span class="meta pace"></span></div><div class="bar"><i></i></div>`;
    row.querySelector(".who").textContent = "sending to " + transfer.peer.slice(0, 12);
    card.querySelector(".transfers").appendChild(row);
  }
  return row;
}

function showProgress(transfer) {
  const row = transferRow(transfer);
  if (!row) return;
  const share = state.shares.get(transfer.token);
  const fraction = share.size ? transfer.sent / share.size : 0;
  row.querySelector("i").style.width = (fraction * 100).toFixed(1) + "%";
  const seconds = (performance.now() - transfer.started) / 1000;
  row.querySelector(".pace").textContent =
    `${humanBytes(transfer.sent)} of ${humanBytes(share.size)} · ${humanBytes(transfer.sent / Math.max(seconds, 0.1))}/s`;
}

function finishRow(transfer, text, good) {
  const row = transferRow(transfer);
  if (!row) return;
  row.querySelector(".pace").textContent = text;
  row.querySelector(".pace").style.color = good ? "var(--good)" : "var(--bad)";
  if (good) row.querySelector("i").style.width = "100%";
}

// --- sending ------------------------------------------------------------------------

// Resolves once the channel has drained to `limit`. bufferedamountlow only fires on crossing the
// threshold, so waiting for 0 at the end of a file hangs unless we poll too.
function drain(channel, limit, transfer) {
  if (channel.bufferedAmount <= limit) return Promise.resolve();
  return new Promise((resolve, reject) => {
    let last = channel.bufferedAmount;
    let lastMoved = performance.now();
    const finish = (error) => {
      channel.removeEventListener("bufferedamountlow", check);
      clearInterval(timer);
      error ? reject(error) : resolve();
    };
    const check = () => {
      if (transfer.cancelled) return finish(new Error(transfer.cancelled));
      if (channel.bufferedAmount <= limit) return finish();
      if (channel.bufferedAmount < last) { last = channel.bufferedAmount; lastMoved = performance.now(); }
      if (performance.now() - lastMoved > STALL_MS) finish(new Error("the transfer stalled"));
    };
    channel.addEventListener("bufferedamountlow", check);
    const timer = setInterval(check, 100);
  });
}

// One slice of the file: from kit for a file it holds, straight from the File for a dropped one.
async function readSlice(share, offset, end) {
  if (share.source === "window") {
    const file = state.files.get(share.token);
    if (!file) throw new Error("this window doesn't hold that file any more - drop it in again");
    return new Uint8Array(await file.slice(offset, end + 1).arrayBuffer());
  }
  const response = await fetch("/api/bytes?token=" + encodeURIComponent(share.token),
    { credentials: "same-origin", headers: { Range: `bytes=${offset}-${end}` } });
  if (!response.ok) throw new Error("kit couldn't read the file (" + response.status + ")");
  return new Uint8Array(await response.arrayBuffer());
}

async function sendFile(conn, share, transfer) {
  const channel = conn.dataChannel;
  channel.bufferedAmountLowThreshold = LOW;
  conn.send(JSON.stringify({
    kind: "header", name: share.name, size: share.size, chunk: CHUNK,
    chunks: Math.ceil(share.size / CHUNK),
  }));

  const digests = [];
  let pending = Promise.resolve();
  let sinceReport = 0;

  for (let offset = 0; offset < share.size; offset += state.slice) {
    if (transfer.cancelled) throw new Error(transfer.cancelled);
    const end = Math.min(offset + state.slice, share.size) - 1;
    const slice = await readSlice(share, offset, end);
    if (slice.byteLength !== end - offset + 1) throw new Error("the file gave back a short read");

    for (let at = 0; at < slice.length; at += CHUNK) {
      if (transfer.cancelled) throw new Error(transfer.cancelled);
      const piece = slice.subarray(at, Math.min(at + CHUNK, slice.length));
      const copy = piece.slice();
      pending = pending.then(async () => {
        digests.push(new Uint8Array(await crypto.subtle.digest("SHA-256", copy)));
      });
      if (digests.length % HASH_BATCH === 0) await pending;   // keep the hash queue bounded
      if (channel.bufferedAmount > HIGH) await drain(channel, LOW, transfer);
      conn.send(copy.buffer);
      transfer.sent += piece.byteLength;
      sinceReport += piece.byteLength;
      if (sinceReport >= 4 << 20) {
        sinceReport = 0;
        showProgress(transfer);
        report({ kind: "progress", token: share.token, peer: transfer.peer, bytes: transfer.sent });
      }
    }
  }

  await drain(channel, 0, transfer);    // queued is not the same as delivered
  await pending;
  const all = new Uint8Array(digests.length * 32);
  digests.forEach((digest, index) => all.set(digest, index * 32));
  const root = hex(await crypto.subtle.digest("SHA-256", all));
  conn.send(JSON.stringify({ kind: "done", bytes: transfer.sent, root }));
  showProgress(transfer);
}

function handleConnection(conn) {
  const transfer = {
    id: Math.random().toString(36).slice(2), peer: conn.peer, token: null,
    sent: 0, started: performance.now(), cancelled: "", verdict: null,
  };
  let closed = false;

  const stop = (reason) => {
    if (!transfer.cancelled) transfer.cancelled = reason;
  };

  conn.on("close", () => {
    closed = true;
    stop("the recipient closed the page");
    state.transfers.delete(transfer.id);
    render();
  });
  conn.on("error", () => stop("the connection broke"));

  conn.on("data", async (data) => {
    if (typeof data !== "string") return;
    let message;
    try { message = JSON.parse(data); } catch { return; }

    if (message.kind === "hello") {
      let share;
      try {
        share = await api("/api/share?token=" + encodeURIComponent(message.token || ""));
      } catch (error) {
        report({ kind: "rejected", token: message.token || "", peer: transfer.peer });
        conn.send(JSON.stringify({ kind: "denied", reason: String(error.message || error) }));
        setTimeout(() => conn.close(), 200);
        return;
      }
      transfer.token = share.token;
      const answer = await report({ kind: "asked", token: share.token, peer: transfer.peer });
      if (answer && answer.ok === false) {
        conn.send(JSON.stringify({ kind: "denied", reason: answer.error || "this link is closed" }));
        setTimeout(() => conn.close(), 200);
        return;
      }
      // A reload means the same recipient asks again: drop the previous attempt rather than
      // writing into a channel nobody is reading.
      for (const [id, other] of state.transfers) {
        if (other.peer === transfer.peer && id !== transfer.id) other.cancelled = "the recipient reloaded";
      }
      state.transfers.set(transfer.id, transfer);
      render();
      const local = state.shares.get(share.token);
      local.downloads = share.downloads;
      try {
        await sendFile(conn, local, transfer);
      } catch (error) {
        state.transfers.delete(transfer.id);
        finishRow(transfer, String(error.message || error), false);
        render();
        if (!closed) conn.send(JSON.stringify({ kind: "aborted", reason: String(error.message || error) }));
        report({ kind: "failed", token: share.token, peer: transfer.peer, message: String(error.message || error) });
      }
      return;
    }

    if (message.kind === "report") {
      const seconds = (performance.now() - transfer.started) / 1000;
      state.transfers.delete(transfer.id);
      finishRow(transfer, message.ok ? `sent in ${seconds.toFixed(1)}s` : "the file arrived corrupted", !!message.ok);
      // Tell kit first, then refresh: otherwise the card still shows the count from before this send.
      await report({
        kind: "done", token: transfer.token, peer: transfer.peer,
        bytes: transfer.sent, seconds, verified: !!message.ok,
      });
      await refresh().catch(() => {});
      if (historyOpen()) loadHistory();
      render();
    }
  });
}

// --- start --------------------------------------------------------------------------

function applyState(data) {
  state.slice = data.slice || state.slice;
  state.dialog = !!data.dialog;
  for (const share of data.shares) {
    const existing = state.shares.get(share.token);
    state.shares.set(share.token, { ...(existing || {}), ...share });
  }
  const live = data.shares.filter((share) => !share.dead).length;
  $("head").textContent = live
    ? `${live} link${live === 1 ? "" : "s"} live · this window does the sending`
    : data.shares.length ? "every link is closed - add another file, or close this window"
    : "add a file to get a link";
  render();
  return data;
}

const refresh = async () => applyState(await api("/api/state"));

async function main() {
  const data = applyState(await post("/api/window-hello", { session: SESSION }));
  fillSelect($("def-expire"), EXPIRIES, data.defaults.expire || "2h");
  fillSelect($("def-max"), LIMITS, data.defaults.once ? "1" : String(data.defaults.max || 0));
  $("choose").disabled = !data.dialog;
  if (!data.dialog) {
    $("add-hint").textContent =
      "This system has no file dialog kit can open - use Browse, or drop files onto this window.";
  }
  $("peer").textContent = "id " + data.peer;

  const peer = new Peer(data.peer, { debug: 1, config: { iceServers: ICE } });
  state.peer = peer;
  peer.on("open", () => { $("head").textContent = $("head").textContent.replace("starting…", ""); });
  peer.on("error", (error) => {
    $("head").textContent = "matchmaker problem: " + error.type + " - try restarting kit send";
  });
  peer.on("connection", handleConnection);
  setInterval(() => { refresh().catch(() => {}); }, 5000);
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

$("stop").addEventListener("click", async () => {
  $("stop").disabled = true;
  for (const transfer of state.transfers.values()) transfer.cancelled = "you stopped sharing";
  await post("/api/stop", {}).catch(() => {});
  await refresh().catch(() => {});
});

main().catch((error) => { $("head").textContent = "couldn't start: " + error.message; });
