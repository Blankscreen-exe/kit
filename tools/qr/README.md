# qr

Show a QR code in the terminal for any text or URL, or save it as a PNG or SVG.

## Usage

```
kit qr <text...> [--png FILE] [--svg FILE] [--show] [--invert] [--error L|M|Q|H]
kit qr --wifi SSID [--password PASS] [--security WPA|WEP|nopass] [--hidden]
```

- Text can also be piped in: `"hello" | kit qr`.
- `--png` / `--svg` save the code to a file instead of drawing it; add `--show` to do both. `--scale` sets pixels per module (default 10).
- `--wifi` makes a code that phones scan to join a Wi-Fi network. Security defaults to WPA when a password is given, otherwise `nopass`.
- In colour terminals the code is drawn black on white, so it scans on any background. Without colour (e.g. `NO_COLOR` is set
  or the output is piped) it assumes a dark terminal; use `--invert` on a light one.
- `--error` picks the error-correction level (default `M`); `H` survives more damage but makes a bigger code.

## Examples

```
kit qr https://github.com                         # draw it in the terminal
kit qr "meet at 5" --png note.png                 # save a PNG instead
kit qr https://example.com --svg code.svg --show  # save and draw
kit qr --wifi HomeNet --password "s3cret"         # let a phone join your Wi-Fi
"piped text" | kit qr
```
