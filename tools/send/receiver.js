// The receiving half of kit send. Everything secret arrives in the link's # fragment, which browsers
// never send to a web server - so this page can be published anywhere without the host learning
// anything about the transfer.
//
// Saving, best first:
//   1. showSaveFilePicker  - straight to disk, any size (Chrome, Edge, Opera on desktop)
//   2. a service worker    - a normal streamed download (Firefox, Safari 16+, and the rest)
//   3. memory              - only offered for small files; big ones would kill the tab
const STALL_MS = 30000;
const HASH_BATCH = 64;
const MEMORY_LIMIT = 300 << 20;
const ICE = [{ urls: "stun:stun.l.google.com:19302" }, { urls: "stun:stun.cloudflare.com:3478" }];

const $ = (id) => document.getElementById(id);
const humanBytes = (n) => {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return (i === 0 || n >= 100 ? n.toFixed(0) : n.toFixed(1)) + " " + units[i];
};
const hex = (buffer) => [...new Uint8Array(buffer)].map((b) => b.toString(16).padStart(2, "0")).join("");

const fragment = new URLSearchParams(location.hash.slice(1));
const LINK = {
  peer: fragment.get("i") || "",
  token: fragment.get("t") || "",
  name: fragment.get("n") ? decodeURIComponent(fragment.get("n")) : "file",
  size: Number(fragment.get("s") || 0),
};

const note = (text, kind = "") => { $("note").textContent = text; $("note").className = "note " + kind; };

// --- where the bytes go --------------------------------------------------------------

// Browsers only expose crypto.subtle on https (and on localhost). A link served straight from the
// sender's machine - kit send --lan - is plain http, so the file can still arrive and be saved, it
// just can't be checked afterwards. Worth saying, not worth refusing.
const CAN_VERIFY = !!(window.crypto && window.crypto.subtle);

function saveMethod() {
  if (typeof window.showSaveFilePicker === "function") return "picker";
  // navigator.serviceWorker exists but is undefined in some private windows, so test the object itself
  if (navigator.serviceWorker && window.isSecureContext) return "worker";
  return "memory";
}

function describe(method) {
  if (method === "picker") return "Saves straight to disk - any size is fine.";
  if (method === "worker") return "Downloads as a stream - any size is fine.";
  if (LINK.size > MEMORY_LIMIT) {
    return CAN_VERIFY
      ? `This browser can only hold the file in memory, and ${humanBytes(LINK.size)} is too big for that. Try Chrome or Edge.`
      : `This link isn't https, so the file has to be held in memory, and ${humanBytes(LINK.size)} is too big for that.`;
  }
  return CAN_VERIFY
    ? "This browser holds the file in memory while it arrives, which is fine at this size."
    : "Held in memory while it arrives, which is fine at this size. Not https, so the file can't be checked afterwards.";
}

async function pickerSink() {
  const handle = await window.showSaveFilePicker({ suggestedName: LINK.name });
  const writable = await handle.createWritable();
  return {
    write: (buffer) => writable.write(buffer),
    close: () => writable.close(),
    abort: () => writable.abort().catch(() => {}),
  };
}

// A service worker turns pushed chunks into an ordinary download, so nothing is buffered whole.
async function workerSink() {
  const registration = await navigator.serviceWorker.register("./sw.js", { scope: "./" });
  await navigator.serviceWorker.ready;
  const worker = registration.active || navigator.serviceWorker.controller;
  if (!worker) throw new Error("the download helper didn't start");
  const token = Math.random().toString(36).slice(2);
  const channel = new MessageChannel();
  const ready = new Promise((resolve) => { channel.port1.onmessage = () => resolve(); });
  worker.postMessage({ kind: "register", token, name: LINK.name, size: LINK.size }, [channel.port2]);
  await ready;

  const frame = document.createElement("iframe");
  frame.hidden = true;
  frame.src = new URL("dl/" + token, location.href).href;
  document.body.appendChild(frame);
  return {
    write: (buffer) => channel.port1.postMessage(buffer, [buffer]),
    close: () => channel.port1.postMessage("end"),
    abort: () => channel.port1.postMessage({ abort: true }),
  };
}

