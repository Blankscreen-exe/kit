# Tab completion for the kit command in bash.
# Sourced by the block that install.sh adds to ~/.bashrc.
#   kit <Tab>        tools and built-in commands
#   kit help <Tab>   tool names (same after 'kit path')

_kit_complete() {
    local cur=${COMP_WORDS[COMP_CWORD]} mode=
    if [ "$COMP_CWORD" -eq 1 ]; then
        mode=all
    elif [ "$COMP_CWORD" -eq 2 ] && { [ "${COMP_WORDS[1]}" = help ] || [ "${COMP_WORDS[1]}" = path ]; }; then
        mode=tools
    fi
    [ -n "$mode" ] || return 0

    local IFS=$'\n'
    COMPREPLY=($(compgen -W "$(kit _complete "$mode" 2>/dev/null)" -- "$cur"))
}

complete -o default -F _kit_complete kit

# Runs kit unchanged, except that 'kit env set|unset|path' also updates this shell: the tool writes
# export/unset commands to the temp file named in KIT_ENV_APPLY, and they're sourced once it finishes.
kit() {
    case "${1-} ${2-}" in
        "env set" | "env unset" | "env path") ;;
        *) command kit "$@"; return ;;
    esac
    local __kit_apply __kit_status
    __kit_apply=$(mktemp "${TMPDIR:-/tmp}/kit-env.XXXXXX") || { command kit "$@"; return; }
    KIT_ENV_APPLY=$__kit_apply KIT_ENV_SHELL=bash command kit "$@"
    __kit_status=$?
    if [ -s "$__kit_apply" ]; then . "$__kit_apply"; fi
    rm -f "$__kit_apply"
    return "$__kit_status"
}
