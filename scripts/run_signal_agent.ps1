param(
    [string]$Vault = "."
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Push-Location $root
try {
    python -m signal_deck --vault $Vault refresh
}
finally {
    Pop-Location
}
