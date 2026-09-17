"""Developer utilities: UUIDs, hashes, base64, JWT decoding, timestamps, JSON, URL encoding, secrets."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import secrets
import string
import sys
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

from kitlib import die, style, warn
from kitlib.clipboard import copy_to_clipboard
from kitlib.settings import tool_settings

HASH_ALGOS = ["md5", "sha1", "sha256", "sha512"]
SECRET_CHARSETS = {
    "alnum": string.ascii_letters + string.digits,
    "hex": "0123456789abcdef",
    "symbols": string.ascii_letters + string.digits + "!#$%&()*+,-./:;<=>?@[]^_{|}~",
    "url": string.ascii_letters + string.digits + "-_",
}


class Result:
    """What a subcommand prints, and the part of it --copy puts on the clipboard."""

    def __init__(self, text: str, copy: str | None = None) -> None:
        self.text = text
        self.copy = text if copy is None else copy


# --- input helpers -------------------------------------------------------------

def read_stdin() -> str:
    # PowerShell pipes text to native programs with a UTF-8 BOM; drop it.
    return sys.stdin.read().lstrip("﻿")


def text_or_stdin(value: str | None, what: str) -> str:
    if value is not None:
        return value
    if sys.stdin.isatty():
        die(f"give {what} as an argument or pipe it in")
    return read_stdin().rstrip("\r\n")


# --- uuid ----------------------------------------------------------------------

def uuid7() -> uuid.UUID:
    """RFC 9562 UUIDv7: 48-bit Unix milliseconds, version, 74 random bits."""
    millis = time.time_ns() // 1_000_000
    rand = int.from_bytes(secrets.token_bytes(10), "big")
    rand_a = (rand >> 68) & 0xFFF
    rand_b = rand & ((1 << 62) - 1)
    value = (millis & ((1 << 48) - 1)) << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    return uuid.UUID(int=value)


def cmd_uuid(args: argparse.Namespace) -> Result:
    if args.count < 1:
        die("-n must be at least 1")
    if args.v7:
        # Same-millisecond v7 values are ordered by their random bits, so sort to keep the list time-ordered.
        values = sorted((uuid7() for _ in range(args.count)), key=lambda u: u.int)
    else:
        values = [uuid.uuid4() for _ in range(args.count)]
    return Result("\n".join(str(v) for v in values))


# --- hash ----------------------------------------------------------------------

def cmd_hash(args: argparse.Namespace) -> Result:
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            die(f"file not found: {args.file}")
        digests = {name: hashlib.new(name) for name in HASH_ALGOS}
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                for digest in digests.values():
                    digest.update(chunk)
        hexes = {name: d.hexdigest() for name, d in digests.items()}
    else:
        data = text_or_stdin(args.text, "text to hash (or use --file)").encode("utf-8")
        hexes = {name: hashlib.new(name, data).hexdigest() for name in HASH_ALGOS}

    if args.algo == "all":
        width = max(map(len, HASH_ALGOS))
        return Result("\n".join(f"{style(name.ljust(width), 'dim')}  {value}" for name, value in hexes.items()),
                      copy="\n".join(f"{name}  {value}" for name, value in hexes.items()))
    return Result(hexes[args.algo])


# --- base64 --------------------------------------------------------------------

def b64_decode_bytes(value: str, url: bool) -> bytes:
    cleaned = "".join(value.split())
    cleaned += "=" * (-len(cleaned) % 4)
    try:
        if url or "-" in cleaned or "_" in cleaned:
            return base64.urlsafe_b64decode(cleaned)
        return base64.b64decode(cleaned, validate=True)
    except (binascii.Error, ValueError) as exc:
        die(f"not valid base64: {exc}")


def cmd_b64(args: argparse.Namespace) -> Result:
    value = text_or_stdin(args.text, "text")
    if args.action == "encode":
        raw = value.encode("utf-8")
        encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=") if args.url else base64.b64encode(raw).decode()
        return Result(encoded)
    decoded = b64_decode_bytes(value, args.url)
    try:
        return Result(decoded.decode("utf-8"))
    except UnicodeDecodeError:
        warn("decoded bytes are not UTF-8 text; showing them as hex")
        return Result(decoded.hex())


# --- jwt -----------------------------------------------------------------------

def humanize_delta(seconds: float) -> str:
    future = seconds > 0
    seconds = abs(int(seconds))
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            count = seconds // size
            text = f"{count} {unit}{'s' if count != 1 else ''}"
            break
    else:
        text = f"{seconds} second{'s' if seconds != 1 else ''}"
    return f"in {text}" if future else f"{text} ago"


def format_epoch(value: float) -> str:
    moment = datetime.fromtimestamp(value, tz=timezone.utc)
    return f"{moment.strftime('%Y-%m-%d %H:%M:%S UTC')} ({humanize_delta(value - time.time())})"


def decode_jwt_segment(segment: str, name: str) -> dict:
    try:
        return json.loads(b64_decode_bytes(segment, url=True).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        die(f"the JWT {name} is not base64url-encoded JSON")


def cmd_jwt(args: argparse.Namespace) -> Result:
    token = text_or_stdin(args.token, "a JWT").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    parts = token.split(".")
    if len(parts) != 3:
        die(f"a JWT has 3 dot-separated parts, this has {len(parts)}")
    header = decode_jwt_segment(parts[0], "header")
    payload = decode_jwt_segment(parts[1], "payload")

    lines = [style("HEADER", "bold"), json.dumps(header, indent=2, ensure_ascii=False), "",
             style("PAYLOAD", "bold"), json.dumps(payload, indent=2, ensure_ascii=False)]

    times = {key: payload[key] for key in ("iat", "nbf", "exp") if isinstance(payload.get(key), (int, float))}
    if times:
        lines += ["", style("TIMES", "bold")]
        for key, value in times.items():
            lines.append(f"  {key:<6} {format_epoch(value)}")
        now = time.time()
        if "exp" in times and times["exp"] <= now:
            status = style("EXPIRED", "bold", "red")
        elif "nbf" in times and times["nbf"] > now:
            status = style("NOT YET VALID", "bold", "yellow")
        else:
            status = style("within its validity window", "green")
        lines.append(f"  {'status':<6} {status}")

    lines += ["", style("Signature NOT verified - this only decodes the token.", "yellow")]
    return Result("\n".join(lines), copy=json.dumps(payload, indent=2, ensure_ascii=False))


# --- time ----------------------------------------------------------------------

def parse_moment(value: str, force_ms: bool) -> tuple[datetime, str]:
    """(aware datetime, kind of input: 'epoch' or 'iso')."""
    stripped = value.strip()
    try:
        number = float(stripped)
    except ValueError:
        number = None
    if number is not None:
        seconds = number / 1000 if force_ms or abs(number) >= 1e11 else number
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc), "epoch"
        except (OverflowError, OSError, ValueError):
            die(f"timestamp out of range: {value}")
    text = stripped[:-1] + "+00:00" if stripped.endswith(("Z", "z")) else stripped
    if "T" not in text and " " in text:
        text = text.replace(" ", "T", 1)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        die(f"not an epoch timestamp or ISO 8601 date: '{value}' (e.g. 1700000000, 2024-05-01T12:00:00Z)")
    if moment.tzinfo is None:
        moment = moment.astimezone()  # no offset given: treat it as local time
    return moment, "iso"


def cmd_time(args: argparse.Namespace) -> Result:
    if args.value in (None, "now"):
        moment, kind = datetime.now(timezone.utc).replace(microsecond=0), "now"
    else:
        moment, kind = parse_moment(args.value, args.ms)
    seconds = moment.timestamp()
    utc = moment.astimezone(timezone.utc)
    local = moment.astimezone()
    epoch_s = int(seconds) if seconds == int(seconds) else round(seconds, 3)
    iso_utc = utc.isoformat(timespec="milliseconds" if utc.microsecond else "seconds").replace("+00:00", "Z")

    rows = [
        ("epoch s", str(epoch_s)),
        ("epoch ms", str(round(seconds * 1000))),
        ("UTC", iso_utc),
        ("local", local.isoformat(timespec="seconds")),
        ("relative", "now" if kind == "now" else humanize_delta(seconds - time.time())),
    ]
    text = "\n".join(f"{style(label.ljust(9), 'dim')} {value}" for label, value in rows)
    # Copy the converted form: epoch in -> ISO out, ISO (or now) in -> epoch seconds out.
    return Result(text, copy=iso_utc if kind == "epoch" else str(epoch_s))


# --- json ----------------------------------------------------------------------

def cmd_json(args: argparse.Namespace) -> Result:
    if args.file and args.file != "-":
        path = Path(args.file)
        if not path.is_file():
            die(f"file not found: {args.file}")
        source, name = path.read_text(encoding="utf-8-sig"), args.file
    else:
        if sys.stdin.isatty():
            die("give a JSON file or pipe JSON in")
        source, name = read_stdin(), "<stdin>"

    try:
        data = json.loads(source)
    except json.JSONDecodeError as exc:
        lines = source.splitlines()
        print(f"{style('error:', 'bold', 'red')} invalid JSON in {name} at line {exc.lineno}, column {exc.colno}: {exc.msg}",
              file=sys.stderr)
        if 0 < exc.lineno <= len(lines):
            pointer = " " * (exc.colno - 1) + "^"
            print(f"  {lines[exc.lineno - 1]}\n  {style(pointer, 'red')}", file=sys.stderr)
        raise SystemExit(1)

    if args.minify:
        return Result(json.dumps(data, separators=(",", ":"), ensure_ascii=False, sort_keys=args.sort_keys))
    return Result(json.dumps(data, indent=args.indent, ensure_ascii=False, sort_keys=args.sort_keys))


# --- url -----------------------------------------------------------------------

def cmd_url(args: argparse.Namespace) -> Result:
    value = text_or_stdin(args.text, "text")
    if args.action == "encode":
        return Result(urllib.parse.quote_plus(value) if args.plus else urllib.parse.quote(value, safe=""))
    return Result(urllib.parse.unquote_plus(value) if args.plus else urllib.parse.unquote(value))


# --- secret --------------------------------------------------------------------

def cmd_secret(args: argparse.Namespace) -> Result:
    if not 1 <= args.length <= 4096:
        die("--length must be between 1 and 4096")
    alphabet = SECRET_CHARSETS[args.chars]
    return Result("".join(secrets.choice(alphabet) for _ in range(args.length)))


# --- main ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    # --copy works before or after the subcommand; SUPPRESS keeps a subparser from resetting it to False.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--copy", action="store_true", default=argparse.SUPPRESS, help="also copy the result to the clipboard")

    parser = argparse.ArgumentParser(prog="kit dev", description="Developer utilities.", parents=[common])
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = commands.add_parser("uuid", parents=[common], help="generate UUIDs (v4, or time-ordered v7)")
    p.add_argument("-n", "--count", type=int, default=1, help="how many (default: 1)")
    p.add_argument("--v7", action="store_true", help="time-ordered UUIDv7 instead of random v4")
    p.set_defaults(handler=cmd_uuid)

    p = commands.add_parser("hash", parents=[common], help="hash text, stdin or a file")
    p.add_argument("text", nargs="?", help="text to hash (or pipe it in)")
    p.add_argument("--file", help="hash a file instead")
    p.add_argument("--algo", choices=[*HASH_ALGOS, "all"], default="sha256", help="algorithm (default: sha256)")
    p.set_defaults(handler=cmd_hash)

    p = commands.add_parser("b64", parents=[common], help="base64 encode or decode")
    p.add_argument("action", choices=["encode", "decode"])
    p.add_argument("text", nargs="?", help="input (or pipe it in)")
    p.add_argument("--url", action="store_true", help="URL-safe alphabet without padding")
    p.set_defaults(handler=cmd_b64)

    p = commands.add_parser("jwt", parents=[common], help="decode a JWT (signature is NOT verified)")
    p.add_argument("token", nargs="?", help="the token, optionally with 'Bearer ' (or pipe it in)")
    p.set_defaults(handler=cmd_jwt)

    p = commands.add_parser("time", parents=[common], help="convert between epoch timestamps and ISO 8601")
    p.add_argument("value", nargs="?", help="epoch seconds/ms or an ISO 8601 date (default: now)")
    p.add_argument("--ms", action="store_true", help="treat a number as milliseconds")
    p.set_defaults(handler=cmd_time)

    p = commands.add_parser("json", parents=[common], help="pretty-print or minify JSON")
    p.add_argument("file", nargs="?", help="JSON file (or pipe JSON in)")
    p.add_argument("--minify", action="store_true", help="compact output on one line")
    p.add_argument("--indent", type=int, default=2, help="indent width (default: 2)")
    p.add_argument("--sort-keys", action="store_true", help="sort object keys")
    p.set_defaults(handler=cmd_json)

    p = commands.add_parser("url", parents=[common], help="percent-encode or decode text")
    p.add_argument("action", choices=["encode", "decode"])
    p.add_argument("text", nargs="?", help="input (or pipe it in)")
    p.add_argument("--plus", action="store_true", help="form encoding: spaces as +")
    p.set_defaults(handler=cmd_url)

    p = commands.add_parser("secret", parents=[common], help="generate a secure random string")
    p.add_argument("--length", type=int, default=tool_settings().get("secret_length", 32),
                   help="length (setting: dev.secret_length, default: 32)")
    p.add_argument("--chars", choices=list(SECRET_CHARSETS), default="alnum", help="character set (default: alnum)")
    p.set_defaults(handler=cmd_secret)
    return parser


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args()
    result = args.handler(args)
    print(result.text)
    if getattr(args, "copy", False):
        if copy_to_clipboard(result.copy):
            print(style("copied to clipboard", "dim"), file=sys.stderr)
        else:
            warn("couldn't copy: no clipboard available (Linux: install wl-clipboard, xclip or xsel)")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
