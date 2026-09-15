# pomodoro

A pomodoro focus timer in the terminal that you can drive with the mouse or keyboard, and that keeps a history of your sessions.

## Usage

```
kit pomodoro [--focus MIN] [--short MIN] [--long MIN] [--every N] [--task TEXT]
kit pomodoro --stats
```

- Big countdown, progress bar and session dots (a long break comes after every N focus sessions).
- Click the buttons (Start/Pause, Reset, Skip, −1m, +1m, ⚙ settings) or use the keys shown at the bottom:
  `space` start/pause, `r` reset, `s` skip, `-` / `+` change the time, `t` edit the task, `,` settings, `q` quit.
- Type what you're working on in the task box; `Enter` or `Esc` returns the keys to the timer.
- When a phase ends you get an in-app notification, the terminal bell, and a desktop nudge where available
  (Windows: a system beep; Linux: `notify-send` if installed; macOS: a notification).
- Settings changed in the ⚙ dialog are remembered. Flags like `--focus 50` override them for that run.
- Completed focus sessions are saved to `%APPDATA%\kit\pomodoro.json` (Windows) or
  `~/.local/share/kit/pomodoro.json` (Linux; `$XDG_DATA_HOME` is respected). Set `KIT_POMODORO_DATA` to use another file.
- `--stats` prints today, this week, all time, and your last five sessions.

## Examples

```
kit pomodoro                                  # 25 / 5 / 15, long break every 4
kit pomo --focus 50 --short 10 --task "write report"
kit pomodoro --stats
```