function memorySink() {
  const parts = [];
  return {
    write: (buffer) => { parts.push(buffer); },
    close: () => {
      const url = URL.createObjectURL(new Blob(parts, { type: "application/octet-stream" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = LINK.name;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    },
    abort: () => { parts.length = 0; },
  };
}

// --- the transfer --------------------------------------------------------------------

async function receive() {
  const method = saveMethod();
  if (method === "memory" && LINK.size > MEMORY_LIMIT) {
    note(describe(method), "bad");
    return;
  }
  $("go").disabled = true;

  let sink = null;
  if (method === "picker") {
    try {
      sink = await pickerSink();       // asked first: it needs the click that got us here
    } catch (error) {
      $("go").disabled = false;
      note(error.name === "AbortError" ? "Cancelled - press Download when you're ready." : String(error.message || error),
           error.name === "AbortError" ? "" : "bad");
      return;
    }
  }

  note("connecting…");
  $("bar").style.display = "block";

  const peer = new Peer({ debug: 1, config: { iceServers: ICE } });
  let done = false;
  const fail = (text) => {
    if (done) return;
    done = true;
    note(text, "bad");
    $("go").disabled = false;
    if (sink) sink.abort();
    try { peer.destroy(); } catch {}
  };

  peer.on("error", (error) => fail(
    error.type === "peer-unavailable"
      ? "The sender's window isn't open any more - ask them to share it again."
      : "Connection problem: " + error.type));

  peer.on("open", () => {
    const conn = peer.connect(LINK.peer, { reliable: true, serialization: "raw" });
    const queued = [];                       // chunks that beat the sink into existence
    let received = 0, chunks = 0, started = 0, lastData = performance.now();
    const digests = [];
    let pending = Promise.resolve();

    const watchdog = setInterval(() => {
      if (done) return clearInterval(watchdog);
      if (started && performance.now() - lastData > STALL_MS) {
        clearInterval(watchdog);
        fail("The transfer stopped - the sender's window was probably closed.");
      }
    }, 2000);

    conn.on("open", () => conn.send(JSON.stringify({ kind: "hello", token: LINK.token })));
    conn.on("error", () => fail("The connection broke."));
    conn.on("close", () => { if (!done) fail("The sender closed the connection."); });

    conn.on("data", async (data) => {
      lastData = performance.now();

      if (typeof data === "string") {
        const message = JSON.parse(data);
        if (message.kind === "denied") return fail(message.reason || "The sender refused this link.");
        if (message.kind === "aborted") return fail(message.reason || "The sender stopped the transfer.");
        if (message.kind === "header") {
          started = performance.now();
          $("name").textContent = message.name || LINK.name;
          $("size").textContent = humanBytes(message.size);
          note("receiving…");
          if (!sink) {
            try {
              sink = method === "worker" ? await workerSink() : memorySink();
            } catch (error) {
              // The download helper can fail late (private windows, blocked workers). Memory still
              // works for a file this browser can hold; anything bigger has to stop here.
              if (method === "worker" && (message.size || 0) <= MEMORY_LIMIT) {
                sink = memorySink();
                $("how").textContent = "Saving from memory - the streaming helper wasn't available.";
              } else {
                return fail("Couldn't start saving: " + (error.message || error));
              }
            }
            for (const chunk of queued) sink.write(chunk);   // the spike lost 128 KB right here
            queued.length = 0;
          }
          return;
        }
        if (message.kind === "done") {
          clearInterval(watchdog);
          await pending;
          let root = "";
          if (CAN_VERIFY) {
            const all = new Uint8Array(digests.length * 32);
            digests.forEach((digest, index) => all.set(digest, index * 32));
            root = hex(await crypto.subtle.digest("SHA-256", all));
          }
          const ok = received === message.bytes && (!CAN_VERIFY || root === message.root);
          done = true;
          if (ok) {
            await sink.close();
            const seconds = (performance.now() - started) / 1000;
            note(`Done - ${humanBytes(received)} in ${seconds.toFixed(1)}s, `
                 + (CAN_VERIFY ? "checked and intact." : "though an https link would also let it be checked."), "good");
            $("bar").querySelector("i").style.width = "100%";
          } else {
            sink.abort();
            note(CAN_VERIFY
              ? "The file arrived damaged, so it wasn't saved. Ask the sender to try again."
              : "The file arrived incomplete, so it wasn't saved. Ask the sender to try again.", "bad");
            $("go").disabled = false;
          }
          conn.send(JSON.stringify({ kind: "report", ok, bytes: received }));
          setTimeout(() => { try { peer.destroy(); } catch {} }, 500);
        }
        return;
      }

      const buffer = data instanceof ArrayBuffer ? data : data.buffer;
      received += buffer.byteLength;
      chunks += 1;
      if (CAN_VERIFY) {
        const copy = buffer.slice(0);
        pending = pending.then(async () => {
          digests.push(new Uint8Array(await crypto.subtle.digest("SHA-256", copy)));
        });
        if (chunks % HASH_BATCH === 0) await pending;
      }
      if (sink) sink.write(buffer.slice(0));
      else queued.push(buffer.slice(0));

      if (chunks % 16 === 0) {
        const size = LINK.size || received;
        $("bar").querySelector("i").style.width = ((received / size) * 100).toFixed(1) + "%";
        const seconds = (performance.now() - started) / 1000;
        note(`receiving ${humanBytes(received)} of ${humanBytes(size)} · ${humanBytes(received / Math.max(seconds, 0.1))}/s`);
      }
    });
  });
}

// --- start ---------------------------------------------------------------------------

$("name").textContent = LINK.name;
$("size").textContent = LINK.size ? humanBytes(LINK.size) : "";
if (!LINK.peer || !LINK.token) {
  note("This link is incomplete - copy the whole thing, including everything after the #.", "bad");
  $("go").disabled = true;
} else {
  $("how").textContent = describe(saveMethod());
  $("go").addEventListener("click", () => receive().catch((error) => note(String(error.message || error), "bad")));
}
