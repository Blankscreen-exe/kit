# port

See what's using network ports, stop it, and find free ports.

## Usage

```
kit port [--udp]                          # everything listening
kit port --live [--udp]                   # the same, live and auto-refreshing
kit port <number>                         # what's on that port
kit port <number> --kill [--force] [-y]   # stop whatever is listening on it
kit port free [--from N]                  # next unused port (default: from 8000)
```

- The list shows protocol, port, address, PID, process name, whether it's currently shared via
  `kit share` (and under what name), and command line.
  An address of `0.0.0.0` or `::` (highlighted) means other machines can reach it;
  `127.0.0.1` / `::1` means only this machine can.
- `kit port <number>` also shows connections to that port, e.g. your app talking to a database.
- `--kill` only targets the process that is **listening** on the port, never its clients. It asks first,
  and warns if the process looks like Docker, WSL, a database or part of the OS.
  Without `--force` the process is asked to exit (on Windows that's the same as a kill).
- `free` prints just the number, so it's handy in scripts.
- On Linux, processes owned by other users (e.g. system services) are hidden unless you use `sudo`.

### Live view

`kit port --live` is the same listing, refreshed on its own (`port.refresh` seconds, default 2) in a
full-screen terminal view: **u** toggles UDP, **k** stops the selected process (asks first), **r** refreshes
now, **q** quits. It never listens on the network itself - this is your own terminal, not a page, precisely
because a "here's everything my machine has open" view is the kind of thing you want to check yourself, not
broadcast to other devices.

## Examples

```
kit port                     # what's listening
kit port --live              # the same, live
kit port 3000                # who has port 3000?
kit port 3000 --kill         # free it up
kit ports --udp              # alias, with UDP too
kit port free --from 5000    # e.g. 5001
```
