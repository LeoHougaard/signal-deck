param(
    [string]$Vault = ".",
    [int]$Port = 8766,
    [switch]$SkipServer
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Invoke-Step {
    param(
        [string]$Name,
        [scriptblock]$Command
    )

    Write-Host ""
    Write-Host "== $Name =="
    & $Command
}

function Get-FreePort {
    param([int]$StartPort)

    $candidate = $StartPort
    while ($true) {
        $busy = $false
        if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
            $busy = [bool](Get-NetTCPConnection -LocalPort $candidate -ErrorAction SilentlyContinue)
        }
        if (-not $busy) {
            return $candidate
        }
        $candidate += 1
    }
}

$tempVault = Join-Path ([System.IO.Path]::GetTempPath()) ("signal-deck-test-" + $PID)
$serverProcess = $null

try {
    Invoke-Step "Unit tests" {
        python -m unittest discover -s tests
    }

    Invoke-Step "Compile check" {
        python -m py_compile signal_deck\render.py signal_deck\research.py signal_deck\server.py signal_deck\app.py
    }

    Invoke-Step "Current vault doctor" {
        python -m signal_deck --vault $Vault doctor
    }

    Invoke-Step "Isolated demo vault" {
        python -m signal_deck --vault $tempVault init --demo
        python -m signal_deck --vault $tempVault refresh
        python -m signal_deck --vault $tempVault render

        $html = Join-Path $tempVault "Signal Deck.html"
        $markdown = Join-Path $tempVault "Signal Deck.md"
        if (-not (Test-Path $html)) {
            throw "Expected dashboard HTML was not created: $html"
        }
        if (-not (Test-Path $markdown)) {
            throw "Expected dashboard Markdown was not created: $markdown"
        }
    }

    if (-not $SkipServer) {
        Invoke-Step "HTTP smoke test" {
            $freePort = Get-FreePort -StartPort $Port
            $argsList = @(
                "-m", "signal_deck",
                "--vault", $tempVault,
                "serve",
                "--host", "127.0.0.1",
                "--port", "$freePort",
                "--no-agent"
            )
            $serverProcess = Start-Process -FilePath "python" -ArgumentList $argsList -WorkingDirectory $root -WindowStyle Hidden -PassThru
            Start-Sleep -Seconds 2

            if ($serverProcess.HasExited) {
                throw "Server exited early with code $($serverProcess.ExitCode)"
            }

            $status = Invoke-RestMethod -Uri "http://127.0.0.1:$freePort/api/status" -TimeoutSec 8
            if ($status.status -ne "ok") {
                throw "Unexpected status response: $($status | ConvertTo-Json -Compress)"
            }

            $page = Invoke-WebRequest -Uri "http://127.0.0.1:$freePort/" -TimeoutSec 8 -UseBasicParsing
            if ($page.Content -notmatch "Signal Deck") {
                throw "Dashboard page did not contain Signal Deck"
            }

            Write-Host "Smoke URL: http://127.0.0.1:$freePort"
        }
    }

    Write-Host ""
    Write-Host "PASS: Signal Deck test run completed."
}
finally {
    if ($serverProcess -and -not $serverProcess.HasExited) {
        Stop-Process -Id $serverProcess.Id -Force
    }
    if (Test-Path $tempVault) {
        Remove-Item -LiteralPath $tempVault -Recurse -Force
    }
}
