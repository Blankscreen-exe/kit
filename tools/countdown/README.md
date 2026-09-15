# countdown

A big countdown timer in the terminal that you can click or drive with keys, with an alert when time is up.

## Usage

```
kit countdown <duration> [message...] [--once] [--no-notify] [--plain]
kit countdown --until <time> [message...]
```

- Durations: `90s`, `15m`, `1h30m`, `2 hours`, `25:00` (minutes:seconds), `1:30:00` (hours:minutes:seconds),
  or a bare number of minutes like `25`.
- `--until` counts down to the next time the clock shows that time: `14:30`, `2:30pm`, `9am`.
- The screen shows the time left in big digits, your message, a progress bar and when it ends. Click the buttons
  or use the keys: `space` pause/resume, `-` / `+` take away or add a minute, `r` reset, `q` quit.
- When time is up the screen flashes, the terminal beeps and you get a desktop notification (Windows toast,
  `notify-send` on Linux, Notification Center on macOS). The beep repeats every few seconds until you press
  `space`, `enter` or `esc`, or click Dismiss. `+` snoozes for a minute. `--once` alerts only once;
  `--no-notify` skips the desktop notification and system sound.
- `--plain` draws a single updating line instead of the full screen, which suits SSH sessions and scripts.
  It exits with code 0 when time is up and 130 if you cancel with Ctrl+C.

## Examples

```
kit countdown 15m check the deploy
kit timer 25:00 write docs
kit countdown --until 14:30 standup
kit countdown 90s --plain && echo "go"
```
