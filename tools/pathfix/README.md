# pathfix

Find and clean up duplicate, missing and redundant folders in your PATH.

## Usage

```
kit pathfix [-q]                         # report only, changes nothing
kit pathfix --apply [--keep-missing] [-y]
kit pathfix --restore <backup>           # Windows
kit pathfix --undo                       # Linux/macOS
```

Each entry is marked as one of:

- `ok`
- `duplicate`: the same folder appears earlier in the same PATH
- `redundant`: a user PATH entry that the machine PATH already has (Windows)
- `missing`: the folder doesn't exist
- `empty`: a blank entry, e.g. from `;;` or a trailing `;`

Paths are compared after expanding variables like `%USERPROFILE%`, ignoring trailing slashes,
and ignoring case on Windows.

**Windows**

- The report covers your user PATH and the machine PATH, both read straight from the registry.
- `--apply` rewrites the **user** PATH only. It saves a backup to `%LOCALAPPDATA%\kit\backups` first,
  keeps `%VARIABLES%` unexpanded, and tells other programs about the change, so new terminals see it.
- The machine PATH needs admin rights, so problems there are reported for you to fix by hand.
- `--restore` puts a backup back.

**Linux / macOS**

- The report covers the current `$PATH`.
- PATH is assembled by many startup files, so `--apply` doesn't edit them. Instead it adds a commented,
  marked block to `~/.bashrc` (and `~/.zshrc` if you have one). The block rebuilds PATH without duplicates
  or missing folders at every shell start, and the rc files are backed up to `~/.local/state/kit/backups` first.
- `--undo` removes the block.

`--keep-missing` leaves non-existent folders alone, which is useful for drives that are sometimes unplugged.

## Examples

```
kit pathfix              # see what's wrong
kit pathfix -q           # only the problems
kit pathfix --apply      # fix them (asks first)
kit pathfix --apply --keep-missing -y
```
