# media

Convert, trim, compress and resize videos, make GIFs and pull out audio, with short ffmpeg commands.

## Usage

```
kit media info FILE... [--json]
kit media convert FILE... --to FORMAT [--copy]
kit media trim FILE... --from TIME [--to TIME | --duration TIME] [--exact]
kit media gif FILE... [--from TIME] [--to TIME | --duration TIME] [--fps N] [--width PX]
kit media audio FILE... [--format mp3|m4a|wav|flac|opus]
kit media compress FILE... [--crf N | --size SIZE] [--preset NAME] [--codec h264|h265]
kit media resize FILE... --width PX | --height PX | --scale 50%
kit media mute FILE...
kit media concat FILE FILE... [-o OUT] [--reencode]
kit media thumb FILE... [--at TIME] [--format jpg|png|webp] [--width PX]
kit media frames FILE... [--every TIME] [--format jpg|png|webp] [--width PX]
```

Every command that writes files also takes:

- `-o PATH`: the output file, or a folder. A path ending in `/` or `\` is created as a folder.
- `-y`: overwrite existing files without asking. Without it, kit asks first.
- `--dry-run`: only print the ffmpeg command.

kit prints each ffmpeg command before running it, so you can see what it does and reuse the command.

**Needs ffmpeg.** Install it with `winget install Gyan.FFmpeg`, `sudo apt install ffmpeg`, `sudo dnf install ffmpeg`
(the full build from RPM Fusion includes H.264), `sudo pacman -S ffmpeg` or `brew install ffmpeg`. kit looks on your
PATH and in the usual winget, scoop, Chocolatey and `C:\ffmpeg` folders. To use a different copy, set `media.ffmpeg`
or pass `--ffmpeg`.

### Commands

- `info`: the container, length, size, bitrate and every stream (codec, resolution, frame rate, sample rate,
  channels, language). `--json` prints ffprobe's full output.
- `convert`: changes the format. Choose it with `--to`, or with the extension in `-o out.webm`.
  - kit picks sensible codecs: H.264 + AAC for mp4/mov/mkv, VP9 + Opus for webm, and LAME for mp3.
  - Audio is copied unchanged when the new container accepts it.
  - `--copy` swaps the container without re-encoding anything, which is fast and lossless. It stops with a message
    if the codecs don't fit the new container.
  - `--to gif` works the same as the `gif` command.
- `trim`: keeps the part between `--from` and `--to` (or `--duration`).
  - By default it doesn't re-encode, so it's instant and lossless. The catch is that the cut starts at the keyframe
    at or before `--from`, so the clip can begin a little early.
  - `--exact` re-encodes for a frame-accurate cut.
- `gif`: a good-quality GIF, using a colour palette built from the clip itself. Keep GIFs short with
  `--from`/`--to`; kit warns about anything over 30 seconds. The width is capped at `media.gif_width`, and a video
  narrower than that keeps its size.
- `audio`: saves the first audio track. The track is copied as-is when it already suits the format (aac → m4a,
  opus → opus, and so on), and converted otherwise.
- `compress`: makes a video smaller with H.264, or with H.265 if you pass `--codec h265`.
  - The default is `--crf` quality mode. Higher numbers give smaller files: 23 is near-transparent and 28 (the
    default) is a good everyday size.
  - `--size 25MB` aims for a file size instead, using a two-pass encode. It's handy for upload limits. Sizes are
    in binary units (1 MB = 1024 KB), like Explorer and `ls -h` show them, and the result usually lands just under
    the target.
  - kit shows the size before and after. Files already under `--size` are skipped.
  - The result is .mp4 unless the input is .mkv or .mov.
- `resize`: `--width` or `--height` keeps the aspect ratio. With both, the video fits inside that box. `--scale 50%`
  resizes by a factor. Video sizes are rounded to even numbers, which encoders need. Audio is copied unchanged.
  This also works on images and GIFs.
- `mute`: drops every audio track without re-encoding the video.
- `concat`: joins files end to end.
  - When the files have the same container, codecs, resolution, frame rate and audio format, they're joined
    without re-encoding, which is fast.
  - Otherwise kit tells you what differs and re-encodes. Everything is scaled and letterboxed to the first video's
    size and frame rate, and silence fills in for files that have no audio.
  - `--reencode` always re-encodes.
- `thumb`: saves one frame as an image. It uses `--at`, or a point 10% into the video by default.
- `frames`: saves one image every `--every` seconds (default 1s) into a `<name>-frames` folder, or into the folder
  given with `-o`.

**Times** can be `90`, `1:30`, `00:01:30.5`, `45s`, `1.5m` or `2h`.

**Output names** go next to the input unless you pass `-o`:

- Commands that change the format use the new extension: `clip.gif`, `clip.mp3`, `clip.jpg`.
- Commands that keep the format add the action to the name: `clip.trim.mp4`, `clip.compressed.mp4`,
  `clip.resized.mp4`, `clip.muted.mp4` and `clip.joined.mp4`.
- If a new name would be the same as the input, kit inserts the action too, e.g. `song.audio.mp3`.

**Several files and wildcards**

- Commands that take `FILE...` accept several files and wildcards like `"clips/*.mov"`. kit expands the wildcards
  itself, so they work in PowerShell and cmd too.
- With several inputs, `-o` must be a folder.
- kit prints a summary at the end and exits with 1 if any file failed.

**Progress and stopping**

- In a terminal, a progress bar shows the percentage, encoding speed and time left. In `kit hub` you just get
  start and done lines.
- Ctrl+C stops ffmpeg and deletes the half-written file.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `media.ffmpeg` | *(found automatically)* | ffmpeg executable, or the folder it's in. `$KIT_FFMPEG` overrides it. ffprobe must be next to it. |
| `media.crf` | `28` | Quality `compress` uses without `--crf`/`--size` (0-51, lower is better and bigger) |
| `media.gif_fps` | `12` | Frames per second for `gif` |
| `media.gif_width` | `480` | Maximum GIF width in pixels (`0` keeps the video's width) |
| `media.audio_format` | `mp3` | Format `audio` saves without `--format`: mp3, m4a, wav, flac or opus |

```
kit config set media.crf 24
kit config set media.ffmpeg "C:\Tools\ffmpeg\bin"
```

## Examples

```
kit media info holiday.mov
kit media convert holiday.mov --to mp4               # holiday.mp4
kit media convert recording.mkv --to mp4 --copy      # instant remux, no quality loss
kit media trim talk.mp4 --from 12:30 --to 15:00      # talk.trim.mp4
kit media trim talk.mp4 --from 1:02.5 -t 10 --exact
kit media gif screen.mp4 --from 3 --duration 5 --width 640
kit media audio "lectures/*.mp4" --format m4a -o audio/
kit media compress screen.mp4 --size 25MB            # fits a 25 MB upload limit
kit media compress "*.mp4" --crf 30 -o small/
kit media resize clip.mp4 --height 720
kit media mute clip.mp4
kit media concat part1.mp4 part2.mp4 part3.mp4 -o whole.mp4
kit media thumb clip.mp4 --at 0:42 -o cover.jpg
kit media frames clip.mp4 --every 5s --format png
kit ff compress clip.mp4 --dry-run                   # just show the ffmpeg command
```
