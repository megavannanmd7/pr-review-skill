<#
.SYNOPSIS
    Install the pr-review skill for one or more AI coding tools (Windows).
.DESCRIPTION
    Copies core/ (the review procedure and helper scripts) to
    %USERPROFILE%\.pr-review-skill\core, then drops a small adapter into each
    selected tool's own skill/command directory. Every adapter points back at that
    one core, so there is never more than one copy of the rubric or the scripts.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -All
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -Platform claude-code,cursor
#>
[CmdletBinding()]
param(
    # Accepts -Platform a,b either as a PowerShell array or, when this script is run
    # via `powershell -File`, as the single string "a,b".
    [string[]]$Platform,
    [switch]$All
)

$ErrorActionPreference = 'Stop'
$known = @('antigravity', 'claude-code', 'cursor', 'gemini-cli')
$root = $PSScriptRoot
$home_ = $env:USERPROFILE
$coreDest = Join-Path $home_ '.pr-review-skill\core'

if (-not (Test-Path (Join-Path $root 'core\REVIEW.md'))) {
    throw "Cannot find core\REVIEW.md next to this script."
}
if ($All) {
    $Platform = $known
} elseif ($Platform) {
    $Platform = @($Platform | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}
if (-not $Platform -or $Platform.Count -eq 0) {
    Write-Host 'Usage: install.ps1 -All'
    Write-Host '       install.ps1 -Platform antigravity,claude-code,cursor,gemini-cli'
    exit 1
}
$unknown = $Platform | Where-Object { $known -notcontains $_ }
if ($unknown) {
    throw ("Unknown platform(s): " + ($unknown -join ', ') + ". Expected one or more of: " + ($known -join ', '))
}

function Install-Dir($source, $dest) {
    if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
    $parent = Split-Path $dest -Parent
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    Copy-Item -Recurse -Force $source $dest
    Write-Host "  -> $dest"
}

function Install-File($source, $dest) {
    $parent = Split-Path $dest -Parent
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    Copy-Item -Force $source $dest
    Write-Host "  -> $dest"
}

Write-Host 'Installing shared core...'
Install-Dir (Join-Path $root 'core') $coreDest
Get-ChildItem $coreDest -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-Item -Recurse -Force $_.FullName }

foreach ($p in $Platform) {
    Write-Host "Installing adapter: $p"
    switch ($p) {
        'antigravity' {
            # Antigravity indexes skills as a folder containing SKILL.md.
            $ide = Join-Path $home_ '.gemini\config\skills\pr-review'
            $cli = Join-Path $home_ '.gemini\antigravity-cli\skills\pr-review'
            foreach ($d in @($ide, $cli)) {
                if (Test-Path $d) { Remove-Item -Recurse -Force $d }
                Install-File (Join-Path $root 'adapters\antigravity\SKILL.md') (Join-Path $d 'SKILL.md')
            }
        }
        'claude-code' {
            $d = Join-Path $home_ '.claude\skills\pr-review'
            if (Test-Path $d) { Remove-Item -Recurse -Force $d }
            Install-File (Join-Path $root 'adapters\claude-code\SKILL.md') (Join-Path $d 'SKILL.md')
        }
        'cursor' {
            Install-File (Join-Path $root 'adapters\cursor\pr-review.md') (Join-Path $home_ '.cursor\commands\pr-review.md')
        }
        'gemini-cli' {
            Install-File (Join-Path $root 'adapters\gemini-cli\pr-review.toml') (Join-Path $home_ '.gemini\commands\pr-review.toml')
        }
    }
}

Write-Host ''
Write-Host 'Checking prerequisites...'

$py = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
if ($null -eq $py) {
    Write-Warning 'Python 3.9+ not found on PATH. Install it from python.org or the Microsoft Store.'
} else {
    Write-Host ("  python: " + $py.Source)
}

$gh = Get-Command gh -ErrorAction SilentlyContinue
if ($null -eq $gh) {
    Write-Warning 'GitHub CLI not found. Run:  winget install --id GitHub.cli'
    Write-Warning 'Then open a new terminal and run:  gh auth login'
} else {
    Write-Host ("  gh: " + $gh.Source)
    & $gh.Source auth status
    if (-not $?) { Write-Warning 'gh is installed but not authenticated. Run:  gh auth login' }
}

Write-Host ''
Write-Host 'Done. Restart the tool, then run:  /pr-review 842 paxiai-event-processor'
