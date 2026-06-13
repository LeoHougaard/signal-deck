param(
    [string]$Vault = "."
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
python -m signal_deck --vault $Vault doctor
