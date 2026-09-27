<#
.SYNOPSIS
    Runs tools/make_package.py to package planning-mcp source and manifest.
.DESCRIPTION
    Wraps tools/make_package.py with Python detection and argument forwarding.
    Supports optional arguments like --with-python <path_to_embed_zip>.
.EXAMPLE
    .\package_source.ps1
.EXAMPLE
    .\package_source.ps1 --with-python C:\path\to\python-embed.zip
#>

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AdditionalArgs
)

$ErrorActionPreference = "Stop"

$RootDir = $PSScriptRoot
$MakePackagePy = Join-Path $RootDir "tools\make_package.py"

if (-not (Test-Path -LiteralPath $MakePackagePy)) {
    Write-Error "[ERROR] Script not found: $MakePackagePy"
    exit 1
}

# Resolve Python executable: try system python, embedded runtime, or py launcher
$PythonExe = $null

$EmbeddedPython = Join-Path $RootDir "runtime\python.exe"
$SystemPython = (Get-Command python -ErrorAction SilentlyContinue).Source
$PyLauncher = (Get-Command py -ErrorAction SilentlyContinue).Source

if ($SystemPython) {
    $PythonExe = $SystemPython
} elseif (Test-Path -LiteralPath $EmbeddedPython) {
    $PythonExe = $EmbeddedPython
} elseif ($PyLauncher) {
    $PythonExe = $PyLauncher
}

if (-not $PythonExe) {
    Write-Error "[ERROR] Python interpreter not found. Please install Python or ensure it is in PATH."
    exit 1
}

Write-Host "Running tools/make_package.py using $PythonExe..." -ForegroundColor Cyan

if ($AdditionalArgs) {
    & $PythonExe $MakePackagePy @AdditionalArgs
} else {
    & $PythonExe $MakePackagePy
}

$ExitCode = $LASTEXITCODE
if ($ExitCode -ne 0) {
    Write-Warning "make_package.py exited with code $ExitCode"
}
exit $ExitCode
