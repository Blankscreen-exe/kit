<#
.SYNOPSIS
    Hooks kit into Windows so the 'kit' command works in every terminal.

.DESCRIPTION
    Makes four changes. Each is safe to repeat:
      1. Appends this repo's bin folder to your user PATH.
         The previous PATH value is backed up to %LOCALAPPDATA%\kit\backups first.
      2. Sets the KIT_HOME user environment variable to this repo.
      3. Adds a commented, clearly marked block to your PowerShell profile that loads
         tab completion (shell\kit.ps1). The profile is backed up first.
      4. Runs 'uv sync' to create .venv with the Python packages tools need (pyproject.toml).

    -DryRun  shows what would change without changing anything.
    -Uninstall  undoes 1-3 and leaves .venv in place.

.EXAMPLE
    .\install.ps1 -DryRun
.EXAMPLE
    .\install.ps1
.EXAMPLE
    .\install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$KitHome     = $PSScriptRoot
$BinDir      = Join-Path $KitHome 'bin'
$Completion  = Join-Path $KitHome 'shell\kit.ps1'
$BackupDir   = Join-Path $env:LOCALAPPDATA 'kit\backups'
$BeginMarker = '# >>> kit >>>'
$EndMarker   = '# <<< kit <<<'
$Stamp       = Get-Date -Format 'yyyyMMdd-HHmmss'

function Write-Info([string]$Message) { Write-Host "  - $Message" }

function Invoke-Change([string]$Description, [scriptblock]$Action) {
    if ($DryRun) {
        Write-Host "  [dry-run] $Description" -ForegroundColor Yellow
    } else {
        & $Action
        Write-Host "  [done] $Description" -ForegroundColor Green
    }
}

function Save-Backup([string]$Name, [string]$Content) {
    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
    $path = Join-Path $BackupDir "$Name.$Stamp.bak"
    [IO.File]::WriteAllText($path, $Content)
    Write-Info "backup saved: $path"
}

