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
