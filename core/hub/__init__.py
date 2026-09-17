"""kit hub: a local web dashboard for kit - browse and run tools, change settings, update kit."""

from __future__ import annotations

import argparse
import secrets
import signal
import webbrowser

from kitlib import style
from kitlib.settings import config_path, kit_settings


def _interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def main(args: list[str]) -> int:
    from core.hub.server import start_server

    settings = kit_settings()
    parser = argparse.ArgumentParser(
        prog="kit hub",
        description="Open the kit dashboard: browse and run tools, change settings and update kit.",
    )
    parser.add_argument("--port", type=int,
                        help=f"port to listen on (default: {settings['hub_port']} from kit.hub_port, or the next free one)")
    parser.add_argument("--no-open", action="store_true", help="don't open the dashboard in your browser")
    options = parser.parse_args(args)

    token = secrets.token_urlsafe(24)
    server = start_server(options.port or settings["hub_port"], options.port is not None, token)
    url = f"http://127.0.0.1:{server.server_address[1]}/?token={token}"
    print(f"{style('kit hub', 'bold', 'cyan')}  the kit dashboard")
    print(f"  open      {style(url, 'bold')}")
    print(f"  settings  {config_path()}")
    print(style("  only reachable from this computer - press Ctrl+C to stop", "dim"), flush=True)

    # Closing the console window or `kill` should shut down as cleanly as Ctrl+C.
    for name in ("SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            try:
                signal.signal(signum, _interrupt)
            except (ValueError, OSError):
                pass

    if settings["hub_open"] and not options.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping kit hub")
    finally:
        stopped = server.stop_all_jobs()
        server.server_close()
        if stopped:
            print(style(f"  stopped {stopped} running tool(s)", "dim"))
    return 0
