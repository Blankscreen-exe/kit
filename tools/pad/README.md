# pad

A plain scratch pad in the terminal that saves as you type, for drafting messages and parking links.

## Usage

```
kit pad [NAME]                 # open a pad (default: scratch)
kit pad --list                 # your pads, newest first
kit pad [NAME] --print         # print the text (good for pipes)
kit pad [NAME] --add [TEXT]    # add a line at the end; reads a pipe when TEXT is - or left out
kit pad [NAME] --copy          # copy the whole pad to the clipboard
kit pad [NAME] --clear         # empty it (the old text is kept, see ctrl+r)
kit pad [NAME] --path          # where the file lives
```

The pad fills the whole terminal and resizes with it. The bottom edge shows lines, words and characters
and whether everything is saved. Text is saved about a second after you stop typing, and again when you quit,
so closing the window never loses anything.

| Key | What it does |
|---|---|
| `ctrl+c` | Copy the selection, or the whole pad when nothing is selected |
| `ctrl+l` | Copy the current line (handy for a link) |
| `ctrl+o` | Open the link under the cursor, or the nearest one on the line, in your browser |
| `ctrl+n` | Clear the pad |
| `ctrl+r` | Bring back the text you cleared (press again to swap back) |
| `ctrl+s` | Save now |
| `ctrl+q` | Quit |
| `ctrl+a` | Select everything |
| `ctrl+x` | Cut the selection, or the current line |
| `ctrl+v` | Paste from the system clipboard (right-click and the terminal's own paste work too) |
| `ctrl+z` / `ctrl+y` | Undo / redo |

Shift with the arrow keys, or dragging with the mouse, selects text.

- Pads are plain UTF-8 text files in `%APPDATA%\kit\pad\` on Windows and `~/.local/share/kit/pad/` on
  Linux (`$XDG_DATA_HOME/kit/pad`). Set `KIT_PAD_DIR` to keep them somewhere else.
- Clearing keeps the old text in `<name>.prev.txt`, so there's no "are you sure?" question.
- Copying uses the system clipboard (Linux: needs `wl-clipboard`, `xclip` or `xsel`). Without one, for
  example over SSH, the copy goes through the terminal instead, which works in most modern terminals.
- If the file changes while the pad is open, for example through `kit pad --add` in another terminal,
  the pad reloads it.
- Names can use letters, digits, `.`, `_` and `-`.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `pad.default` | `scratch` | Pad that opens when you don't give a name |
| `pad.autosave` | `true` | Save as you type. When off, `ctrl+s` saves and quitting asks about unsaved text |
| `pad.wrap` | `true` | Wrap long lines to the window width |
| `pad.line_numbers` | `false` | Show line numbers |

```
kit config set pad.line_numbers true
```

## Examples

```
kit pad                                  # the scratch pad
kit pad reply                            # a separate pad called "reply"
kit pad links --add https://example.com  # park a link without opening the pad
git remote get-url origin | kit pad links --add
kit pad reply --copy                     # copy the finished draft
kit pad links --print | grep github
```
