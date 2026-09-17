"""Friendly shortcuts for everyday ffmpeg jobs: info, convert, trim, gif, audio, compress, resize and more."""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from kitlib import die, error, style, warn
from kitlib.settings import tool_settings

IS_WINDOWS = os.name == "nt"
NULL_DEVICE = "NUL" if IS_WINDOWS else "/dev/null"

INSTALL_HINT = """ffmpeg isn't installed, or kit can't find it. Install it with one of:
  Windows        winget install Gyan.FFmpeg
  Debian/Ubuntu  sudo apt install ffmpeg
  Fedora         sudo dnf install ffmpeg
  Arch           sudo pacman -S ffmpeg
  macOS          brew install ffmpeg
then open a new terminal. If it's installed somewhere unusual, point kit at it:
  kit config set media.ffmpeg "<path to ffmpeg or its folder>\""""


# --- formats -----------------------------------------------------------------------

ANY = None  # a container that takes any codec as-is

H264 = ("-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p")
VP9 = ("-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1")
MPEG4 = ("-c:v", "mpeg4", "-q:v", "4")
AAC = ("-c:a", "aac", "-b:a", "192k")
OPUS = ("-c:a", "libopus", "-b:a", "128k")
MP3 = ("-c:a", "libmp3lame", "-q:a", "2")
FASTSTART = ("-movflags", "+faststart")
PCM = frozenset({"pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_u8"})


@dataclass(frozen=True)
class Format:
    video: tuple[str, ...]  # encoder options; empty for audio-only formats
    audio: tuple[str, ...]
    copy_video: frozenset[str] | None = frozenset()  # codecs the container accepts without re-encoding
    copy_audio: frozenset[str] | None = frozenset()
    extra: tuple[str, ...] = ()

    @property
    def audio_only(self) -> bool:
        return not self.video


MP4_VIDEO = frozenset({"h264", "hevc", "av1", "mpeg4", "vp9"})
MP4_AUDIO = frozenset({"aac", "mp3", "alac", "ac3", "eac3", "opus", "flac"})

FORMATS: dict[str, Format] = {
    "mp4": Format(H264, AAC, MP4_VIDEO, MP4_AUDIO, FASTSTART),
    "m4v": Format(H264, AAC, MP4_VIDEO, MP4_AUDIO, FASTSTART),
    "mov": Format(H264, AAC, frozenset({"h264", "hevc", "prores", "mpeg4", "mjpeg"}),
                  frozenset({"aac", "alac", "mp3"}) | PCM, FASTSTART),
    "mkv": Format(H264, AAC, ANY, ANY),
    "webm": Format(VP9, OPUS, frozenset({"vp8", "vp9", "av1"}), frozenset({"opus", "vorbis"})),
    "avi": Format(MPEG4, MP3, frozenset({"mpeg4", "h264", "mjpeg", "msmpeg4v3"}), frozenset({"mp3", "ac3"}) | PCM),
    "mp3": Format((), MP3, copy_audio=frozenset({"mp3"})),
    "m4a": Format((), AAC, copy_audio=frozenset({"aac", "alac"}), extra=FASTSTART),
    "aac": Format((), AAC, copy_audio=frozenset({"aac"})),
    "wav": Format((), ("-c:a", "pcm_s16le"), copy_audio=PCM),
    "flac": Format((), ("-c:a", "flac"), copy_audio=frozenset({"flac"})),
    "opus": Format((), OPUS, copy_audio=frozenset({"opus"})),
    "ogg": Format((), ("-c:a", "libvorbis", "-q:a", "5"), copy_audio=frozenset({"vorbis", "opus", "flac"})),
}
CONVERT_TARGETS = sorted([*FORMATS, "gif"])
AUDIO_FORMATS = ["mp3", "m4a", "wav", "flac", "opus"]
IMAGE_FORMATS = ["jpg", "png", "webp"]
PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]


def fits(allowed: frozenset[str] | None, codec: str | None) -> bool:
    return codec is None or allowed is ANY or codec in allowed


def encode_args(ext: str, keep_audio: str | None = None) -> list[str]:
    """Encoder options for a container; `keep_audio` is the source audio codec, copied when the container takes it."""
    fmt = FORMATS.get(ext)
    if fmt is None:
        return []  # unknown container: let ffmpeg pick its default codecs
    audio = ["-c:a", "copy"] if keep_audio and fits(fmt.copy_audio, keep_audio) else list(fmt.audio)
    if fmt.audio_only:
        return ["-vn", *audio, *fmt.extra]
    return [*fmt.video, *audio, *fmt.extra]


# --- parsing and formatting -------------------------------------------------------

TIME_UNITS = {"ms": 0.001, "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600}
SIZE_UNITS = {"": 1, "b": 1, "k": 1024, "kb": 1024, "kib": 1024, "m": 1024**2, "mb": 1024**2, "mib": 1024**2,
              "g": 1024**3, "gb": 1024**3, "gib": 1024**3}


def parse_time(text: str) -> float:
    """90, 1:30, 00:01:30.5, 45s, 1.5m or 2h -> seconds."""
    raw = text.strip().lower()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ms|sec|s|min|m|h)", raw)
    if match:
        return float(match[1]) * TIME_UNITS[match[2]]
    parts = raw.split(":")
    if 1 <= len(parts) <= 3 and all(re.fullmatch(r"\d+", p) for p in parts[:-1]) \
            and re.fullmatch(r"\d+(?:\.\d+)?", parts[-1]) \
            and all(float(p) < 60 for p in parts[1:]):
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + float(part)
        return seconds
    raise argparse.ArgumentTypeError(f"invalid time '{text}' (use 90, 1:30, 00:01:30.5 or 45s)")


def parse_size(text: str) -> int:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-z]*)", text.strip().lower())
    if not match or match[2] not in SIZE_UNITS or float(match[1]) <= 0:
        raise argparse.ArgumentTypeError(f"invalid size '{text}' (use e.g. 25MB, 800KB or 1.5GB)")
    return int(float(match[1]) * SIZE_UNITS[match[2]])


def parse_scale(text: str) -> float:
    raw = text.strip()
    try:
        value = float(raw[:-1]) / 100 if raw.endswith("%") else float(raw)
    except ValueError:
        value = 0
    if not 0 < value <= 10:
        raise argparse.ArgumentTypeError(f"invalid scale '{text}' (use e.g. 50% or 0.5)")
    return value


def positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive whole number, got '{text}'")
    return value


