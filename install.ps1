<#
.SYNOPSIS
    Install the pr-review skill into Antigravity (IDE + CLI) on Windows.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1
#>
[CmdletBinding()]
param(
    [switch]$IdeOnly,
    [switch]$CliOnly
)

$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'skills\pr-review'

if (-not (Test-Path (Join-Path $source 'SKILL.md'))) {
    throw "Cannot find skills\pr-review\SKILL.md next to this script."
}

$targets = @()
if (-not $CliOnly) { $targets += Join-Path $env:USERPROFILE '.gemini\config\skills\pr-review' }
if (-not $IdeOnly) { $targets += Join-Path $env:USERPROFILE '.gemini\antigravity-cli\skills\pr-review' }

foreach ($t in $targets) {
    if (Test-Path $t) { Remove-Item -Recurse -Force $t }
    $parent = Split-Path $t -Parent
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    Copy-Item -Recurse -Force $source $t
    Write-Host "installed -> $t"
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
    gh auth status
    if (-not $?) { Write-Warning 'gh is installed but not authenticated. Run:  gh auth login' }
}

Write-Host ''
Write-Host 'Done. Restart Antigravity, then try:  /pr-review 842 paxiai-event-processor'
