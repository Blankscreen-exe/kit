# serve

Share a folder, or an app running on this computer, with other devices on your network or with anyone over the internet, with a QR code for your phone.

## Usage

```
kit serve [folder] [-p PORT] [--host HOST | --internet] [--no-qr] [--ui | --no-ui]
kit serve --app PORT [-p PORT] [--host HOST | --internet] [--no-qr] [--ui | --no-ui]
```

- `kit serve [folder]` shares a folder (default: the current one) over HTTP, with directory listings.
- `kit serve --app 3000` shares an app that's already running on `localhost:3000`, such as a dev server. It forwards
  raw TCP, so websockets and hot reload keep working. Both IPv4 (`127.0.0.1`) and IPv6 (`::1`) localhost apps are found.
- Listens on port 8000, or the next free port after it; `-p` picks a port.
- Listens on every network interface (`0.0.0.0`) so phones and other computers on the same Wi-Fi can connect.
  `--host 127.0.0.1` keeps it private to this computer.
- `--internet` shares with anyone, anywhere, instead of your network - see below.
- Prints the local URL, the network URL(s) and a QR code of the main network URL; `--no-qr` hides the code.
- `--ui` also opens the page in its own app window (Edge, Chrome or Chromium `--app` mode: no tabs, no address bar).
  Without one of those browsers it opens a normal browser tab. `serve.window` turns that on every time; `--no-ui`
  turns it off for one run. The older `--window` / `--no-window` mean the same.
- In an SSH session, or on Linux with no desktop, serve never opens a window by itself - even with `serve.window`
  on, it says it's skipping it. Everything else works the same, so a server can share a folder too.
- Ctrl+C stops it.

### Anyone, anywhere: `--internet`

`kit serve --app 8000 --internet` gives a link like `https://<words>.trycloudflare.com/` that works for someone
who isn't on your network - to demo an app running on your machine, say. kit runs Cloudflare's `cloudflared`, which
dials out and gets a random public https address, so it works behind any router or firewall that allows ordinary
outgoing traffic, with no account and no open port. cloudflared is rarely installed, so the first time kit downloads
the official release for this machine (about 55 MB), checks it against the SHA-256 GitHub publishes, and keeps it in
kit's data folder (`kit send --internet` uses the same one).

- The address takes about 15 seconds to set up, and kit checks it works before showing it. It's a new one every run.
- **Anyone who has the link can open it** until you press Ctrl+C - there's no password. Everything passes through
  Cloudflare on the way (over https).
- Nothing is shared on your network meanwhile: kit serve listens on this computer only, so `--host` doesn't go with it.
- Websockets work, so hot reload does too. Server-Sent Events don't get through Cloudflare's free tunnels.
- The log names visitors by their public address, as Cloudflare reports it.

### Firewall

The first time you share on your network, Windows Firewall asks whether Python may accept connections. Allow it on
**Private networks**. If you dismissed the prompt, allow Python under *Windows Security > Firewall & network protection >
Allow an app through firewall*. On Linux with ufw: `sudo ufw allow 8000/tcp`. `--internet` needs none of this.

### Dev servers that reject other hosts

Some dev servers only answer requests addressed to `localhost` and reject others with an "invalid host" or
"blocked request" error (Django with `DEBUG` off says only "Bad Request (400)"). Allow other hosts in the app's own settings, for example Vite: `server.allowedHosts: true`,
webpack-dev-server: `allowedHosts: 'all'`, Django: add the network IP to `ALLOWED_HOSTS`. With `--internet` the host
is a different `<words>.trycloudflare.com` every run, so allow them all: `.trycloudflare.com` in Vite's
`server.allowedHosts` or Django's `ALLOWED_HOSTS`.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `serve.port` | `8000` | Port to try first; the next free one is used if it's taken. `-p` asks for exactly one port. |
| `serve.host` | `0.0.0.0` | Address to listen on. `127.0.0.1` keeps everything private to this computer. |
| `serve.window` | `false` | Also open the page in an app window, where there's a display (`--ui` / `--no-ui`) |
| `serve.cloudflared` | *(PATH, else downloaded)* | The cloudflared program `--internet` uses (`KIT_CLOUDFLARED`) |

```
kit config set serve.host 127.0.0.1
```

## Examples

```
kit serve                          # share the current folder
kit serve D:\photos -p 9000        # share a folder on port 9000
kit serve --app 3000               # put a local dev server on your Wi-Fi
kit serve --app 5173 --ui          # ...and open it in an app window
kit serve --app 8000 --internet    # demo a local app to someone outside your network
kit serve D:\photos --internet     # a folder, for anyone who has the link
kit serve . --host 127.0.0.1       # this computer only
```
