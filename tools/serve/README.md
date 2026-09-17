# serve

Share a folder, or an app running on this computer, with other devices on your network, with a QR code for your phone.

## Usage

```
kit serve [folder] [-p PORT] [--host HOST] [--no-qr] [--window]
kit serve --app PORT [-p PORT] [--host HOST] [--no-qr] [--window]
```

- `kit serve [folder]` shares a folder (default: the current one) over HTTP, with directory listings.
- `kit serve --app 3000` shares an app that's already running on `localhost:3000`, such as a dev server. It forwards
  raw TCP, so websockets and hot reload keep working. Both IPv4 (`127.0.0.1`) and IPv6 (`::1`) localhost apps are found.
- Listens on port 8000, or the next free port after it; `-p` picks a port.
- Listens on every network interface (`0.0.0.0`) so phones and other computers on the same Wi-Fi can connect.
  `--host 127.0.0.1` keeps it private to this computer.
- Prints the local URL, the network URL(s) and a QR code of the main network URL; `--no-qr` hides the code.
- `--window` also opens the page in its own app window (Edge, Chrome or Chromium `--app` mode: no tabs, no address bar).
  Without one of those browsers it opens a normal browser tab.
- Ctrl+C stops it.

### Firewall

The first time you share on your network, Windows Firewall asks whether Python may accept connections. Allow it on
**Private networks**. If you dismissed the prompt, allow Python under *Windows Security > Firewall & network protection >
Allow an app through firewall*. On Linux with ufw: `sudo ufw allow 8000/tcp`.

### Dev servers that reject other hosts

Some dev servers only answer requests addressed to `localhost` and reject others with an "invalid host" or
"blocked request" error. Allow other hosts in the app's own settings, for example Vite: `server.allowedHosts: true`,
webpack-dev-server: `allowedHosts: 'all'`, Django: add the network IP to `ALLOWED_HOSTS`.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `serve.port` | `8000` | Port to try first; the next free one is used if it's taken. `-p` asks for exactly one port. |
| `serve.host` | `0.0.0.0` | Address to listen on. `127.0.0.1` keeps everything private to this computer. |
| `serve.window` | `false` | Also open the page in an app window (`--window` / `--no-window`) |

```
kit config set serve.host 127.0.0.1
```

## Examples

```
kit serve                          # share the current folder
kit serve D:\photos -p 9000        # share a folder on port 9000
kit serve --app 3000               # put a local dev server on your Wi-Fi
kit serve --app 5173 --window      # ...and open it in an app window
kit serve . --host 127.0.0.1       # this computer only
```
