# dev

Developer utilities: UUIDs, hashes, base64, JWT decoding, timestamps, JSON, URL encoding and secrets.

## Usage

```
kit dev uuid [-n N] [--v7]
kit dev hash [TEXT] [--file FILE] [--algo md5|sha1|sha256|sha512|all]
kit dev b64 encode|decode [TEXT] [--url]
kit dev jwt [TOKEN]
kit dev time [VALUE] [--ms]
kit dev json [FILE] [--minify] [--indent N] [--sort-keys]
kit dev url encode|decode [TEXT] [--plus]
kit dev secret [--length N] [--chars alnum|hex|symbols|url]
```

- Add `--copy` to any command to also put the result on the clipboard
  (Windows: built in; Linux: needs `wl-copy`, `xclip` or `xsel`; macOS: `pbcopy`).
- Commands that take text also read it from a pipe when the argument is left out.
- `jwt` only decodes the token: the signature is **not** verified. It shows `iat`/`nbf`/`exp` as dates
  and says whether the token has expired. `--copy` copies the payload JSON.
- `time` accepts epoch seconds, epoch milliseconds (numbers of 12+ digits, or `--ms`) and ISO 8601 dates.
  An ISO date without an offset is treated as local time. `--copy` copies the converted value.
- `json` points at the line and column of the first error in invalid JSON.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `dev.secret_length` | `32` | Length `secret` uses when you don't pass `--length` (1-4096) |

```
kit config set dev.secret_length 48
```

## Examples

```
kit dev uuid -n 3 --v7                   # three time-ordered UUIDs
kit dev hash --file setup.exe --algo all
kit dev b64 encode "hello world" --copy
kit dev jwt "Bearer eyJhbGciOi..."       # decode a token from a request header
kit dev time 1700000000                  # epoch -> UTC, local and relative
kit dev time 2024-05-01T12:00:00Z        # ISO -> epoch
Get-Content data.json | kit dev json     # pretty-print piped JSON
kit dev url encode "a b&c"
kit dev secret --length 48 --chars symbols --copy
```
