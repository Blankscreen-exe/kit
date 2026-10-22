// kit receive's upload page: the code, then files sent in pieces that can pick up where they stopped.
'use strict';

(() => {
  const main = document.querySelector('main');
  const NEEDS_CODE = main.dataset.code === '1';
  const MAX = Number(main.dataset.max) || 0;
  const LIMIT = Math.min(Number(main.dataset.limit) || 64 << 20, 48 << 20); // well under Cloudflare's 100 MB
  const MIN_CHUNK = 1 << 20;
  const PIECE_SECONDS = 15;     // aim for pieces that take about this long, whatever the connection
  const STORE = 'kit-receive:' + location.pathname;
  const $ = (id) => document.getElementById(id);

  let session = sessionStorage.getItem(STORE) || '';
  let chunk = Number(main.dataset.chunk) || 8 << 20;
  let items = [];               // {file, row, bar, state, id, received, inflight, done, bad}
  let sending = false;
  let codeWaiter = null;        // resolves once the code has been entered again mid-send
  let samples = [];             // [time, bytes so far] for the speed

  class HttpError extends Error {
    constructor(status, body) {
      super((body && body.error) || (status ? `HTTP ${status}` : 'no connection'));
      this.status = status;
      this.body = body || {};
    }
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function human(bytes) {
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
    return (i === 0 || bytes >= 100 ? bytes.toFixed(0) : bytes.toFixed(1)) + ' ' + units[i];
  }

  function humanTime(seconds) {
    seconds = Math.round(seconds);
    if (seconds >= 3600) return `${Math.floor(seconds / 3600)} h ${Math.floor(seconds % 3600 / 60)} min`;
    if (seconds >= 60) return `${Math.floor(seconds / 60)} min`;
    return `${seconds} s`;
  }

  function show(step) {
    $('code-step').hidden = step !== 'code';
    $('pick-step').hidden = step !== 'pick';
  }

  function fatal(message) {
    $('fatal').textContent = message;
    $('fatal').hidden = false;
    show('');
    sending = false;
    window.onbeforeunload = null;
  }

  // --- talking to kit ----------------------------------------------------------------

  async function api(path, body) {
    let response;
    try {
      response = await fetch(path, {
        method: 'POST', cache: 'no-store', credentials: 'omit',
        headers: { 'Content-Type': 'application/json', 'X-Kit-Session': session },
        body: JSON.stringify(body),
      });
    } catch (err) {
      throw new HttpError(0, {});
    }
    let data = {};
    try { data = await response.json(); } catch (err) { /* a proxy's own error page */ }
    if (!response.ok) throw new HttpError(response.status, data);
    return data;
  }

  function put(item, offset, blob) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();   // fetch can't report upload progress
      xhr.open('PUT', `chunk?id=${encodeURIComponent(item.id)}&offset=${offset}`);
      xhr.setRequestHeader('X-Kit-Session', session);
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.timeout = 10 * 60 * 1000;
      xhr.upload.onprogress = (event) => { item.inflight = event.loaded; render(); };
      xhr.onload = () => {
        item.inflight = 0;
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch (err) { /* not kit's answer */ }
        if (xhr.status === 200) resolve(data); else reject(new HttpError(xhr.status, data));
      };
      xhr.onerror = xhr.ontimeout = xhr.onabort = () => { item.inflight = 0; reject(new HttpError(0, {})); };
      xhr.send(blob);
    });
  }

  // The connection dropped, Cloudflare had a moment, or kit wants the code again: wait it out and say
  // so. False when it's something that won't get better by trying again.
  let failures = 0;
  async function recover(err) {
    if (err.status === 401) {
      session = '';
      sessionStorage.removeItem(STORE);
      $('code-error').textContent = 'Enter the code again to carry on.';
      $('code-error').hidden = false;
      show('code');
      await new Promise((resolve) => { codeWaiter = resolve; });
      return true;
    }
    const passing = err.status === 0 || err.status === 408 || err.status === 429 || err.status >= 500 && err.status !== 507;
    if (!passing) return false;
    failures++;
    const wait = Math.min(30, 2 ** Math.min(failures, 5));
    for (let left = wait; left > 0; left--) {
      $('overall').textContent = `Connection lost - trying again in ${left} s. Leave this page open.`;
      await sleep(1000);
    }
    $('overall').textContent = 'Trying again...';
    return true;
  }

  // --- the code ----------------------------------------------------------------------

  async function signIn(code) {
    const data = await api('code', { code });
    session = data.session;
    sessionStorage.setItem(STORE, session);
  }

  $('code-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = $('code-form').querySelector('button');
    button.disabled = true;
    try {
      await signIn($('code').value);
      $('code-error').hidden = true;
      $('code').value = '';
      show('pick');
      if (codeWaiter) { const resolve = codeWaiter; codeWaiter = null; resolve(); }
    } catch (err) {
      if (err.status === 410) return fatal(err.message);
      let message = err.status ? err.message : "Couldn't reach the computer - check your connection and try again.";
      if (typeof err.body.left === 'number') message += ` ${err.body.left} ${err.body.left === 1 ? 'try' : 'tries'} left.`;
      $('code-error').textContent = message;
      $('code-error').hidden = false;
      $('code').select();
    } finally {
      button.disabled = false;
    }
  });

  // --- choosing files ----------------------------------------------------------------

  function add(files, skipped = 0) {
    if (sending) return;
    for (const file of files) {
      if (items.some((item) => item.file.name === file.name && item.file.size === file.size
                               && item.file.lastModified === file.lastModified)) continue;
      const row = document.createElement('div');
      row.className = 'file';
      row.innerHTML = '<div class="top"><span class="fname"></span><span class="fsize"></span>'
        + '<span class="fstate"></span></div><div class="progress"><i></i></div>';
      row.querySelector('.fname').textContent = file.name;
      row.querySelector('.fsize').textContent = human(file.size);
      const item = { file, row, bar: row.querySelector('.progress > i'), state: row.querySelector('.fstate'),
                     id: '', received: 0, inflight: 0, done: false, bad: '' };
      if (MAX && file.size > MAX) item.bad = `too big - this link takes files up to ${human(MAX)}`;
      items.push(item);
      $('files').append(row);
    }
    $('drop-text').textContent = skipped
      ? `Folders can't be sent - zip ${skipped === 1 ? 'it' : 'them'} first. Drop files, or click to choose them`
      : 'Drop more files, or click to choose them';
    render();
  }

  $('picker').addEventListener('change', () => { add($('picker').files); $('picker').value = ''; });
  const drop = $('drop');
  drop.addEventListener('dragover', (event) => { event.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', (event) => {
    event.preventDefault();
    drop.classList.remove('over');
    const files = [];
    let folders = 0;
    for (const entry of event.dataTransfer.items || []) {
      const asEntry = entry.webkitGetAsEntry && entry.webkitGetAsEntry();
      if (asEntry && asEntry.isDirectory) { folders++; continue; }
      const file = entry.getAsFile();
      if (file) files.push(file);
    }
    add(files.length || folders ? files : event.dataTransfer.files, folders);
  });
  $('clear').addEventListener('click', () => {
    if (sending) return;
    items = [];
    $('files').replaceChildren();
    $('all-done').hidden = true;
    $('overall').textContent = '';
    $('drop-text').textContent = 'Drop files here, or click to choose them';
    render();
  });

  // --- progress ----------------------------------------------------------------------

  function render() {
    let done = 0;
    let total = 0;
    for (const item of items) {
      const have = Math.min(item.file.size, item.received + item.inflight);
      const percent = item.file.size ? 100 * have / item.file.size : (item.done ? 100 : 0);
      item.bar.style.width = `${percent}%`;
      item.row.classList.toggle('done', item.done);
      item.row.classList.toggle('bad', Boolean(item.bad));
      item.state.textContent = item.bad || (item.done ? '✓ sent' : have ? `${percent.toFixed(0)}%` : '');
      if (!item.bad) { done += have; total += item.file.size; }
    }
    const ready = items.some((item) => !item.done && !item.bad);
    $('send').disabled = sending || !ready;
    $('send').textContent = sending ? 'Sending...' : items.some((item) => item.received && !item.done) ? 'Carry on sending' : 'Send';
    $('clear').hidden = sending || !items.length;
    if (sending) {
      const now = performance.now();
      samples.push([now, done]);
      while (samples.length > 2 && now - samples[0][0] > 10000) samples.shift();
      const [then, before] = samples[0];
      const rate = now - then > 1000 ? (done - before) / ((now - then) / 1000) : 0;
      let line = `${human(done)} of ${human(total)}`;
      if (rate > 0) line += ` · ${human(rate)}/s · about ${humanTime((total - done) / rate)} left`;
      if (!failures) $('overall').textContent = line + '. Leave this page open until it\'s done.';
    }
  }

  function tune(bytes, ms) {
    const rate = bytes / Math.max(ms / 1000, 0.05);
    const target = Math.round(rate * PIECE_SECONDS / MIN_CHUNK) * MIN_CHUNK;
    chunk = Math.max(MIN_CHUNK, Math.min(LIMIT, target));
  }

  // --- sending -----------------------------------------------------------------------

  async function start(list) {
    for (;;) {
      try {
        const data = await api('start', {
          files: list.map((item) => ({ name: item.file.name, size: item.file.size, modified: item.file.lastModified })),
        });
        data.files.forEach((answer, i) => {
          list[i].id = answer.id;
          list[i].received = answer.received;
          list[i].done = answer.done;
        });
        failures = 0;
        return true;
      } catch (err) {
        if (await recover(err)) continue;
        $('overall').textContent = err.message;
        return false;
      }
    }
  }

  async function sendFile(item) {
    while (!item.done) {
      if (item.received >= item.file.size) {
        // all of it has gone, but the answer saying it was kept didn't come back: ask
        if (!(await start([item]))) { item.bad = $('overall').textContent; return; }
        if (!item.done) { item.bad = "it didn't arrive whole - send it again"; return; }
        break;
      }
      const size = Math.min(chunk, item.file.size - item.received);
      const began = performance.now();
      try {
        const data = await put(item, item.received, item.file.slice(item.received, item.received + size));
        item.received = data.received;
        item.done = data.done;
        failures = 0;
        tune(size, performance.now() - began);
      } catch (err) {
        if (err.status === 409 && typeof err.body.received === 'number') {
          item.received = err.body.received;      // kit has more (or less) of it than this page thought
          item.done = Boolean(err.body.done);
          if (err.body.busy) await sleep(3000);  // an earlier, broken piece is still being let go of
          continue;
        }
        if (!(await recover(err))) {
          item.bad = err.message;
          return;
        }
        // After a drop, part of that piece may have arrived: ask how much before going on.
        if (!(await start([item]))) { item.bad = $('overall').textContent; return; }
      }
      render();
    }
  }

  $('send').addEventListener('click', async () => {
    const list = items.filter((item) => !item.done && !item.bad);
    if (!list.length || sending) return;
    sending = true;
    samples = [];
    $('all-done').hidden = true;
    window.onbeforeunload = (event) => { event.preventDefault(); event.returnValue = ''; };
    render();
    if (await start(list)) {
      for (const item of list) {
        render();
        await sendFile(item);
        render();
      }
    } else {
      for (const item of list) item.bad = item.bad || $('overall').textContent;
    }
    sending = false;
    window.onbeforeunload = null;
    render();
    const failed = items.filter((item) => item.bad && !item.done).length;
    const sent = items.filter((item) => item.done).length;
    $('overall').textContent = failed ? `${sent} sent, ${failed} not sent.` : `${sent} file${sent === 1 ? '' : 's'} sent.`;
    $('all-done').hidden = failed > 0 || !sent;
  });

  // --- off we go ---------------------------------------------------------------------

  if (session) {
    show('pick');
  } else if (NEEDS_CODE) {
    show('code');
    $('code').focus();
  } else {
    signIn('').then(() => show('pick'), (err) => fatal(err.status === 410 ? err.message
      : "Couldn't reach the computer - check your connection and reload this page."));
  }
  render();
})();
