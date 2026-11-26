[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot

$apaHermesHome = [System.IO.Path]::GetFullPath((Join-Path $repo 'data\hermes-home'))
if (-not (Test-Path -LiteralPath $apaHermesHome)) {
    New-Item -ItemType Directory -Force -Path $apaHermesHome | Out-Null
}
$env:HERMES_HOME = $apaHermesHome
$env:UV_PROJECT_ENVIRONMENT = Join-Path $repo '.venv'

$apaBin = Join-Path $repo 'bin'
if (Test-Path -LiteralPath $apaBin) {
    if ($env:PATH -notlike "*$apaBin*") {
        $env:PATH = "$apaBin;$env:PATH"
    }
}

$py = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) {
    Write-Error "APA Python not found at $py"
    exit 1
}

& $py (Join-Path $repo 'apa.py') @Arguments
exit $LASTEXITCODE
