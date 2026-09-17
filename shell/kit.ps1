# Tab completion for the kit command in PowerShell.
# Dot-sourced by the block that install.ps1 adds to your PowerShell profile.
#   kit <Tab>        cycles through tools and built-in commands
#   kit help <Tab>   cycles through tool names (same after 'kit path')

& {
    $launcher = Join-Path $PSScriptRoot '..\bin\kit.ps1'

    $completer = {
        param($wordToComplete, $commandAst, $cursorPosition)

        # Words fully typed before the cursor (the word being completed is excluded).
        $before = @($commandAst.CommandElements | Where-Object { $_.Extent.EndOffset -lt $cursorPosition })
        $mode = $null
        if ($before.Count -eq 1) {
            $mode = 'all'
        } elseif ($before.Count -eq 2 -and $before[1].Extent.Text -in @('help', 'path')) {
            $mode = 'tools'
        }
        if (-not $mode) { return }

        & $launcher _complete $mode 2>$null |
            Where-Object { $_ -like "$wordToComplete*" } |
            ForEach-Object { [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_) }
    }.GetNewClosure()

    Register-ArgumentCompleter -Native -CommandName 'kit', 'kit.ps1', 'kit.cmd' -ScriptBlock $completer

    # A kit function that runs the launcher unchanged, except that 'kit env set|unset|path' also
    # updates this terminal: the tool writes PowerShell commands to the temp file named in
    # KIT_ENV_APPLY, and they run here once it finishes.
    $wrapper = {
        $apply = $null
        if ($args.Count -ge 2 -and $args[0] -eq 'env' -and $args[1] -in @('set', 'unset', 'path')) {
            $apply = [IO.Path]::GetTempFileName()
            $env:KIT_ENV_APPLY = $apply
            $env:KIT_ENV_SHELL = 'powershell'
        }
        try {
            if ($MyInvocation.ExpectingInput) { $input | & $launcher @args } else { & $launcher @args }
        } finally {
            if ($apply) {
                $code = $LASTEXITCODE
                Remove-Item Env:KIT_ENV_APPLY, Env:KIT_ENV_SHELL -ErrorAction SilentlyContinue
                $commands = [IO.File]::ReadAllText($apply)
                Remove-Item -LiteralPath $apply -ErrorAction SilentlyContinue
                if ($commands.Trim()) { Invoke-Expression $commands }
                $global:LASTEXITCODE = $code
            }
        }
    }.GetNewClosure()
    Set-Item -Path Function:global:kit -Value $wrapper
}