def crf_value(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = -1
    if not 0 <= value <= 51:
        raise argparse.ArgumentTypeError(f"expected a whole number from 0 to 51, got '{text}'")
    return value


def format_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def format_duration(seconds: float, decimals: int = 1) -> str:
    seconds = max(0.0, seconds)
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(int(minutes), 60)
    text = f"{secs:0{3 + decimals if decimals else 2}.{decimals}f}"
    return f"{hours}:{minutes:02d}:{text}" if hours else f"{minutes}:{text}"


def format_bitrate(bits: object) -> str:
    try:
        value = int(bits)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    return f"{value / 1_000_000:.1f} Mb/s" if value >= 10_000_000 else f"{value // 1000} kb/s"


def quote(arg: str) -> str:
    if IS_WINDOWS:
        return arg if re.fullmatch(r"[\w@%+=:,./\\-]+", arg) else '"' + arg.replace('"', '\\"') + '"'
    return shlex.quote(arg)


# --- finding ffmpeg ----------------------------------------------------------------

@dataclass
class FF:
    ffmpeg: str
    ffprobe: str


def candidate_paths() -> list[Path]:
    if IS_WINDOWS:
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        packages = local / "Microsoft" / "WinGet" / "Packages"
        found = [local / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"]
        for pattern in ("*FFmpeg*/*/bin/ffmpeg.exe", "*FFmpeg*/bin/ffmpeg.exe"):
            found += sorted(packages.glob(pattern), reverse=True)
        program_data = Path(os.environ.get("ProgramData") or r"C:\ProgramData")
        program_files = Path(os.environ.get("ProgramFiles") or r"C:\Program Files")
        return found + [
            Path.home() / "scoop" / "shims" / "ffmpeg.exe",
            program_data / "chocolatey" / "bin" / "ffmpeg.exe",
            Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
            program_files / "ffmpeg" / "bin" / "ffmpeg.exe",
        ]
    return [Path(p) / "ffmpeg" for p in (
        "/usr/local/bin", "/opt/homebrew/bin", "/snap/bin", Path.home() / ".local" / "bin",
        "/home/linuxbrew/.linuxbrew/bin", "/opt/local/bin",
    )]


def find_ffmpeg(choice: str) -> FF:
    exe = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
    if choice:
        path = Path(os.path.expandvars(choice)).expanduser()
        if path.is_dir():
            path = path / exe if (path / exe).is_file() else path / "bin" / exe
        ffmpeg = str(path) if path.is_file() else shutil.which(choice)
        if not ffmpeg:
            die(f"ffmpeg not found at '{choice}' (from --ffmpeg, $KIT_FFMPEG or the media.ffmpeg setting)")
    else:
        ffmpeg = shutil.which("ffmpeg") or next((str(p) for p in candidate_paths() if p.is_file()), None)
        if not ffmpeg:
            die(INSTALL_HINT)

    sibling = Path(ffmpeg).with_name("ffprobe.exe" if IS_WINDOWS else "ffprobe")
    ffprobe = str(sibling) if sibling.is_file() else shutil.which("ffprobe")
    if not ffprobe:
        die(f"found ffmpeg ({ffmpeg}) but not ffprobe, which comes with it - reinstall ffmpeg\n\n{INSTALL_HINT}")
    return FF(ffmpeg, ffprobe)


# --- probing -----------------------------------------------------------------------

@dataclass
class Media:
    path: Path
    data: dict

    @property
    def format(self) -> dict:
        return self.data.get("format", {})

    @property
    def streams(self) -> list[dict]:
        return self.data.get("streams", [])

    @property
    def duration(self) -> float:
        try:
            return float(self.format.get("duration", 0))
        except ValueError:
            return 0.0

    @property
    def video(self) -> dict | None:
        return next((s for s in self.streams if s.get("codec_type") == "video"
                     and not s.get("disposition", {}).get("attached_pic")), None)

    @property
    def audio(self) -> dict | None:
        return next((s for s in self.streams if s.get("codec_type") == "audio"), None)

    def codec(self, kind: str) -> str | None:
        stream = self.video if kind == "video" else self.audio
        return stream.get("codec_name") if stream else None


def probe_raw(ff: FF, path: Path) -> dict | None:
    result = subprocess.run(
        [ff.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip().splitlines()
        error(f"can't read {path}: {message[-1] if message else 'ffprobe failed'}")
        return None
    return json.loads(result.stdout.decode("utf-8", "replace"))


def probe(ff: FF, path: Path) -> Media | None:
    data = probe_raw(ff, path)
    return Media(path, data) if data is not None else None


def frame_rate(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = str(stream.get(key, "0/0")).partition("/")
        try:
            value = float(num) / float(den or 1)
        except (ValueError, ZeroDivisionError):
            continue
        if 0 < value < 1000:
            return value
    return 0.0


# --- running ffmpeg ----------------------------------------------------------------

interrupted = threading.Event()


def on_signal(signum, frame) -> None:
    interrupted.set()
    raise KeyboardInterrupt


def install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    if IS_WINDOWS:
        signal.signal(signal.SIGBREAK, on_signal)  # Ctrl+Break, and what kit hub-like launchers send


class Progress:
    """A one-line bar redrawn in place; silent when stdout isn't a terminal."""

    def __init__(self, label: str, duration: float) -> None:
        self.label = label
        self.duration = duration
        self.enabled = sys.stdout.isatty()
        self.drawn = False
        self.last = 0.0

    def update(self, position: float, speed: str) -> None:
        now = time.monotonic()
        if not self.enabled or now - self.last < 0.15:
            return
        self.last = now
        width = shutil.get_terminal_size((80, 20)).columns - 1
        speed_text = speed if speed and speed != "N/A" else ""
        if self.duration > 0:
            fraction = min(max(position / self.duration, 0.0), 1.0)
            eta = ""
            try:
                factor = float(speed.rstrip("x"))
                if factor > 0 and fraction > 0:
                    eta = f"ETA {format_duration((self.duration - position) / factor, 0)}"
            except (ValueError, AttributeError):
                pass
            cells = 24
            filled = int(fraction * cells)
            bar = style("█" * filled, "cyan") + style("░" * (cells - filled), "dim")
            tail = f" {fraction * 100:3.0f}%  {format_duration(position, 0)}/{format_duration(self.duration, 0)}  {speed_text}  {eta}"
            line = f"  {self.label}{bar}{tail}"
            visible = len(f"  {self.label}") + cells + len(tail)
        else:
            line = f"  {self.label}{format_duration(position, 0)}  {speed_text}"
            visible = len(line)
        sys.stdout.write("\r" + line + " " * max(0, width - visible))
        sys.stdout.flush()
        self.drawn = True

    def finish(self) -> None:
        if self.drawn:
            width = shutil.get_terminal_size((80, 20)).columns - 1
            sys.stdout.write("\r" + " " * width + "\r")
            sys.stdout.flush()
            self.drawn = False


def show_command(args: list[str]) -> None:
    print(style("  $ " + " ".join(quote(a) for a in ["ffmpeg", *args]), "dim"))


def remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def stop_process(process: subprocess.Popen) -> None:
    try:
        process.wait(timeout=2)  # Ctrl+C reaches ffmpeg too; give it a moment to exit by itself
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def run_ffmpeg(ff: FF, args: list[str], *, duration: float, output: Path | None, dry_run: bool,
               label: str = "", cwd: str | None = None, cleanup: Callable[[], None] | None = None,
               show: bool = True) -> bool:
    """Runs one ffmpeg command with a progress bar. On failure or Ctrl+C the partial output is deleted."""
    if show:
        show_command(args)
    if dry_run:
        return True
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)

    def discard() -> None:
        if output is not None:
            remove_quietly(output)
        if cleanup is not None:
            cleanup()

    command = [ff.ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error",
               "-progress", "pipe:1", "-y", *args]
    progress = Progress(label, duration)
    stderr: list[str] = []
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, cwd=cwd)
    reader = threading.Thread(
        target=lambda: stderr.extend(process.stderr.read().decode("utf-8", "replace").splitlines()),  # type: ignore[union-attr]
        daemon=True,
    )
    reader.start()
    position, speed = 0.0, ""
    try:
        for raw in process.stdout:  # type: ignore[union-attr]
            key, _, value = raw.decode("utf-8", "replace").strip().partition("=")
            if key in ("out_time_us", "out_time_ms"):  # both are microseconds
                try:
                    position = int(value) / 1_000_000
                except ValueError:
                    pass
            elif key == "speed":
                speed = value.strip()
            elif key == "progress":
                progress.update(position, speed)
        process.wait()
        reader.join(timeout=5)
    except KeyboardInterrupt:
        interrupted.set()
    finally:
        progress.finish()
        if interrupted.is_set():
            stop_process(process)
            discard()
            print(style("stopped - partial output removed", "yellow"))
            raise SystemExit(130)

    if process.returncode != 0:
        discard()
        error(f"ffmpeg failed (exit code {process.returncode})")
        for line in stderr[-15:]:
            print(style(f"  {line}", "dim"), file=sys.stderr)
        missing = re.search(r"Unknown encoder '([^']+)'|Encoder (\S+) not found", "\n".join(stderr))
        if missing:
            print(f"  This ffmpeg build doesn't include the {missing[1] or missing[2]} encoder. Install a full build "
                  "(Windows: winget install Gyan.FFmpeg; Fedora: ffmpeg from RPM Fusion).", file=sys.stderr)
        return False
    return True


# --- inputs and outputs --------------------------------------------------------------

def expand_inputs(patterns: list[str]) -> list[Path]:
    """Files as given, with wildcards expanded here because PowerShell and cmd don't do it."""
    paths: list[Path] = []
    for pattern in patterns:
        path = Path(pattern).expanduser()
        if path.is_file():
            paths.append(path)
        elif glob.has_magic(pattern):
            matches = [Path(m) for m in sorted(glob.glob(os.path.expanduser(pattern), recursive=True))]
            matches = [m for m in matches if m.is_file()]
            if not matches:
                die(f"no files match {pattern}")
            paths.extend(matches)
        elif path.is_dir():
            die(f"{pattern} is a folder - give files, or a pattern like \"{Path(pattern) / '*.mp4'}\"")
        else:
            die(f"file not found: {pattern}")
    unique: dict[str, Path] = {}
    for path in paths:
        unique.setdefault(os.path.normcase(str(path.resolve())), path)
    return list(unique.values())


def looks_like_folder(text: str) -> bool:
    return text.endswith(("/", "\\")) or Path(text).expanduser().is_dir()


def output_path(src: Path, ext: str, output: str | None, many: bool, action: str, natural: bool = False) -> Path:
    """Where a result goes. `natural` names it <stem>.<ext> (clip.gif) unless that would be the input itself;
    otherwise <stem>.<action>.<ext> (clip.trim.mp4)."""
    name = f"{src.stem}.{ext}" if natural and src.suffix.lower() != f".{ext}" else f"{src.stem}.{action}.{ext}"
    if output is None:
        return src.with_name(name)
    if looks_like_folder(output):
        return Path(output).expanduser() / name
    if many:
        die("with several input files, -o must be a folder (end it with a slash to create one)")
    target = Path(output).expanduser()
    return target if target.suffix else target.with_suffix(f".{ext}")


def same_file(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


def ask(question: str) -> bool:
    if not sys.stdin.isatty():
        die(f"{question} Add --yes to go ahead (kit can't ask in a non-interactive shell)")
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        print()
        die("no answer (input was closed) - add --yes to skip the question")


def may_write(dst: Path, sources: list[Path], args: argparse.Namespace) -> bool:
    if any(same_file(dst, src) for src in sources):
        die(f"{dst} is also an input - choose another name with -o")
    if dst.exists() and not args.dry_run and not args.yes:
        if dst.is_dir():
            error(f"{dst} is a folder")
            return False
        if not ask(f"{dst} already exists. Overwrite?"):
            print(style("  skipped", "yellow"))
            return False
    return True


@dataclass
class Outcome:
    src: Path
    ok: bool | None  # None = skipped
    detail: str = ""


def done_line(dst: Path, started: float, extra: str = "", size: bool = True) -> str:
    size_text = format_size(dst.stat().st_size) if size and dst.is_file() else ""
    parts = [p for p in (size_text, extra, f"{time.monotonic() - started:.1f}s") if p]
    return f"{style('done', 'bold', 'green')} {dst}  {style('  '.join(parts), 'dim')}"


def for_each(paths: list[Path], handle: Callable[[Path], Outcome]) -> int:
    many = len(paths) > 1
    outcomes: list[Outcome] = []
    for index, src in enumerate(paths, 1):
        if many:
            print(style(f"[{index}/{len(paths)}] {src}", "bold"))
        outcomes.append(handle(src))
        if many:
            print()
    if many:
        done = sum(1 for o in outcomes if o.ok)
        failed = [o for o in outcomes if o.ok is False]
        skipped = sum(1 for o in outcomes if o.ok is None)
        print(style("SUMMARY", "bold"))
        for outcome in outcomes:
            mark = {True: style("ok     ", "green"), False: style("failed ", "red"), None: style("skipped", "yellow")}[outcome.ok]
            print(f"  {mark}  {outcome.src}  {style(outcome.detail, 'dim')}")
        tail = [f"{done} done"] + ([f"{len(failed)} failed"] if failed else []) + ([f"{skipped} skipped"] if skipped else [])
        print(f"  {', '.join(tail)}")
    return 1 if any(o.ok is False for o in outcomes) else 0


def span(media: Media, start: float | None, end: float | None, length: float | None) -> tuple[float, float | None]:
    """(start, duration) from --from/--to/--duration, checked against the file's length."""
    start = start or 0.0
    total = media.duration
    if total and start >= total:
        raise ValueError(f"--from {format_duration(start)} is past the end ({format_duration(total)})")
    if end is not None:
        if end <= start:
            raise ValueError("--to must be later than --from")
        if total:
            end = min(end, total)
        return start, end - start
    if length is not None:
        if length <= 0:
            raise ValueError("--duration must be more than zero")
        return start, min(length, total - start) if total else length
    return start, (total - start) if total else None


# --- info ----------------------------------------------------------------------------

def stream_line(stream: dict) -> str:
    kind = stream.get("codec_type", "?")
    codec = stream.get("codec_name", "?")
    if stream.get("profile") and stream["profile"] not in ("unknown",):
        codec += f" ({stream['profile']})"
    parts = [f"#{stream.get('index', '?')}", f"{kind:<8}", codec]
    if kind == "video":
        if stream.get("disposition", {}).get("attached_pic"):
            parts.append("cover art")
        if stream.get("width"):
            parts.append(f"{stream['width']}x{stream.get('height')}")
        fps = frame_rate(stream)
        if fps and not stream.get("disposition", {}).get("attached_pic"):
            parts.append(f"{fps:.3f}".rstrip("0").rstrip(".") + " fps")
        if stream.get("pix_fmt"):
            parts.append(stream["pix_fmt"])
    elif kind == "audio":
        if stream.get("sample_rate"):
            parts.append(f"{stream['sample_rate']} Hz")
        layout = stream.get("channel_layout") or (f"{stream['channels']} ch" if stream.get("channels") else "")
        if layout:
            parts.append(layout)
    bitrate = format_bitrate(stream.get("bit_rate"))
    if bitrate:
        parts.append(bitrate)
    tags = stream.get("tags", {})
    language = tags.get("language")
    if language and language != "und":
        parts.append(f"[{language}]")
    if tags.get("title"):
        parts.append(f'"{tags["title"]}"')
    if stream.get("disposition", {}).get("default") and kind in ("audio", "subtitle"):
        parts.append("default")
    return "  ".join(parts)


def cmd_info(ff: FF, args: argparse.Namespace, settings: dict) -> int:
    paths = expand_inputs(args.inputs)
    if args.json:
        results, failed = [], False
        for path in paths:
            data = probe_raw(ff, path)
            failed |= data is None
            if data is not None:
                results.append(data)
        print(json.dumps(results[0] if len(paths) == 1 and results else results, indent=2, ensure_ascii=False))
        return 1 if failed else 0

    failed = False
    for index, path in enumerate(paths):
        media = probe(ff, path)
        if media is None:
            failed = True
            continue
        if index:
            print()
        fmt = media.format
        print(style(str(path), "bold"))
        rows = [
            ("container", fmt.get("format_long_name") or fmt.get("format_name", "?")),
            ("duration", format_duration(media.duration, 2) if media.duration else "unknown"),
            ("size", format_size(int(fmt.get("size", 0) or path.stat().st_size))),
            ("bitrate", format_bitrate(fmt.get("bit_rate")) or "unknown"),
        ]
        tags = {k.lower(): v for k, v in fmt.get("tags", {}).items()}
        for key in ("title", "artist", "album", "creation_time"):
            if tags.get(key):
                rows.append((key.replace("_", " "), tags[key]))
        for label, value in rows:
            print(f"  {style(label.ljust(13), 'dim')}{value}")
        print(f"  {style('streams', 'dim')}")
        for stream in media.streams:
            print(f"    {stream_line(stream)}")
    return 1 if failed else 0


# --- convert ---------------------------------------------------------------------------

def cmd_convert(ff: FF, args: argparse.Namespace, settings: dict) -> int:
    paths = expand_inputs(args.inputs)
    many = len(paths) > 1
    if args.to:
        ext = args.to.lower().lstrip(".")
    elif args.output and not looks_like_folder(args.output) and Path(args.output).suffix and not many:
        ext = Path(args.output).suffix.lower().lstrip(".")
    else:
        die("choose a format with --to (e.g. --to mp4), or give an output file like -o out.webm")
    if ext == "gif":
        if args.copy:
            die("--copy can't make a GIF - GIFs are always re-encoded")
        args.start = args.end = args.length = None
        args.fps, args.width = settings["gif_fps"], settings["gif_width"]
        return cmd_gif(ff, args, settings)
    fmt = FORMATS.get(ext)
    if fmt is None:
        warn(f"kit has no preset for .{ext}, so ffmpeg picks the codecs")

    def handle(src: Path) -> Outcome:
        media = probe(ff, src)
        if media is None:
            return Outcome(src, False, "unreadable")
        dst = output_path(src, ext, args.output, many, "convert", natural=True)
        if not may_write(dst, paths, args):
            return Outcome(src, None)
        vcodec, acodec = media.codec("video"), media.codec("audio")
        if fmt and fmt.audio_only and not acodec:
            error(f"{src} has no audio to put in a .{ext} file")
            return Outcome(src, False, "no audio")
        options = ["-i", str(src)]
        if args.copy:
            if fmt is None or fmt.copy_video is ANY:
                options += ["-map", "0", "-c", "copy"]
            elif fmt.audio_only:
                if not fits(fmt.copy_audio, acodec):
                    error(f"can't copy {acodec} audio into .{ext} - drop --copy to re-encode it")
                    return Outcome(src, False, f"{acodec} doesn't fit .{ext}")
                options += ["-vn", "-c:a", "copy", *fmt.extra]
            else:
                bad = [f"{c} {kind}" for kind, c, allowed in (("video", vcodec, fmt.copy_video), ("audio", acodec, fmt.copy_audio))
                       if not fits(allowed, c)]
                if bad:
                    error(f"can't copy {' and '.join(bad)} into .{ext} - drop --copy to re-encode")
                    return Outcome(src, False, f"{', '.join(bad)} doesn't fit .{ext}")
                options += ["-c", "copy", *fmt.extra]
                if vcodec == "hevc" and ext in ("mp4", "m4v", "mov"):
                    options += ["-tag:v", "hvc1"]  # so Apple players accept it
        else:
            options += encode_args(ext, keep_audio=acodec)
        started = time.monotonic()
        if not run_ffmpeg(ff, options + [str(dst)], duration=media.duration, output=dst, dry_run=args.dry_run):
            return Outcome(src, False)
        if args.dry_run:
            return Outcome(src, True, "dry run")
        print(done_line(dst, started))
        return Outcome(src, True, f"-> {dst.name}")

    return for_each(paths, handle)


# --- trim -----------------------------------------------------------------------------

def cmd_trim(ff: FF, args: argparse.Namespace, settings: dict) -> int:
    if args.start is None and args.end is None and args.length is None:
        die("say where to cut with --from, --to and/or --duration")
    paths = expand_inputs(args.inputs)
    many = len(paths) > 1
    if not args.exact:
        print(style("  Fast cut without re-encoding: the start snaps to the keyframe at or before --from. "
                    "Add --exact for a frame-accurate cut (slower).", "dim"))

    def handle(src: Path) -> Outcome:
        media = probe(ff, src)
        if media is None:
            return Outcome(src, False, "unreadable")
        try:
            start, length = span(media, args.start, args.end, args.length)
        except ValueError as exc:
            error(str(exc))
            return Outcome(src, False, str(exc))
        dst = output_path(src, src.suffix.lower().lstrip(".") or "mp4", args.output, many, "trim")
        if not may_write(dst, paths, args):
            return Outcome(src, None)
        options = ["-ss", f"{start:.3f}", "-i", str(src)]
        if length is not None:
            options += ["-t", f"{length:.3f}"]
        ext = dst.suffix.lower().lstrip(".")
        if args.exact:
            options += encode_args(ext)
        else:
            options += ["-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-c", "copy", "-avoid_negative_ts", "make_zero"]
            if ext in ("mp4", "m4v", "mov", "m4a"):
                options += list(FASTSTART)
        started = time.monotonic()
        if not run_ffmpeg(ff, options + [str(dst)], duration=length or 0, output=dst, dry_run=args.dry_run):
            return Outcome(src, False)
        if args.dry_run:
            return Outcome(src, True, "dry run")
        cut = f"{format_duration(start)} + {format_duration(length) if length is not None else 'rest'}"
        print(done_line(dst, started, cut))
        return Outcome(src, True, f"-> {dst.name}")

    return for_each(paths, handle)


# --- gif ------------------------------------------------------------------------------

def cmd_gif(ff: FF, args: argparse.Namespace, settings: dict) -> int:
    paths = expand_inputs(args.inputs)
    many = len(paths) > 1
    fps = args.fps if args.fps is not None else settings["gif_fps"]
    width = args.width if args.width is not None else settings["gif_width"]
    graph = f"fps={fps}"
    if width:
        graph += f",scale='min({width},iw)':-1:flags=lanczos"
    graph += ",split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"

    def handle(src: Path) -> Outcome:
        media = probe(ff, src)
        if media is None:
            return Outcome(src, False, "unreadable")
        if not media.video:
            error(f"{src} has no video")
            return Outcome(src, False, "no video")
        try:
            start, length = span(media, args.start, args.end, args.length)
        except ValueError as exc:
            error(str(exc))
            return Outcome(src, False, str(exc))
        if length is not None and length > 30:
            warn(f"that's a {format_duration(length, 0)} GIF - GIFs get big fast; pick a shorter part with --from/--to")
        dst = output_path(src, "gif", args.output, many, "gif", natural=True)
        if not may_write(dst, paths, args):
            return Outcome(src, None)
        options = (["-ss", f"{start:.3f}"] if start else []) + ["-i", str(src)]
        if length is not None:
            options += ["-t", f"{length:.3f}"]
        options += ["-filter_complex", graph, "-loop", "0", str(dst)]
        started = time.monotonic()
        if not run_ffmpeg(ff, options, duration=length or 0, output=dst, dry_run=args.dry_run):
            return Outcome(src, False)
        if args.dry_run:
            return Outcome(src, True, "dry run")
        print(done_line(dst, started, f"{fps} fps"))
        return Outcome(src, True, f"-> {dst.name}")

    return for_each(paths, handle)


# --- audio -----------------------------------------------------------------------------

def cmd_audio(ff: FF, args: argparse.Namespace, settings: dict) -> int:
    paths = expand_inputs(args.inputs)
    many = len(paths) > 1
    ext = args.format or settings["audio_format"]
    fmt = FORMATS[ext]

    def handle(src: Path) -> Outcome:
        media = probe(ff, src)
        if media is None:
            return Outcome(src, False, "unreadable")
        acodec = media.codec("audio")
        if not acodec:
            error(f"{src} has no audio")
            return Outcome(src, False, "no audio")
        dst = output_path(src, ext, args.output, many, "audio", natural=True)
        if not may_write(dst, paths, args):
            return Outcome(src, None)
        copy = fits(fmt.copy_audio, acodec)
        options = ["-i", str(src), "-vn", "-sn", "-map", "0:a:0"]
        options += ["-c:a", "copy", *fmt.extra] if copy else [*fmt.audio, *fmt.extra]
        started = time.monotonic()
        if not run_ffmpeg(ff, options + [str(dst)], duration=media.duration, output=dst, dry_run=args.dry_run):
            return Outcome(src, False)
        if args.dry_run:
            return Outcome(src, True, "dry run")
        note = f"{acodec} copied as-is" if copy else f"{acodec} -> {ext}"
        print(done_line(dst, started, note))
        return Outcome(src, True, f"-> {dst.name}")

    return for_each(paths, handle)


# --- compress --------------------------------------------------------------------------

def video_codec_args(codec: str, preset: str) -> list[str]:
    if codec == "h265":
        return ["-c:v", "libx265", "-preset", preset, "-tag:v", "hvc1"]
    return ["-c:v", "libx264", "-preset", preset, "-pix_fmt", "yuv420p"]


def cmd_compress(ff: FF, args: argparse.Namespace, settings: dict) -> int:
    paths = expand_inputs(args.inputs)
    many = len(paths) > 1
    crf = args.crf if args.crf is not None else settings["crf"]

    def handle(src: Path) -> Outcome:
        media = probe(ff, src)
        if media is None:
            return Outcome(src, False, "unreadable")
        if not media.video:
            error(f"{src} has no video to compress (for audio, use: kit media convert --to mp3)")
            return Outcome(src, False, "no video")
        before = src.stat().st_size
        if args.size and before <= args.size:
            print(f"  already {format_size(before)}, within {format_size(args.size)} - nothing to do")
            return Outcome(src, None, "already small enough")
        suffix = src.suffix.lower().lstrip(".")
        ext = suffix if suffix in ("mp4", "mkv", "mov", "m4v") else "mp4"
        dst = output_path(src, ext, args.output, many, "compressed")
        if not may_write(dst, paths, args):
            return Outcome(src, None)
        audio = media.audio
        extra = list(FASTSTART) if ext in ("mp4", "mov", "m4v") else []
        started = time.monotonic()

        if not args.size:
            try:
                audio_bitrate = int(audio.get("bit_rate", 0)) if audio else 0
            except ValueError:
                audio_bitrate = 0
            if audio and audio.get("codec_name") == "aac" and 0 < audio_bitrate <= 140_000:
                audio_args = ["-c:a", "copy"]
            else:
                audio_args = ["-c:a", "aac", "-b:a", "128k"]
            options = ["-i", str(src), *video_codec_args(args.codec, args.preset), "-crf", str(crf)]
            if args.codec == "h265":
                options += ["-x265-params", "log-level=error"]
            options += [*audio_args, *extra, str(dst)]
            if not run_ffmpeg(ff, options, duration=media.duration, output=dst, dry_run=args.dry_run):
                return Outcome(src, False)
        else:
            if not media.duration:
                error(f"can't tell how long {src} is, so --size can't work out a bitrate - use --crf")
                return Outcome(src, False, "unknown duration")
            total = args.size * 8 / media.duration
            audio_bps = (128_000 if total >= 600_000 else 64_000) if audio else 0
            video_bps = int(total * 0.96 - audio_bps)  # leave room for the container
            if video_bps < 30_000:
                need = format_size((30_000 + audio_bps) / 0.96 * media.duration / 8)
                error(f"{format_size(args.size)} is too small for {format_duration(media.duration, 0)} of video - "
                      f"it needs at least about {need}")
                return Outcome(src, False, "target too small")
            print(style(f"  target {format_size(args.size)}: video {video_bps // 1000} kb/s"
                        + (f" + audio {audio_bps // 1000} kb/s" if audio_bps else "") + ", two passes", "dim"))
            with tempfile.TemporaryDirectory(prefix="kit-media-") as tmp:
                # x265 parses ':' as a separator, so its stats file is named relative to the temp folder it
                # runs in - which means the input and output need full paths
                cwd = tmp if args.codec == "h265" else None
                source, target = (str(src.resolve()), str(dst.resolve())) if cwd else (str(src), str(dst))
                base = [*video_codec_args(args.codec, args.preset), "-b:v", str(video_bps)]
                passes = []
                for number in (1, 2):
                    if args.codec == "h265":
                        pass_args = ["-x265-params", f"pass={number}:stats=x265.log:log-level=error"]
                    else:
                        pass_args = ["-pass", str(number), "-passlogfile", os.path.join(tmp, "pass")]
                    passes.append(pass_args)
                first = ["-i", source, *base, *passes[0], "-an", "-f", "null", NULL_DEVICE]
                audio_args = ["-c:a", "aac", "-b:a", str(audio_bps)] if audio_bps else ["-an"]
                second = ["-i", source, *base, *passes[1], *audio_args, *extra, target]
                if not run_ffmpeg(ff, first, duration=media.duration, output=None, dry_run=args.dry_run,
                                  label="pass 1/2 ", cwd=cwd):
                    return Outcome(src, False)
                if not run_ffmpeg(ff, second, duration=media.duration, output=dst, dry_run=args.dry_run,
                                  label="pass 2/2 ", cwd=cwd):
                    return Outcome(src, False)

        if args.dry_run:
            return Outcome(src, True, "dry run")
        after = dst.stat().st_size
        change = (1 - after / before) * 100 if before else 0
        summary = f"{format_size(before)} -> {format_size(after)}"
        summary += f" ({change:.0f}% smaller)" if change >= 0 else f" ({-change:.0f}% bigger)"
        print(done_line(dst, started, summary, size=False))
        if after > before:
            warn("the result is bigger than the original - try a higher --crf (e.g. --crf 32) or --size")
        elif args.size and after > args.size:
            warn(f"came out at {format_size(after)}, a little over {format_size(args.size)} - "
                 "try a slightly smaller --size")
        return Outcome(src, True, summary)

    return for_each(paths, handle)


# --- resize ------------------------------------------------------------------------------

def even(value: int) -> int:
    return max(2, value - value % 2)


def cmd_resize(ff: FF, args: argparse.Namespace, settings: dict) -> int:
    if args.scale is None and args.width is None and args.height is None:
        die("give a new size with --width, --height or --scale")
    if args.scale is not None and (args.width or args.height):
        die("use either --scale or --width/--height, not both")
    paths = expand_inputs(args.inputs)
    many = len(paths) > 1

    def handle(src: Path) -> Outcome:
        media = probe(ff, src)
        if media is None:
            return Outcome(src, False, "unreadable")
        video = media.video
        if not video:
            error(f"{src} has no video or image to resize")
            return Outcome(src, False, "no video")
        dst = output_path(src, src.suffix.lower().lstrip(".") or "mp4", args.output, many, "resized")
        if not may_write(dst, paths, args):
            return Outcome(src, None)
        ext = dst.suffix.lower().lstrip(".")
        is_video = ext in FORMATS and not FORMATS[ext].audio_only
        fix = even if is_video else (lambda v: v)  # most video encoders need even sizes
        if args.scale is not None:
            if is_video:
                scale = f"scale=trunc(iw*{args.scale:g}/2)*2:trunc(ih*{args.scale:g}/2)*2"
            else:
                scale = f"scale=trunc(iw*{args.scale:g}):trunc(ih*{args.scale:g})"
        elif args.width and args.height:
            scale = f"scale={fix(args.width)}:{fix(args.height)}:force_original_aspect_ratio=decrease"
            if is_video:
                scale += ":force_divisible_by=2"
        elif args.width:
            scale = f"scale={fix(args.width)}:{-2 if is_video else -1}"
        else:
            scale = f"scale={-2 if is_video else -1}:{fix(args.height)}"
        options = ["-i", str(src), "-vf", scale + ":flags=lanczos", *encode_args(ext, keep_audio=media.codec("audio")), str(dst)]
        started = time.monotonic()
        if not run_ffmpeg(ff, options, duration=media.duration, output=dst, dry_run=args.dry_run):
            return Outcome(src, False)
        if args.dry_run:
            return Outcome(src, True, "dry run")
        result = probe(ff, dst)
        new = result.video if result else None
        sizes = f"{video.get('width')}x{video.get('height')} -> {new.get('width')}x{new.get('height')}" if new else ""
        print(done_line(dst, started, sizes))
        return Outcome(src, True, sizes)

    return for_each(paths, handle)


# --- mute --------------------------------------------------------------------------------

def cmd_mute(ff: FF, args: argparse.Namespace, settings: dict) -> int:
    paths = expand_inputs(args.inputs)
    many = len(paths) > 1

    def handle(src: Path) -> Outcome:
        media = probe(ff, src)
        if media is None:
            return Outcome(src, False, "unreadable")
        if not media.video:
            error(f"{src} has no video - muting it would leave nothing")
            return Outcome(src, False, "no video")
        dst = output_path(src, src.suffix.lower().lstrip(".") or "mp4", args.output, many, "muted")
        if not may_write(dst, paths, args):
            return Outcome(src, None)
        ext = dst.suffix.lower().lstrip(".")
        options = ["-i", str(src), "-map", "0", "-map", "-0:a", "-map", "-0:d?", "-c", "copy"]
        if ext in ("mp4", "m4v", "mov"):
            options += list(FASTSTART)
        started = time.monotonic()
        if not run_ffmpeg(ff, options + [str(dst)], duration=media.duration, output=dst, dry_run=args.dry_run):
            return Outcome(src, False)
        if args.dry_run:
            return Outcome(src, True, "dry run")
        print(done_line(dst, started, "audio removed"))
        return Outcome(src, True, f"-> {dst.name}")

    return for_each(paths, handle)


# --- concat --------------------------------------------------------------------------------

def signature(media: Media) -> dict[str, object]:
    video, audio = media.video or {}, media.audio or {}
    return {
        "container": media.path.suffix.lower(),
        "video codec": video.get("codec_name"),
        "resolution": (video.get("width"), video.get("height")),
        "pixel format": video.get("pix_fmt"),
        "frame rate": round(frame_rate(video), 2) if video else None,
        "audio codec": audio.get("codec_name"),
        "sample rate": audio.get("sample_rate"),
        "channels": audio.get("channels"),
    }


def concat_list_entry(path: Path) -> str:
    text = str(path.resolve()).replace("\\", "/").replace("'", "'\\''")
    return f"file '{text}'"


def cmd_concat(ff: FF, args: argparse.Namespace, settings: dict) -> int:
    paths = expand_inputs(args.inputs)
    if len(paths) < 2:
        die("give at least two files to join")
    medias = []
    for path in paths:
        media = probe(ff, path)
        if media is None:
            return 1
        medias.append(media)
    first = paths[0]
    ext = first.suffix.lower().lstrip(".") or "mp4"
    if args.output and not looks_like_folder(args.output):
        dst = Path(args.output).expanduser()
        if not dst.suffix:
            dst = dst.with_suffix(f".{ext}")
    else:
        dst = output_path(first, ext, args.output, False, "joined")
    if not may_write(dst, paths, args):
        return 1
    out_ext = dst.suffix.lower().lstrip(".")
    total = sum(m.duration for m in medias)

    signatures = [signature(m) for m in medias]
    differences = [key for key in signatures[0] if any(s[key] != signatures[0][key] for s in signatures[1:])]
    if out_ext != ext:
        differences.append("output container")
    started = time.monotonic()

    if not differences and not args.reencode:
        print(style("  The files match, so they're joined without re-encoding (fast).", "dim"))
        with tempfile.TemporaryDirectory(prefix="kit-media-") as tmp:
            listing = Path(tmp) / "files.txt"
            listing.write_text("\n".join(concat_list_entry(p) for p in paths) + "\n", encoding="utf-8")
            options = ["-f", "concat", "-safe", "0", "-i", str(listing), "-map", "0:v?", "-map", "0:a?", "-c", "copy"]
            if out_ext in ("mp4", "m4v", "mov", "m4a"):
                options += list(FASTSTART)
            if not run_ffmpeg(ff, options + [str(dst)], duration=total, output=dst, dry_run=args.dry_run):
                return 1
    else:
        reason = ", ".join(differences) if differences else "--reencode"
        print(style(f"  The files differ ({reason}), so they're re-encoded to join them (slower).", "dim"))
        fmt = FORMATS.get(out_ext, FORMATS["mp4"])
        want_video = not fmt.audio_only and any(m.video for m in medias)
        want_audio = any(m.audio for m in medias)
        if not want_video and not want_audio:
            die("nothing to join: the files have no audio or video")
        if any(not m.duration for m in medias) and (want_video and not all(m.video for m in medias)
                                                   or want_audio and not all(m.audio for m in medias)):
            die("can't fill in missing audio/video for a file of unknown length")
        base = next((m.video for m in medias if m.video), None)
        width, height = (even(int(base["width"])), even(int(base["height"]))) if base else (0, 0)
        fps = round(frame_rate(base), 3) if base else 30
        fps = fps or 30

        inputs: list[str] = []
        filters: list[str] = []
        pads: list[str] = []
        count = len(medias)
        extra_index = count
        for media in medias:
            inputs += ["-i", str(media.path)]
        for index, media in enumerate(medias):
            if want_video:
                source = f"{index}:v:0"
                if not media.video:
                    inputs += ["-f", "lavfi", "-t", f"{media.duration:.3f}", "-i", f"color=c=black:s={width}x{height}:r={fps}"]
                    source, extra_index = f"{extra_index}:v", extra_index + 1
                filters.append(f"[{source}]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                               f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v{index}]")
                pads.append(f"[v{index}]")
            if want_audio:
                source = f"{index}:a:0"
                if not media.audio:
                    inputs += ["-f", "lavfi", "-t", f"{media.duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
                    source, extra_index = f"{extra_index}:a", extra_index + 1
                filters.append(f"[{source}]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{index}]")
                pads.append(f"[a{index}]")
        filters.append(f"{''.join(pads)}concat=n={count}:v={int(want_video)}:a={int(want_audio)}"
                       + ("[v]" if want_video else "") + ("[a]" if want_audio else ""))
        options = [*inputs, "-filter_complex", ";".join(filters)]
        options += (["-map", "[v]"] if want_video else []) + (["-map", "[a]"] if want_audio else [])
        codecs = [*fmt.video, *fmt.audio, *fmt.extra] if want_video else ["-vn", *fmt.audio, *fmt.extra]
        if out_ext not in FORMATS:
            warn(f"kit has no preset for .{out_ext}, so it uses H.264/AAC")
        options += codecs + [str(dst)]
        if not run_ffmpeg(ff, options, duration=total, output=dst, dry_run=args.dry_run):
            return 1

    if not args.dry_run:
        print(done_line(dst, started, f"{len(paths)} files, {format_duration(total, 0)}"))
    return 0


# --- thumb and frames -----------------------------------------------------------------------

def image_args(ext: str) -> list[str]:
    return ["-q:v", "2"] if ext in ("jpg", "jpeg") else (["-quality", "90"] if ext == "webp" else [])


def cmd_thumb(ff: FF, args: argparse.Namespace, settings: dict) -> int:
    paths = expand_inputs(args.inputs)
    many = len(paths) > 1

    def handle(src: Path) -> Outcome:
        media = probe(ff, src)
        if media is None:
            return Outcome(src, False, "unreadable")
        if not media.video:
            error(f"{src} has no video")
            return Outcome(src, False, "no video")
        at = args.at if args.at is not None else media.duration * 0.1
        if media.duration and at >= media.duration:
            error(f"--at {format_duration(at)} is past the end ({format_duration(media.duration)})")
            return Outcome(src, False, "past the end")
        dst = output_path(src, args.format, args.output, many, "thumb", natural=True)
        if not may_write(dst, paths, args):
            return Outcome(src, None)
        ext = dst.suffix.lower().lstrip(".")
        options = ["-ss", f"{at:.3f}", "-i", str(src), "-frames:v", "1", "-update", "1", *image_args(ext)]
        if args.width:
            options += ["-vf", f"scale='min({args.width},iw)':-1:flags=lanczos"]
        started = time.monotonic()
        if not run_ffmpeg(ff, options + [str(dst)], duration=0, output=dst, dry_run=args.dry_run):
            return Outcome(src, False)
        if args.dry_run:
            return Outcome(src, True, "dry run")
        if not dst.is_file():
            error("ffmpeg didn't write a frame - try an earlier --at")
            return Outcome(src, False, "no frame")
        print(done_line(dst, started, f"frame at {format_duration(at)}"))
        return Outcome(src, True, f"-> {dst.name}")

    return for_each(paths, handle)


def cmd_frames(ff: FF, args: argparse.Namespace, settings: dict) -> int:
    if args.every <= 0:
        die("--every must be more than zero")
    paths = expand_inputs(args.inputs)
    many = len(paths) > 1

    def handle(src: Path) -> Outcome:
        media = probe(ff, src)
        if media is None:
            return Outcome(src, False, "unreadable")
        if not media.video:
            error(f"{src} has no video")
            return Outcome(src, False, "no video")
        if args.output:
            folder = Path(args.output).expanduser()
            if many:
                folder = folder / f"{src.stem}-frames"
        else:
            folder = src.with_name(f"{src.stem}-frames")
        existing = sorted(folder.glob(f"{glob.escape(src.stem)}-*.{args.format}")) if folder.is_dir() else []
        if existing and not args.dry_run and not args.yes:
            if not ask(f"{folder} already has {len(existing)} frame image(s) that may be overwritten. Continue?"):
                print(style("  skipped", "yellow"))
                return Outcome(src, None)
        before = {p.name for p in existing}
        started_wall = time.time()

        def cleanup() -> None:
            for image in folder.glob(f"{glob.escape(src.stem)}-*.{args.format}"):
                try:
                    if image.name not in before or image.stat().st_mtime >= started_wall - 1:
                        image.unlink()
                except OSError:
                    pass

        if not args.dry_run:
            folder.mkdir(parents=True, exist_ok=True)
        pattern = folder / f"{src.stem.replace('%', '%%')}-%04d.{args.format}"
        graph = f"fps={1 / args.every:.6g}"
        if args.width:
            graph += f",scale='min({args.width},iw)':-1:flags=lanczos"
        options = ["-i", str(src), "-vf", graph, *image_args(args.format), str(pattern)]
        started = time.monotonic()
        if not run_ffmpeg(ff, options, duration=media.duration, output=None, dry_run=args.dry_run, cleanup=cleanup):
            return Outcome(src, False)
        if args.dry_run:
            return Outcome(src, True, "dry run")
        written = [p for p in folder.glob(f"{glob.escape(src.stem)}-*.{args.format}") if p.stat().st_mtime >= started_wall - 1]
        detail = f"{len(written)} images, one every {format_duration(args.every)}"
        print(f"{style('done', 'bold', 'green')} {folder}  {style(f'{detail}  {time.monotonic() - started:.1f}s', 'dim')}")
        return Outcome(src, True, detail)

    return for_each(paths, handle)


# --- main -----------------------------------------------------------------------------------

EPILOG = """examples:
  kit media info clip.mp4
  kit media convert clip.mov --to mp4
  kit media trim clip.mp4 --from 1:05 --to 1:30
  kit media gif clip.mp4 --from 10 --duration 4
  kit media audio "lectures/*.mp4" --format m4a
  kit media compress clip.mp4 --size 25MB

run 'kit media <command> -h' for a command's options, or 'kit help media' for the full docs"""


def build_parser(settings: dict) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kit media", description="Friendly shortcuts for everyday ffmpeg jobs.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ffmpeg", metavar="PATH", help="ffmpeg to use (default: the media.ffmpeg setting, then PATH)")
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-o", "--output", metavar="PATH",
                        help="output file, or a folder (default: next to the input)")
    common.add_argument("-y", "--yes", action="store_true", help="overwrite existing files without asking")
    common.add_argument("--dry-run", action="store_true", help="print the ffmpeg command without running it")
    inputs = argparse.ArgumentParser(add_help=False)
    inputs.add_argument("inputs", nargs="+", metavar="FILE", help="input files; wildcards like *.mp4 work")
    span_args = argparse.ArgumentParser(add_help=False)
    span_args.add_argument("--from", dest="start", type=parse_time, metavar="TIME", help="start time (90, 1:30, 0:01:30.5)")
    ends = span_args.add_mutually_exclusive_group()
    ends.add_argument("--to", dest="end", type=parse_time, metavar="TIME", help="end time")
    ends.add_argument("--duration", "-t", dest="length", type=parse_time, metavar="TIME", help="length to keep")

    def add(name: str, help_text: str, func, parents: list) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text, description=help_text, parents=parents)
        sub.set_defaults(func=func)
        return sub

    sub = commands.add_parser("info", help="show a file's format, length and streams",
                              description="Show a file's format, length and streams.", parents=[inputs])
    sub.add_argument("--json", action="store_true", help="print ffprobe's raw JSON")
    sub.set_defaults(func=cmd_info)

    sub = add("convert", "convert to another format", cmd_convert, [inputs, common])
    sub.add_argument("--to", metavar="FORMAT", type=str.lower,
                     help=f"target format: {', '.join(CONVERT_TARGETS)} (or any extension ffmpeg knows)")
    sub.add_argument("--copy", action="store_true", help="change the container only, without re-encoding (fast, lossless)")

    sub = add("trim", "cut out a part (fast, no re-encoding)", cmd_trim, [inputs, span_args, common])
    sub.add_argument("--exact", action="store_true", help="re-encode for a frame-accurate cut (slower)")

    sub = add("gif", "make a good-looking GIF", cmd_gif, [inputs, span_args, common])
    sub.add_argument("--fps", type=positive_int, help=f"frames per second (default: {settings['gif_fps']})")
    sub.add_argument("--width", type=int, help=f"maximum width in pixels, 0 = original (default: {settings['gif_width']})")

    sub = add("audio", "extract the audio track", cmd_audio, [inputs, common])
    sub.add_argument("--format", "-f", choices=AUDIO_FORMATS, help=f"audio format (default: {settings['audio_format']})")

    sub = add("compress", "make a video smaller", cmd_compress, [inputs, common])
    quality = sub.add_mutually_exclusive_group()
    quality.add_argument("--crf", type=crf_value, metavar="N",
                         help=f"quality 0-51, higher = smaller (default: {settings['crf']})")
    quality.add_argument("--size", type=parse_size, metavar="SIZE", help="aim for a file size, e.g. 25MB (two passes)")
    sub.add_argument("--preset", choices=PRESETS, default="medium", help="encoder speed: slower = smaller (default: medium)")
    sub.add_argument("--codec", choices=["h264", "h265"], default="h264",
                     help="h264 plays everywhere; h265 is smaller but less widely supported (default: h264)")

    sub = add("resize", "change the resolution, keeping the shape", cmd_resize, [inputs, common])
    sub.add_argument("--width", "-W", type=positive_int, help="new width in pixels")
    sub.add_argument("--height", "-H", type=positive_int, help="new height in pixels")
    sub.add_argument("--scale", type=parse_scale, help="scale by a factor, e.g. 50%% or 0.5")

    add("mute", "remove the audio (no re-encoding)", cmd_mute, [inputs, common])

    sub = add("concat", "join files end to end", cmd_concat, [inputs, common])
    sub.add_argument("--reencode", action="store_true", help="always re-encode, even when the files match")

    sub = add("thumb", "save one frame as an image", cmd_thumb, [inputs, common])
    sub.add_argument("--at", type=parse_time, metavar="TIME", help="time of the frame (default: 10%% in)")
    sub.add_argument("--format", "-f", choices=IMAGE_FORMATS, default="jpg", help="image format (default: jpg)")
    sub.add_argument("--width", type=positive_int, help="maximum width in pixels")

    sub = add("frames", "save a frame every so often into a folder", cmd_frames, [inputs, common])
    sub.add_argument("--every", type=parse_time, default=1.0, metavar="TIME", help="time between frames (default: 1s)")
    sub.add_argument("--format", "-f", choices=IMAGE_FORMATS, default="jpg", help="image format (default: jpg)")
    sub.add_argument("--width", type=positive_int, help="maximum width in pixels")
    return parser


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    settings = tool_settings()
    parser = build_parser(settings)
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 0
    if args.command == "info":
        args.dry_run = False
    ff = find_ffmpeg(args.ffmpeg or settings["ffmpeg"])
    install_signal_handlers()
    try:
        return args.func(ff, args, settings)
    except KeyboardInterrupt:
        print(style("stopped", "yellow"))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
