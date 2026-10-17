# subs

Track your subscriptions: what each costs, what they add up to a month and a year, and what renews next.

## Usage

```
kit subs                                  # open the app
kit subs list [--all] [--sort next|cost|name] [--category C] [--json]
kit subs add NAME PRICE [--every CYCLE] [--next DATE] [--currency CUR] [--category C] [--trial-end DATE] [--notes TEXT]
kit subs edit NAME [--rename NEW] [--price P] [--every ...] [--next ...] [--currency ...] [--category ...] [--trial-end DATE|none] [--notes ...]
kit subs show NAME
kit subs pause|resume|cancel NAME
kit subs rm NAME [--yes]
kit subs due [DAYS]
kit subs total
kit subs rate [CUR [VALUE] | CUR --unset]
kit subs --where
```

- **The app** lists every subscription with its price, cycle, cost per month in your main currency, next
  renewal and how far off it is: yellow within `due_days`, red today or tomorrow. Below it, what's due soon
  and the monthly cost of each category. Keys: `a` add, `e` / `Enter` edit, `p` pause/resume,
  `c` cancel/restore, `d` delete, `s` sort (next renewal, cost, name), `h` show cancelled, `r` exchange
  rates, `q` quit.
- **Names** on the command line can be any unique part of a name, in any case (`kit subs show net`).
- **Cycles**: `monthly`, `yearly`, `weekly`, `quarterly`, `fortnightly`, or a count: `3m`, `2w`, `90d`, `2y`.
- **Dates**: `YYYY-MM-DD`, `today`, `tomorrow`, or `+10d`, `+2w`, `+1m`, `+1y` from today.
- **Renewals move on by themselves.** kit keeps one billing date and the cycle, and works out the next
  renewal from them, so you never have to update it. A bill on the 31st renews on the last day of shorter
  months. With no `--next`, a new subscription renews one cycle from today (or on its trial's end).
- **Status**: paused and cancelled subscriptions are left out of totals and `due`. Cancelled ones stay on
  record but are hidden unless you ask for `--all` (or press `h`). `rm` deletes for good.
- **Trials**: `--trial-end` marks when a free trial turns paid; it shows up in `due` and in the app.
- **Currencies**: each subscription has its own currency (`9.99 EUR` or `--currency EUR`). Totals are in
  the main currency (`subs.currency`), converted with rates you set once: `kit subs rate EUR 1.08` means
  1 EUR = 1.08 of your main currency. Nothing is looked up online. Amounts without a rate are shown
  separately rather than guessed.
- Data lives in `%APPDATA%\kit\subs.json` (Windows) or `~/.local/share/kit/subs.json` (Linux;
  `$XDG_DATA_HOME` is respected). Set `KIT_SUBS_DATA` to use another file.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `subs.currency` | `USD` | Main currency: totals are shown in it, and new subscriptions use it by default |
| `subs.due_days` | `7` | How many days ahead counts as due soon (1-365) |

```
kit config set subs.currency EUR
```

## Examples

```
kit subs add Netflix 15.49 --category streaming --next 2026-11-03
kit subs add "Adobe CC" 263.88 --every yearly --category software
kit subs add Spotify "10.99 EUR" --category music --trial-end +30d
kit subs rate EUR 1.08
kit subs due 30
kit subs total
kit subs edit netflix --price 17.99
kit subs cancel spotify
```
