# kit launcher for PowerShell. PowerShell picks this over kit.cmd, which avoids cmd's
# argument-quoting quirks and its "Terminate batch job (Y/N)?" prompt on Ctrl+C.
# Runs kit inside the repo's uv environment so tools can use the packages in pyproject.toml.
# Set KIT_PYTHON to skip uv and use that interpreter directly.
$kitHome = Split-Path -Parent $PSScriptRoot
$entry = Join-Path $kitHome 'kit.py'

if ($env:KIT_PYTHON) {
    $command = $env:KIT_PYTHON
    $prefix = @()
} elseif (Get-Command uv -ErrorAction SilentlyContinue) {
    $command = 'uv'
    $prefix = @('run', '--quiet', '--project', $kitHome, 'python')
} else {
    $command = 'python'
    $prefix = @()
}

if ($MyInvocation.ExpectingInput) {
    $input | & $command @prefix $entry @args
} else {
    & $command @prefix $entry @args
}
exit $LASTEXITCODE
