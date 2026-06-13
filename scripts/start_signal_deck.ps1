param(
    [string]$Vault = ".",
    [int]$Port = 8765,
    [switch]$NoAgent
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$argsList = @("-m", "signal_deck", "--vault", $Vault, "serve", "--host", "127.0.0.1", "--port", "$Port")
if ($NoAgent) {
    $argsList += "--no-agent"
}

Set-Location $root
python @argsList
