// The sending half of kit send. This page holds the peer connection and streams the file out of kit's
// local server, because browsers do WebRTC far better than anything we could add to Python.
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

const $ = (id) => document.getElementById(id);
const state = { peer: null, slice: 8 << 20, shares: new Map(), transfers: new Map() };
window.state = state;   // handy in devtools, and what the test harness watches

const humanBytes = (n) => {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return (i === 0 || n >= 100 ? n.toFixed(0) : n.toFixed(1)) + " " + units[i];
};

const hex = (buffer) => [...new Uint8Array(buffer)].map((b) => b.toString(16).padStart(2, "0")).join("");

async function api(path, options) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || response.statusText);
  return response.json();
}

const report = (body) => api("/api/event", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
}).catch(() => ({ ok: true }));   // kit going away must not break a transfer in flight

// --- page ---------------------------------------------------------------------------

function render() {
  const files = $("files");
  for (const share of state.shares.values()) {
    let card = document.getElementById("s-" + share.token);
    if (!card) {
      card = document.createElement("div");
      card.className = "card";
      card.id = "s-" + share.token;
      card.innerHTML = `
        <div class="row">
          <span class="name"></span><span class="size"></span>
          <span class="spacer"></span>
          <span class="status waiting"><span class="dot"></span><span class="label"></span></span>
        </div>
        <input class="link" readonly>
        <div class="row" style="margin-top:8px">
          <button class="copy">Copy link</button><span class="copied" hidden>copied</span>
        </div>
        <div class="transfers"></div>`;
      card.querySelector(".name").textContent = share.name;
      card.querySelector(".size").textContent = humanBytes(share.size);
      const input = card.querySelector(".link");
      input.value = share.link;
      card.querySelector(".copy").addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(share.link);
        } catch {
          input.select();
          document.execCommand("copy");
        }
        const flag = card.querySelector(".copied");
        flag.hidden = false;
        setTimeout(() => { flag.hidden = true; }, 1500);
      });
      files.appendChild(card);
    }
    const status = card.querySelector(".status");
    const label = card.querySelector(".label");
    const live = [...state.transfers.values()].filter((t) => t.token === share.token);
    status.className = "status " + (share.dead ? "dead" : live.length ? "live" : share.downloads ? "done" : "waiting");
    label.textContent = share.dead
      ? share.dead
      : live.length ? `sending to ${live.length}`
      : share.downloads ? `sent ${share.downloads}×` : "waiting for the link to be opened";
    card.querySelector(".copy").disabled = !!share.dead;
  }
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
  const url = "/api/bytes?token=" + encodeURIComponent(share.token);

  for (let offset = 0; offset < share.size; offset += state.slice) {
    if (transfer.cancelled) throw new Error(transfer.cancelled);
    const end = Math.min(offset + state.slice, share.size) - 1;
    const response = await fetch(url, { credentials: "same-origin", headers: { Range: `bytes=${offset}-${end}` } });
    if (!response.ok) throw new Error("kit couldn't read the file (" + response.status + ")");
    const slice = new Uint8Array(await response.arrayBuffer());
    if (slice.byteLength !== end - offset + 1) throw new Error("kit returned a short read");

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
      await refresh();
      report({
        kind: "done", token: transfer.token, peer: transfer.peer,
        bytes: transfer.sent, seconds, verified: !!message.ok,
      });
      render();
    }
  });
}

// --- start --------------------------------------------------------------------------

async function refresh() {
  const data = await api("/api/state");
  state.slice = data.slice || state.slice;
  for (const share of data.shares) {
    const existing = state.shares.get(share.token);
    state.shares.set(share.token, { ...(existing || {}), ...share });
  }
  const live = data.shares.filter((share) => !share.dead).length;
  $("head").textContent = live
    ? `${live} link${live === 1 ? "" : "s"} live · this window does the sending`
    : "every link is closed - you can close this window";
  render();
  return data;
}

async function main() {
  const data = await refresh();
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

$("stop").addEventListener("click", async () => {
  $("stop").disabled = true;
  for (const transfer of state.transfers.values()) transfer.cancelled = "you stopped sharing";
  await api("/api/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }).catch(() => {});
  await refresh().catch(() => {});
});

main().catch((error) => { $("head").textContent = "couldn't start: " + error.message; });