function Test-SamePath([string]$A, [string]$B) {
    $normalize = { param($p) [Environment]::ExpandEnvironmentVariables($p).Trim().TrimEnd('\') }
    return (& $normalize $A) -ieq (& $normalize $B)
}

# --- 1. user PATH --------------------------------------------------------------

function Update-UserPath {
    # Read and write the raw registry value so entries like %USERPROFILE%\bin stay unexpanded.
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
    try {
        $raw = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $entries = @($raw -split ';' | Where-Object { $_ -ne '' })
        $others = @($entries | Where-Object { -not (Test-SamePath $_ $BinDir) })
        $present = $entries.Count -ne $others.Count

        if ($Uninstall) {
            if (-not $present) { Write-Info "user PATH does not contain $BinDir"; return }
            $new = $others -join ';'
            $description = "remove $BinDir from user PATH"
        } else {
            if ($present) { Write-Info "user PATH already contains $BinDir"; return }
            $new = ($entries + $BinDir) -join ';'
            $description = "add $BinDir to user PATH"
        }

        Invoke-Change $description {
            Save-Backup 'user-path' $raw
            $key.SetValue('Path', $new, [Microsoft.Win32.RegistryValueKind]::ExpandString)
        }
    } finally {
        $key.Close()
    }
}

# --- 2. KIT_HOME ---------------------------------------------------------------

function Update-KitHome {
    # Setting a user variable through .NET also broadcasts WM_SETTINGCHANGE, so terminals
    # opened from now on pick up the PATH change without signing out.
    if ($Uninstall) {
        Invoke-Change 'remove the KIT_HOME user variable' { [Environment]::SetEnvironmentVariable('KIT_HOME', $null, 'User') }
    } else {
        Invoke-Change "set the KIT_HOME user variable to $KitHome" { [Environment]::SetEnvironmentVariable('KIT_HOME', $KitHome, 'User') }
    }
}

# --- 3. PowerShell profile -----------------------------------------------------

function Get-ProfilePaths {
    $documents = [Environment]::GetFolderPath('MyDocuments')
    $paths = @(Join-Path $documents 'WindowsPowerShell\Microsoft.PowerShell_profile.ps1')
    if (Get-Command pwsh -ErrorAction SilentlyContinue) {
        $paths += Join-Path $documents 'PowerShell\Microsoft.PowerShell_profile.ps1'
    }
    return $paths
}

function Get-ProfileBlock {
    $completionQuoted = $Completion -replace "'", "''"
    $installerQuoted = (Join-Path $KitHome 'install.ps1') -replace "'", "''"
    return @(
        $BeginMarker
        "# kit - personal toolbox ($KitHome)"
        '# Added by kit''s install.ps1. Loads tab completion for the kit command:'
        '#   kit <Tab>        cycles through tools and built-in commands'
        '#   kit help <Tab>   cycles through tool names'
        '# The kit command itself does not need this block; it is on your user PATH.'
        '# Everything between the >>> kit >>> and <<< kit <<< markers is managed by the installer.'
        "# To remove it, run:  & '$installerQuoted' -Uninstall"
        "if (Test-Path -LiteralPath '$completionQuoted') { . '$completionQuoted' }"
        $EndMarker
    ) -join "`r`n"
}

function Update-Profile([string]$ProfilePath) {
    $exists = Test-Path -LiteralPath $ProfilePath
    $content = if ($exists) { [IO.File]::ReadAllText($ProfilePath) } else { '' }
    $pattern = '(?ms)^' + [regex]::Escape($BeginMarker) + '.*?^' + [regex]::Escape($EndMarker) + '[^\r\n]*(\r?\n)?'
    $hasBlock = [regex]::IsMatch($content, $pattern)
    $stripped = [regex]::Replace($content, $pattern, '').TrimEnd()

    if ($Uninstall) {
        if (-not $hasBlock) { Write-Info "no kit block in $ProfilePath"; return }
        $new = if ($stripped) { $stripped + "`r`n" } else { '' }
        $description = "remove kit block from $ProfilePath"
    } else {
        $block = Get-ProfileBlock
        $new = if ($stripped) { $stripped + "`r`n`r`n" + $block + "`r`n" } else { $block + "`r`n" }
        if ($new -eq $content) { Write-Info "profile already up to date: $ProfilePath"; return }
        $description = if ($hasBlock) { "update kit block in $ProfilePath" }
                       elseif ($exists) { "add kit block to $ProfilePath" }
                       else { "create $ProfilePath with kit block" }
    }

    Invoke-Change $description {
        if ($exists) { Save-Backup ('profile-' + (Split-Path -Leaf (Split-Path -Parent $ProfilePath))) $content }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ProfilePath) | Out-Null
        # UTF-8 with BOM: Windows PowerShell 5.1 needs the BOM to read non-ASCII characters correctly.
        [IO.File]::WriteAllText($ProfilePath, $new, (New-Object System.Text.UTF8Encoding $true))
    }
}

# --- main ----------------------------------------------------------------------

$verb = if ($Uninstall) { 'Uninstalling' } else { 'Installing' }
$note = if ($DryRun) { ' (dry run - nothing will be changed)' } else { '' }
Write-Host ''
Write-Host "$verb kit from $KitHome$note"

function Sync-Packages {
    # All tools share one uv environment (.venv in this repo), described by pyproject.toml.
    if ($Uninstall) {
        Write-Info "left $KitHome\.venv in place (git-ignored; delete it by hand if you like)"
        return
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Warning 'uv was not found. Install it (https://docs.astral.sh/uv/) and re-run; until then kit falls back to plain python and tools that need packages will fail.'
        return
    }
    Invoke-Change 'install Python packages into .venv (uv sync)' {
        & uv sync --project $KitHome --quiet
        if ($LASTEXITCODE -ne 0) {
            # Antivirus or proxies that inspect HTTPS break uv's built-in certificates;
            # retry trusting the Windows certificate store instead.
            Write-Info 'uv sync failed; retrying with the system certificate store (--system-certs)'
            & uv sync --project $KitHome --quiet --system-certs
            if ($LASTEXITCODE -ne 0) { throw 'uv sync failed - see the error above' }
        }
    }
}

Update-UserPath
foreach ($profilePath in Get-ProfilePaths) { Update-Profile $profilePath }
Update-KitHome
Sync-Packages

if (-not $DryRun) {
    # Apply to this session as well, so kit works without opening a new terminal.
    $sessionPath = @($env:Path -split ';' | Where-Object { $_ -and -not (Test-SamePath $_ $BinDir) })
    if ($Uninstall) {
        Remove-Item Env:KIT_HOME -ErrorAction SilentlyContinue
    } else {
        $sessionPath += $BinDir
        $env:KIT_HOME = $KitHome
    }
    $env:Path = $sessionPath -join ';'
}

Write-Host ''
if ($DryRun) {
    Write-Host 'Dry run finished. Run again without -DryRun to apply.'
} elseif ($Uninstall) {
    Write-Host 'kit is uninstalled. Open a new terminal for it to take effect everywhere.'
} else {
    Write-Host 'kit is installed. It works in this window now; open a new terminal for tab completion.'
    Write-Host 'Try:  kit'
}
