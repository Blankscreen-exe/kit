# clean

Find and safely free disk space: temp files, the trash, package-manager caches and Docker leftovers.

## Usage

```
kit clean [--older-than DAYS]
kit clean run <category> [<category> ...] [--yes] [--dry-run]
kit clean run --all-safe [--yes] [--dry-run]
kit clean big [PATH] [--top N] [--limit SECONDS]
```

- `kit clean` only scans. It lists every category it found with its size, item count, location and a safety label,
  shows your disk's free space, and estimates how much free space you'd have afterwards. Nothing is deleted.
- `kit clean run` cleans the categories you name, or every `safe` and `re-downloads` category with `--all-safe`.
  It first shows exactly what will go (files and folders, or the command it will run) and asks before deleting.
  `--dry-run` stops after that list. Files that are in use are skipped. At the end it reports the space freed.
- `kit clean big` finds the largest folders and files under a path (your home folder by default), to track down
  what's filling the disk. It skips folders it can't read and stops after `--limit` seconds (default 60).

Safety labels:

| Label | Meaning |
|---|---|
| `safe` | Leftovers nothing needs: old temp files, the trash, crash dumps, thumbnails, untagged Docker images. |
| `re-downloads` | Caches. Deleting them is harmless, but packages are downloaded or rebuilt again next time. |
| `careful` | Can remove things you may still want. Never included in `--all-safe`; only cleaned when you name it. |
| `report only` | Shown for information. kit never deletes these (e.g. Docker volumes, which hold your databases). |

Categories, when the tool or folder exists on this machine:

| Category | What | How it's cleaned |
|---|---|---|
| `temp` | Your temp folder files not changed in `--older-than` days (default 2) | deletes the files |
| `trash` | Recycle Bin (Windows) or Trash (Linux/macOS) | empties it |
| `npm`, `pip`, `uv`, `yarn`, `pnpm`, `bun` | Package-manager caches | the tool's own cache-clean command |
| `go-build` | Go build cache | `go clean -cache` |
| `nuget` | NuGet/.NET package caches | `dotnet nuget locals all --clear` |
| `docker-build-cache` | Docker build cache | `docker builder prune -a -f` |
| `docker-dangling` | Untagged `<none>` Docker images | `docker image prune -f` |
| `docker-unused-images` | Images no container uses (**careful**) | `docker image prune -a -f` |
| `docker-volumes` | Docker volumes (**report only**) | never |
| `crash-dumps` | Windows crash dumps | deletes the files |
| `windows-temp` | `C:\Windows\Temp` (**report only**, needs admin) | clear it from an admin terminal or Disk Cleanup |
| `thumbnails` | Linux thumbnail cache | deletes the files |
| `journal` | systemd journal logs (**report only**) | `sudo journalctl --vacuum-time=2weeks` |

On Linux, `temp` only counts files in `/tmp` and `/var/tmp` that belong to you.

## Examples

```
kit clean                               # what could be freed?
kit clean run temp trash                # clean two categories (asks first)
kit clean run --all-safe --dry-run      # see what --all-safe would do
kit clean run docker-build-cache --yes  # no question
kit clean run temp --older-than 7       # only temp files older than a week
kit clean big                           # what's big in your home folder?
kit clean big C:\ --top 25 --limit 120
```
