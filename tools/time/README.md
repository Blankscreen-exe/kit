# time

Time arithmetic for tracking hours: span between clock times, duration differences and totals.

## Usage

```
kit time between <start> <end> [-s]
kit time diff <duration> <duration> [-s]
kit time sum <duration> [<duration> ...] [-s]
```

- Clock times: `9:15am`, `9:15 AM`, `9pm`, `17:30` (a bare `9` is rejected as ambiguous)
- Durations: `1:30`, `1h30m`, `2h`, `45m`, `1.5h`
- `-s` / `--short` prints only `H:MM`, handy in scripts

## Examples

```
kit time between 9:15am 5:30pm       # 8h 15m - length of a shift
kit time between 10:30 PM 1:15 AM    # 2h 45m - wraps past midnight
kit time diff 2:18 1:18              # 1h 00m - gap between two durations
kit t sum 1:45 2h30m 0.75h           # 5h 00m - total logged time
```
