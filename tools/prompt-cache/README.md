# prompt-cache

Write, find and fill reusable AI prompts in seconds, without managing files.

Most prompting is assembly work: pasting the same standing context, the same
instructions and one new piece of text into a chat window, over and over. This keeps
the fixed parts, pulls the changing part off your clipboard, and hands you the finished
prompt ready to paste.

## Usage

```
kit prompt-cache          # open it
kit pc                    # same thing
kit prompt-cache --where  # print where the database lives
```

Type to search, `Enter` to copy. Type something that doesn't exist and `Enter` writes it.
A prompt with `{{blanks}}` opens a fill form instead of copying, with `{{x: clipboard}}`
fields already filled in.

## Settings

```
kit config prompt-cache.data "D:/prompts"
```

Moves the database somewhere other than its default. Leave it empty to use the app's own
location (`%APPDATA%\prompt-cache` on Windows, `~/.local/share/prompt-cache` on Linux).

## Installing it

prompt-cache is a **separate project** with its own dependencies — kit launches it but
never imports it, and kit works fine without it.

```
git clone https://github.com/Blankscreen-exe/prompt-cache
cd prompt-cache
uv tool install .
```

Working on it instead of using a release? Point `PROMPT_CACHE_HOME` at your checkout and
kit runs it through `uv run` from there.
