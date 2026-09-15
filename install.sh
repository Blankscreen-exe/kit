#!/usr/bin/env bash
# Hooks kit into Linux/macOS so the 'kit' command works in every terminal.
#
# Changes made (each safe to repeat):
#   1. Writes a two-line 'kit' launcher into ~/.local/bin (or $XDG_BIN_HOME).
#   2. Adds a commented, clearly marked block to ~/.bashrc (and ~/.zshrc if present) that
#      puts that folder on PATH, exports KIT_HOME and loads bash tab completion.
#      Each file is backed up next to itself as <file>.kit-backup first.
#
# Usage: ./install.sh [--dry-run] [--uninstall]
set -euo pipefail

KIT_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
LAUNCHER="$BIN_DIR/kit"
LAUNCHER_MARKER='# kit launcher - written by install.sh'
BEGIN_MARKER='# >>> kit >>>'
END_MARKER='# <<< kit <<<'

uninstall=0
dry_run=0
for arg in "$@"; do
    case "$arg" in
        --uninstall) uninstall=1 ;;
        --dry-run) dry_run=1 ;;
        -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

info() { echo "  - $*"; }

change() {  # change <description> <command...>
    local description=$1
    shift
    if [ "$dry_run" = 1 ]; then
        echo "  [dry-run] $description"
    else
        "$@"
        echo "  [done] $description"
    fi
}

# --- 1. launcher on PATH -------------------------------------------------------
# A tiny script rather than a symlink: it behaves the same everywhere, including
# Git Bash/MSYS on Windows, where `ln -s` silently makes a copy instead.

launcher_content() {
    printf '#!/bin/sh\n%s (%s)\nexec sh "%s/bin/kit" "$@"\n' "$LAUNCHER_MARKER" "$KIT_HOME" "$KIT_HOME"
}

is_kit_launcher() {
    [ -f "$LAUNCHER" ] && grep -qF "$LAUNCHER_MARKER" "$LAUNCHER"
}

write_launcher() {
    mkdir -p "$BIN_DIR"
    launcher_content > "$LAUNCHER"
    chmod +x "$LAUNCHER"
}

update_launcher() {
    if [ "$uninstall" = 1 ]; then
        if is_kit_launcher; then
            change "remove $LAUNCHER" rm "$LAUNCHER"
        else
            info "no kit launcher at $LAUNCHER"
        fi
        return
    fi

    if is_kit_launcher && [ "$(cat "$LAUNCHER")" = "$(launcher_content)" ]; then
        info "$LAUNCHER already up to date"
    elif { [ -e "$LAUNCHER" ] || [ -L "$LAUNCHER" ]; } && ! is_kit_launcher; then
        echo "error: $LAUNCHER already exists and was not written by kit - move it aside and re-run" >&2
        exit 1
    else
        change "write launcher $LAUNCHER" write_launcher
    fi
}

# --- 2. shell rc files ---------------------------------------------------------

rc_block() {
    cat <<EOF
$BEGIN_MARKER
# kit - personal toolbox ($KIT_HOME)
# Added by kit's install.sh. Puts the kit command on PATH and loads bash tab completion.
# Everything between the >>> kit >>> and <<< kit <<< markers is managed by the installer.
# To remove it, run:  "$KIT_HOME/install.sh" --uninstall
export KIT_HOME="$KIT_HOME"
case ":\$PATH:" in *":$BIN_DIR:"*) ;; *) export PATH="$BIN_DIR:\$PATH" ;; esac
if [ -n "\${BASH_VERSION:-}" ] && [ -f "\$KIT_HOME/shell/kit.bash" ]; then . "\$KIT_HOME/shell/kit.bash"; fi
$END_MARKER
EOF
}

without_block() {  # print a file with the kit block removed
    awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" \
        '$0 == begin { skip = 1; next } $0 == end { skip = 0; next } !skip' "$1"
}

write_rc() {  # write_rc <file> <content>
    if [ -f "$1" ]; then
        cp "$1" "$1.kit-backup"
    fi
    printf '%s\n' "$2" > "$1"
}

update_rc() {
    local rc=$1 current="" stripped="" new
    if [ -f "$rc" ]; then
        current=$(cat "$rc")
        stripped=$(without_block "$rc")
    fi

    if [ "$uninstall" = 1 ]; then
        if [ "$current" = "$stripped" ]; then
            info "no kit block in $rc"
            return
        fi
        change "remove kit block from $rc" write_rc "$rc" "$stripped"
    else
        if [ -n "$stripped" ]; then
            new="$stripped"$'\n\n'"$(rc_block)"
        else
            new=$(rc_block)
        fi
        if [ "$current" = "$new" ]; then
            info "$rc already up to date"
            return
        fi
        change "add kit block to $rc (backup: $rc.kit-backup)" write_rc "$rc" "$new"
    fi
}

# --- main ----------------------------------------------------------------------

if [ "$uninstall" = 1 ]; then verb=Uninstalling; else verb=Installing; fi
if [ "$dry_run" = 1 ]; then note=' (dry run - nothing will be changed)'; else note=''; fi
echo
echo "$verb kit from $KIT_HOME$note"

if [ "$uninstall" = 0 ] && ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
    echo "warning: python3 not found - kit needs Python 3.10+ (or set KIT_PYTHON)" >&2
fi

update_launcher
update_rc "$HOME/.bashrc"
if [ -f "$HOME/.zshrc" ]; then
    update_rc "$HOME/.zshrc"
fi

echo
if [ "$dry_run" = 1 ]; then
    echo "Dry run finished. Run again without --dry-run to apply."
elif [ "$uninstall" = 1 ]; then
    echo "kit is uninstalled. Open a new terminal for it to take effect."
else
    echo "kit is installed. Open a new terminal (or run: source ~/.bashrc), then try: kit"
fi
