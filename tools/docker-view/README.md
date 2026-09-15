# docker-view

A local web dashboard for Docker containers, images, volumes and networks, with action buttons and the matching docker commands.

## Usage

```
kit docker-view [--port N] [--no-open]
```

- Starts a small web server on `127.0.0.1` (port 9900, or the next free one) and opens it in your browser.
- Containers are grouped by Docker Compose project and show status, health, image, ports, uptime and live CPU/memory.
- Each container has Start/Stop, Restart, Pause/Unpause, Logs and Remove buttons. Stop and Remove ask first.
- Every action reports the exact `docker ...` command it ran. Each card also lists copyable commands
  (`logs -f`, `exec -it <name> sh`, `inspect`, ...) so you can do the same from a terminal.
- Images, volumes and networks are listed with copyable commands; the page only changes containers.
- Refreshes every 5 seconds (untick Auto-refresh to pause). The filter box searches names, images and projects.
- Press Ctrl+C in the terminal to stop it.

Security: the server only listens on this computer, and the page needs the one-time token in the address printed
in the terminal, so other web pages or local users can't use it to control Docker. Remove never forces:
stop a container before removing it.

On Linux your user needs access to the Docker socket (be in the `docker` group), or run kit with sudo.

## Examples

```
kit docker-view            # start and open the dashboard
kit dv --port 9000         # pick the port
kit dv --no-open           # only print the address
```
