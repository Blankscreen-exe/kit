// Turns chunks pushed from the page into an ordinary browser download, so a big file never has to sit
// in memory as one Blob. This is what browsers without showSaveFilePicker (Firefox, Safari) use.
const streams = new Map();   // token -> { stream, name, size }

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.kind !== "register") return;
  const port = event.ports[0];
  const stream = new ReadableStream({
    start(controller) {
      port.onmessage = (message) => {
        const payload = message.data;
        if (payload === "end") { controller.close(); port.close(); return; }
        if (payload && payload.abort) { controller.error(new Error("aborted")); port.close(); return; }
        controller.enqueue(new Uint8Array(payload));
      };
    },
  });
  streams.set(data.token, { stream, name: data.name, size: data.size });
  port.postMessage({ ready: true });
});

self.addEventListener("fetch", (event) => {
  // The page asks for <this folder>/dl/<token>; anything else is none of our business.
  const match = new URL(event.request.url).pathname.match(/\/dl\/([a-z0-9]+)$/i);
  if (!match) return;
  const entry = streams.get(match[1]);
  if (!entry) return;
  streams.delete(match[1]);
  const name = String(entry.name || "download").replace(/["\\\r\n]/g, "_");
  const headers = {
    "Content-Type": "application/octet-stream",
    "Content-Disposition": `attachment; filename="${name}"`,
  };
  if (entry.size) headers["Content-Length"] = String(entry.size);
  event.respondWith(new Response(entry.stream, { headers }));
});
