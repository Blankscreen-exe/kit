# internet-speed

Test your connection's latency, jitter, download and upload speed against Cloudflare, and time TCP connections to any host.

## Usage

```
kit internet-speed [--quick] [--download-only | --upload-only] [--json] [--show-ip]
kit internet-speed ping <host> [-p PORT] [-n COUNT] [-t TIMEOUT]
```

- The speed test uses Cloudflare's public speed test servers (speed.cloudflare.com), picking the data centre nearest to you.
- **Latency** is the median round trip of 10 tiny requests, with Cloudflare's processing time subtracted.
  **Jitter** is how much consecutive round trips differ.
- **Download** and **upload** send bigger and bigger transfers (up to 25 MB down and 10 MB up) for about
  12 seconds each, and report the 90th-percentile speed. A full run uses up to about 110 MB of data.
- `--quick` uses smaller transfers and takes a few seconds (about 10 MB of data).
- `--json` prints only machine-readable results. Your public IP is shown only with `--show-ip`.
- `ping` times TCP connections rather than ICMP pings, so it works without admin rights and through firewalls
  that drop ping. It connects to port 443 unless you pass `-p`.
- A certificate or TLS error usually means antivirus HTTPS scanning (such as Avast's Web Shield) or a proxy is
  intercepting the connection.

## Examples

```
kit internet-speed                     # full test
kit internet-speed --quick             # a few seconds
kit internet-speed --download-only
kit internet-speed --json > speed.json
kit internet-speed ping github.com
kit internet-speed ping 10.0.0.5 -p 22 -n 10
```
