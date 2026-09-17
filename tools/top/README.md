# top

Find suspicious programs: a live process list that flags odd locations, fake system names, missing signatures, shady command lines, backdoor ports and autostart entries.

It's a triage aid, not an antivirus. It points at things worth a closer look and explains why, so
you can decide. Something flagged isn't necessarily infected, and something marked ok isn't guaranteed safe.

## Usage

```
kit top                       # live view (alias: kit procs)
kit top --flagged             # live view, only flagged processes
kit top --scan [--all] [--json] [--no-autostart]
kit top --watch SECONDS       # keep scanning, print new findings as they appear
kit top --trusted             # list the programs you trusted
kit top --untrust PATH
```

### The live view

Three tabs:

- **Processes**: every process with a risk level, CPU, memory, user, number of network connections,
  the main reason it's flagged, and its path. It's sorted by risk by default.
- **Network**: every process with an open connection or a listening port. Connections to ports that
  backdoors, IRC botnets and mining pools often use are highlighted.
- **Autostart**: what starts by itself. On Windows that's Run/RunOnce keys, Startup folders, scheduled tasks,
  services running from outside `C:\Windows`, Winlogon Shell/Userinit, AppInit_DLLs and IFEO "debugger"
  hijacks. On Linux it's crontabs and `/etc/cron.*`, systemd units in `/etc/systemd/system` and your user
  units, `.desktop` autostart files, suspicious lines in shell startup files, `/etc/ld.so.preload` and
  `/etc/rc.local`. On macOS it's LaunchAgents and LaunchDaemons.

| Key | What it does |
|---|---|
| `enter` | Details: full path, command line, parent chain, start time, user, file dates, signer, SHA-256, connections and every reason with its weight |
| `/` | Filter by name, path, PID, user, command line or reason (`esc` clears it) |
| `f` | Show only flagged processes |
| `s` `c` `m` `n` | Sort by risk, CPU, memory, name |
| `k` | Kill the process (asks first) |
| `p` | Suspend / resume it |
| `o` | Open the folder that holds the program |
| `y` / `h` | Copy the path / the SHA-256 |
| `v` | Open the file's VirusTotal page. Only the hash is sent, never the file. |
| `t` | Trust / untrust the program (see below) |
| `r` | Refresh now. On the Autostart tab it runs the autostart check again. |
| `1` `2` `3` | Switch tabs |
| `?` | Help |
| `q` | Quit |

The keys also work inside the details window, on the item it shows.

### Levels

Every warning sign adds points. The total decides the level:

| Level | Score | Typical causes |
|---|---|---|
| **high** | 60+ | a fake `svchost.exe`, a fake kernel thread, a crypto miner, a reverse shell, a program with a double extension like `invoice.pdf.exe`, a SYSTEM/root process running from a temp folder, `/etc/ld.so.preload` |
| **medium** | 30-59 | a program running from a temp or Downloads folder, a broken signature, an encoded PowerShell command, Office starting `cmd.exe`, a connection to port 4444 |
| **low** | 10-29 | a single weaker sign: a signed installer in Temp, a program in an unusual hidden folder |
| **ok** | under 10 | weak signs alone, like no signature or a new file, only count when they come with something else |

What it checks:

- **Where the program lives**: temp folders, Downloads, Desktop, Public, the Recycle Bin, the top of
  ProgramData or AppData\Roaming, `/tmp`, `/var/tmp`, `/dev/shm`, `~/.cache`, and hidden folders.
  Well-known developer folders such as `~/.local`, `~/.cargo` and `~/.vscode` are left alone. A hidden
  program file counts too.
- **Fake names**: Windows process names like `svchost.exe` or `lsass.exe` running from the wrong folder, and
  look-alikes such as `svch0st.exe` or `scvhost.exe`. On Linux: system service names running from odd
  places, `[kworker/…]`-style names on normal programs, and programs that claim to be `sshd`.
- **Signatures (Windows)**: unsigned, broken or untrusted Authenticode signatures. The check is offline
  and includes Windows catalog signatures. Signed programs get lower location scores.
- **Command lines**: encoded or hidden PowerShell, `Invoke-Expression`, download cradles, `certutil`/`bitsadmin`
  downloads, `mshta`/`regsvr32`/`rundll32` tricks, `curl | sh`, `base64 -d | sh`, `nc -e`, `/dev/tcp/`,
  Python reverse shells and miner markers (`stratum+tcp`, `xmrig`, …).
- **Parents**: Word, Excel, Outlook or a PDF reader starting a shell or script host.
- **Program file**: the file was deleted or replaced while the program runs, or it is new (less than 7 days old).
- **Network**: ports that backdoors, botnets, mining pools and Tor use, a listening port from an odd folder,
  and very many connections.
- **CPU**: sustained high CPU (over 50% of all cores for about 30 s), which counts extra when combined with other signs.

### False positives

Some legitimate things get flagged: installers and updaters running from Temp, developer tools and
unsigned open-source programs in your user folder, and games' anti-cheat. When you've checked a program,
press `t` to **trust** it. kit remembers the exact file (path + SHA-256), so it won't be flagged again
unless the file changes, whether through an update or tampering. The list is kept in
`%APPDATA%\kit\top-trusted.json` (Windows) or `~/.local/share/kit/top-trusted.json` (Linux), or in the file
`KIT_TOP_DATA` points to.

### Access

Without admin rights, some processes can't be inspected. The header says how many.
For a full picture, run it from an administrator terminal on Windows, or with `sudo` on Linux
(`sudo -E kit top` keeps your settings).

### --scan

`--scan` prints a report of flagged processes and autostart entries and exits. `--all` includes
everything, and `--json` prints it as JSON. It exits with **1 if anything high was found**, 0 otherwise,
so it can be used in scripts. `kit hub` opens `kit top` in a terminal window. Use `kit top --scan` from a
terminal for a report.

### If something really looks wrong

1. Look at the details (`enter`): who signed it, where it lives, what started it and what it's connected to.
2. Look the hash up on VirusTotal (`v`).
3. Suspend it (`p`) or kill it (`k`), and remove its autostart entry.
4. Get a second opinion:
   - Windows: a Microsoft Defender offline scan (`Start-MpWDOScan` in an administrator PowerShell)
   - Linux: `rkhunter --check`, `chkrootkit`, or ClamAV (`clamscan -r /`)
5. If it really is malware, changing passwords from another device and reinstalling are often the safe route.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `top.refresh` | `2` | Seconds between refreshes of the live view (0.5-60) |
| `top.only_flagged` | `false` | Start the live view showing only flagged processes |

```
kit config set top.refresh 5
```

## Examples

```
kit top                          # look around
kit top --scan                   # quick report
kit top --scan --all --json > report.json
kit top --watch 10 --no-autostart   # print new suspicious processes every 10 seconds
kit top --untrust "C:\Tools\thing.exe"
```
