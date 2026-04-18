<#
.SYNOPSIS
    Start (or stop) the FinFlow-AI backend services on Windows.

.DESCRIPTION
    Starts the agent (8000), vectorstore (8001) and database (8002) services with
    uvicorn from the project venv, waits for each /health endpoint, and stops all
    of them when you press Ctrl+C.

.PARAMETER NewWindows
    Open each service in its own console window instead of logging to logs\.

.PARAMETER NoReload
    Disable uvicorn auto-reload on code changes.

.PARAMETER Stop
    Stop any FinFlow services still listening on ports 8000-8002, then exit.

.PARAMETER TimeoutSec
    How long to wait for each service to become healthy (default 180; the first
    vectorstore start downloads the embedding model).

.EXAMPLE
    .\run_services.ps1
.EXAMPLE
    .\run_services.ps1 -NewWindows
.EXAMPLE
    .\run_services.ps1 -Stop
#>
param(
    [switch]$NewWindows,
    [switch]$NoReload,
    [switch]$Stop,
    [int]$TimeoutSec = 180
)

$ErrorActionPreference = 'Stop'
$Root   = $PSScriptRoot
$Python = Join-Path $Root 'venv\Scripts\python.exe'
$LogDir = Join-Path $Root 'logs'

$Services = @(
    @{ Name = 'agent';       Dir = 'backend\agent-service';       Port = 8000 },
    @{ Name = 'vectorstore'; Dir = 'backend\vectorstore-service'; Port = 8001 },
    @{ Name = 'database';    Dir = 'backend\database-service';    Port = 8002 }
)


# Kill a process and all of its children (uvicorn --reload runs a reloader plus a worker)
function Stop-Tree([int]$ProcessId) {
    Start-Process -FilePath 'taskkill.exe' -ArgumentList "/PID $ProcessId /T /F" `
        -WindowStyle Hidden -Wait
}

# Stop a FinFlow service by its uvicorn command line (catches a reloader whose app crashed
# before opening the port), then by whatever still listens on the port.
# Returns $true if anything was stopped.
function Stop-ServicePort([int]$Port) {
    $stopped = $false
    $uvicorns = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "uvicorn app\.main:app .*--port $Port\b" }
    foreach ($proc in $uvicorns) {
        Stop-Tree $proc.ProcessId
        $stopped = $true
    }

    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($procId in ($conns | Select-Object -ExpandProperty OwningProcess -Unique)) {
        Stop-Tree $procId
        $stopped = $true
    }
    return $stopped
}

function Test-PortInUse([int]$Port) {
    [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Wait-Healthy($svc) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if ($svc.Process.HasExited) { return $false }
        try {
            Invoke-WebRequest "http://127.0.0.1:$($svc.Port)/health" -UseBasicParsing -TimeoutSec 2 | Out-Null
            return $true
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    return $false
}


# === Stop mode ===
if ($Stop) {
    foreach ($svc in $Services) {
        if (Stop-ServicePort $svc.Port) {
            Write-Host "Stopped $($svc.Name) service (port $($svc.Port))"
        } else {
            Write-Host "$($svc.Name) service is not running"
        }
    }
    exit 0
}


# === Pre-flight checks ===
if (-not (Test-Path $Python)) {
    Write-Host "Virtual environment not found at venv\Scripts\python.exe" -ForegroundColor Red
    Write-Host "Create it first:"
    Write-Host "  python -m venv venv"
    Write-Host "  venv\Scripts\python -m pip install -r requirements.txt"
    exit 1
}

if (-not (Test-Path (Join-Path $Root 'backend\.env'))) {
    Write-Host "Warning: backend\.env not found. Services need it to reach Gemini and Supabase." -ForegroundColor Yellow
}

$busy = @($Services | Where-Object { Test-PortInUse $_.Port })
if ($busy.Count -gt 0) {
    Write-Host "Port(s) already in use: $(($busy | ForEach-Object { $_.Port }) -join ', ')" -ForegroundColor Red
    Write-Host "Stop old services with:  .\run_services.ps1 -Stop"
    exit 1
}


# === Start services ===
Clear-Host
Write-Host ""
Write-Host "  FinFlow AI" -ForegroundColor Cyan
Write-Host "  $(Get-Date)" -ForegroundColor DarkGray
Write-Host ""

# The services print emoji in debug output; force UTF-8 so Python doesn't crash on cp1252
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

if (-not $NewWindows) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}

try {
    foreach ($svc in $Services) {
        $uvicornArgs = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$($svc.Port)")
        if (-not $NoReload) { $uvicornArgs += '--reload' }

        $startArgs = @{
            FilePath         = $Python
            ArgumentList     = $uvicornArgs
            WorkingDirectory = Join-Path $Root $svc.Dir
            PassThru         = $true
        }
        if (-not $NewWindows) {
            # uvicorn logs to stderr; print() debug output goes to stdout
            $startArgs.WindowStyle            = 'Hidden'
            $startArgs.RedirectStandardError  = Join-Path $LogDir "$($svc.Name).log"
            $startArgs.RedirectStandardOutput = Join-Path $LogDir "$($svc.Name).out.log"
        }

        $svc.Process = Start-Process @startArgs
        Write-Host "  Starting $($svc.Name) service on port $($svc.Port)..." -ForegroundColor DarkGray
    }

    Write-Host ""
    $allHealthy = $true
    foreach ($svc in $Services) {
        $url = "http://localhost:$($svc.Port)"
        if (Wait-Healthy $svc) {
            Write-Host ("  [ OK ]  {0,-12} {1}   docs: {1}/docs" -f $svc.Name, $url) -ForegroundColor Green
        } else {
            $allHealthy = $false
            Write-Host ("  [FAIL]  {0,-12} {1}" -f $svc.Name, $url) -ForegroundColor Red
        }
    }

    Write-Host ""
    if (-not $allHealthy) {
        if ($NewWindows) {
            Write-Host "  A service failed to start; check its console window for the error." -ForegroundColor Yellow
        } else {
            Write-Host "  A service failed to start; check logs\<service>.log for the error." -ForegroundColor Yellow
        }
    }
    if (-not $NewWindows) {
        Write-Host "  Logs: logs\<service>.log (server) and logs\<service>.out.log (debug prints)" -ForegroundColor DarkGray
    }
    Write-Host "  Press Ctrl+C to stop all services" -ForegroundColor Cyan
    Write-Host ""

    # Wait until Ctrl+C, reporting any service that dies
    $reported = @{}
    while ($true) {
        Start-Sleep -Seconds 1
        foreach ($svc in $Services) {
            if ($svc.Process.HasExited -and -not $reported[$svc.Name]) {
                $reported[$svc.Name] = $true
                Write-Host "  $($svc.Name) service exited (code $($svc.Process.ExitCode))" -ForegroundColor Red
            }
        }
    }
}
finally {
    $ErrorActionPreference = 'Continue'
    Write-Host ""
    Write-Host "  Stopping services..." -ForegroundColor Yellow
    foreach ($svc in $Services) {
        if ($svc.Process -and -not $svc.Process.HasExited) {
            Stop-Tree $svc.Process.Id
        }
    }
    Write-Host "  Services stopped" -ForegroundColor Yellow
}
