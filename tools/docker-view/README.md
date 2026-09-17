# docker-view

A local web dashboard for Docker containers, images, volumes and networks, with action buttons and the matching docker commands.

## Usage

```
kit docker-view [--port N] [--no-open]
```

- Starts a small web server on `127.0.0.1` (port 9900, or the next free one) and opens it in your browser.
- Containers are grouped by Docker Compose project and show status, health, image, ports, uptime and live CPU/memory.
- Every TCP port a running container publishes to your computer is a link (e.g. `localhost:3000 ↗`) that opens
  in a new tab. Ports of services that aren't web pages (Postgres, Redis, MySQL, ...) are still links but shown
  muted, and ports of stopped containers are plain text.
- Each container has Start/Stop, Restart, Pause/Unpause, Logs and Remove buttons. Stop and Remove ask first.
- Every action reports the exact `docker ...` command it ran. Each card also lists copyable commands
  (`logs -f`, `exec -it <name> sh`, `inspect`, ...) so you can do the same from a terminal.
- Volumes are cards grouped by Compose project, showing which containers use each one (in use / attached / unused)
  and copyable inspect, list-files and remove commands. Anonymous volumes are shortened and listed last.
- Images and networks are listed in tables with copyable commands. The page only ever changes containers,
  never images, volumes or networks.
- The open tab is kept in the address (e.g. `#volumes`), so refreshing or bookmarking returns to it.
- Refreshes every 5 seconds (untick Auto-refresh to pause). The filter box searches names, images and projects.
- Press Ctrl+C in the terminal to stop it.

Security: the server only listens on this computer, and the page needs the one-time token in the address printed
in the terminal, so other web pages or local users can't use it to control Docker. Remove never forces:
stop a container before removing it.

On Linux your user needs access to the Docker socket (be in the `docker` group), or run kit with sudo.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `docker-view.port` | `9900` | Port to try first; the next free one is used if it's taken. `--port` asks for exactly one port. |
| `docker-view.open` | `true` | Open the dashboard in your browser when it starts (`--open` / `--no-open`) |

```
kit config set docker-view.open false
```

## Examples

```
kit docker-view            # start and open the dashboard
kit dv --port 9000         # pick the port
kit dv --no-open           # only print the address
```
