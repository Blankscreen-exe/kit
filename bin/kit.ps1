# kit launcher for PowerShell. PowerShell picks this over kit.cmd, which avoids cmd's
# argument-quoting quirks and its "Terminate batch job (Y/N)?" prompt on Ctrl+C.
$python = if ($env:KIT_PYTHON) { $env:KIT_PYTHON } else { 'python' }
$entry = Join-Path $PSScriptRoot '..\kit.py'

if ($MyInvocation.ExpectingInput) {
    $input | & $python $entry @args
} else {
    & $python $entry @args
}
exit $LASTEXITCODE
