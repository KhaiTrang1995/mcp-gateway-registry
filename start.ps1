# Setup and start MCP Gateway & Registry on native Windows (Docker Desktop).
#
# Uses pre-built images plus the explicit Windows compose overlay for log volumes.
# Does not auto-load a generic compose override file.
#
# Usage:
#   .\start.ps1
#   .\start.ps1 -Help
#   .\start.ps1 -ResetData   # destructive: compose down --volumes
#
# Requires: Docker Desktop running, PowerShell 5.1+ or PowerShell 7+

#Requires -Version 5.1

[CmdletBinding()]
param(
    [switch]$Help,
    [switch]$ResetData,
    [switch]$SkipDockerStart
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LibPath = Join-Path $ScriptDir "scripts\windows\McpGatewayWindows.ps1"
if (-not (Test-Path -LiteralPath $LibPath)) {
    Write-Error "Missing helper library: $LibPath"
    exit 1
}
. $LibPath

function Show-McpGatewayWindowsHelp {
    @'
MCP Gateway & Registry - Windows start script

Usage:
  .\start.ps1              Start prebuilt stack (safe default; keeps volumes)
  .\start.ps1 -Help        Show this help and exit 0
  .\start.ps1 -ResetData   Stop stack and REMOVE named volumes, then start
  .\start.ps1 -SkipDockerStart
                           Prepare .env and home dirs only (no compose up)

Prerequisites:
  - Windows 10/11 with Docker Desktop (WSL2 backend recommended)
  - Docker Compose v2 available as: docker compose

Compose files used:
  docker-compose.prebuilt.yml
  docker-compose.windows.yml   (named volume for app logs; opt-in only)

Environment:
  Sets HOME=%USERPROFILE% so ${HOME}/mcp-gateway mounts resolve correctly.

Docs:
  docs/windows-setup-guide.md
'@ | Write-Host
}


function Initialize-McpGatewayDotEnv {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRoot
    )
    $envFile = Join-Path $RepoRoot ".env"
    $envExample = Join-Path $RepoRoot ".env.example"
    if (-not (Test-Path -LiteralPath $envFile)) {
        if (-not (Test-Path -LiteralPath $envExample)) {
            throw ".env.example not found; cannot create .env"
        }
        Copy-Item -LiteralPath $envExample -Destination $envFile
        Write-Host "Created .env from .env.example"
    }

    $adminPass = Get-McpGatewayEnvVar -Path $envFile -Key "KEYCLOAK_ADMIN_PASSWORD"
    if ([string]::IsNullOrEmpty($adminPass) -or $adminPass -in @("changeme", "admin")) {
        $newPass = New-McpGatewayRandomPassword
        Set-McpGatewayEnvVar -Path $envFile -Key "KEYCLOAK_ADMIN_PASSWORD" -Value $newPass
        Set-McpGatewayEnvVar -Path $envFile -Key "INITIAL_ADMIN_PASSWORD" -Value $newPass
        Write-Host "Generated secure KEYCLOAK_ADMIN_PASSWORD."
    }

    $dbPass = Get-McpGatewayEnvVar -Path $envFile -Key "KEYCLOAK_DB_PASSWORD"
    if ([string]::IsNullOrEmpty($dbPass) -or $dbPass -in @("keycloak", "changeme")) {
        Set-McpGatewayEnvVar -Path $envFile -Key "KEYCLOAK_DB_PASSWORD" -Value (New-McpGatewayRandomPassword)
        Write-Host "Generated secure KEYCLOAK_DB_PASSWORD."
    }

    $userPass = Get-McpGatewayEnvVar -Path $envFile -Key "INITIAL_USER_PASSWORD"
    if ([string]::IsNullOrEmpty($userPass) -or $userPass -eq "testpass") {
        Set-McpGatewayEnvVar -Path $envFile -Key "INITIAL_USER_PASSWORD" -Value (New-McpGatewayRandomPassword)
        Write-Host "Generated secure INITIAL_USER_PASSWORD."
    }

    $secretKey = Get-McpGatewayEnvVar -Path $envFile -Key "SECRET_KEY"
    if ([string]::IsNullOrEmpty($secretKey) -or $secretKey -eq "your_secret_key_here") {
        Set-McpGatewayEnvVar -Path $envFile -Key "SECRET_KEY" -Value (New-McpGatewayRandomHex -ByteCount 32)
        Write-Host "Generated secure SECRET_KEY."
    }

    foreach ($key in @(
            "METRICS_API_KEY_REGISTRY",
            "METRICS_API_KEY_AUTH_SERVER",
            "METRICS_API_KEY_MCPGW_SERVER"
        )) {
        $val = Get-McpGatewayEnvVar -Path $envFile -Key $key
        if ([string]::IsNullOrEmpty($val)) {
            Set-McpGatewayEnvVar -Path $envFile -Key $key -Value ("mcp_metrics_" + (New-McpGatewayRandomHex -ByteCount 16))
        }
    }

    Set-McpGatewayEnvVar -Path $envFile -Key "SESSION_COOKIE_SECURE" -Value "false"
    Set-McpGatewayEnvVar -Path $envFile -Key "AUTH_PROVIDER" -Value "keycloak"
    Set-McpGatewayEnvVar -Path $envFile -Key "KEYCLOAK_ENABLED" -Value "true"

    return $envFile
}


function Copy-McpGatewaySeedConfigs {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRoot,
        [Parameter(Mandatory = $true)]
        [string]$McpHome,
        [Parameter(Mandatory = $true)]
        [string]$EnvFile
    )

    # Load .env into process for placeholder expansion
    if (Test-Path -LiteralPath $EnvFile) {
        foreach ($line in (Get-Content -LiteralPath $EnvFile)) {
            if ($line -match '^\s*([A-Za-z0-9_]+)=(.*)$') {
                $k = $Matches[1]
                $v = $Matches[2].Trim().Trim('"').Trim("'")
                [System.Environment]::SetEnvironmentVariable($k, $v, "Process")
            }
        }
    }

    $scopesSrc = Join-Path $RepoRoot "auth_server\scopes.yml"
    $scopesDst = Join-Path $McpHome "auth_server\scopes.yml"
    if ((Test-Path -LiteralPath $scopesSrc) -and -not (Test-Path -LiteralPath $scopesDst)) {
        Copy-Item -LiteralPath $scopesSrc -Destination $scopesDst
        Write-Host "Copied scopes.yml (initial setup)."
    }
    elseif (Test-Path -LiteralPath $scopesDst) {
        Write-Host "Keeping existing scopes.yml (not overwriting)."
    }

    $serversSrc = Join-Path $RepoRoot "registry\servers"
    $serversDst = Join-Path $McpHome "servers"
    if (Test-Path -LiteralPath $serversSrc) {
        # BOM-free UTF-8: registry Python json.load(encoding=utf-8) rejects EF BB BF.
        $null = Export-McpGatewayServerJsonFiles -SourceDir $serversSrc -DestDir $serversDst
        Write-Host "Predefined server configurations copied."
    }

    $agentsSrc = Join-Path $RepoRoot "cli\examples"
    $agentsDst = Join-Path $McpHome "agents"
    if (Test-Path -LiteralPath $agentsSrc) {
        Get-ChildItem -Path $agentsSrc -Filter "*agent*.json" -ErrorAction SilentlyContinue | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $agentsDst $_.Name) -Force
        }
        Write-Host "Seed agent configurations copied."
    }
}


function Invoke-McpGatewayCompose {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRoot,
        [switch]$ResetData
    )
    Push-Location $RepoRoot
    try {
        $composeArgs = Get-McpGatewayComposeArgs
        if ($ResetData) {
            Write-Host "WARNING: -ResetData removes named volumes (data loss)."
            & docker compose @composeArgs down --volumes --remove-orphans 2>$null
        }
        else {
            # Safe default: stop containers/networks only; keep volumes
            & docker compose @composeArgs down --remove-orphans 2>$null
        }

        if (-not $env:DOCKERHUB_ORG) {
            $env:DOCKERHUB_ORG = "mcpgateway"
        }

        Write-Host "Starting containers (prebuilt + windows overlay)..."
        & docker compose @composeArgs up -d
        if ($LASTEXITCODE -ne 0) {
            throw "docker compose up failed with exit code $LASTEXITCODE"
        }

        Write-Host "Running MongoDB init..."
        & docker compose @composeArgs up mongodb-init
        if ($LASTEXITCODE -ne 0) {
            Write-Host "WARNING: mongodb-init exited with code $LASTEXITCODE"
        }

        Write-Host "Waiting for Keycloak..."
        $keycloakUrl = "http://localhost:8080/realms/master"
        $ready = $false
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            try {
                $response = Invoke-WebRequest -Uri $keycloakUrl -UseBasicParsing -TimeoutSec 2
                if ($response.StatusCode -eq 200) {
                    $ready = $true
                    break
                }
            }
            catch {
                # retry
            }
            Write-Host "." -NoNewline
            Start-Sleep -Seconds 5
        }
        Write-Host ""
        if (-not $ready) {
            throw "Keycloak did not become ready. Check: docker compose $($composeArgs -join ' ') logs keycloak"
        }

        Write-Host "Configuring Keycloak (init + bootstrap)..."
        # Mount repo so bash setup scripts run inside a Linux container (they are not ported).
        & docker compose @composeArgs run --user root --rm `
            -v "${RepoRoot}:/repo" -w /repo registry sh -c @"
apt-get update -qq && apt-get install -y -qq jq >/dev/null 2>&1 || true
KEYCLOAK_ADMIN_URL=http://keycloak:8080 /repo/keycloak/setup/init-keycloak.sh
KEYCLOAK_ADMIN_URL=http://keycloak:8080 /repo/cli/bootstrap_user_and_m2m_setup.sh
"@

        Write-Host "Restarting auth-server and registry..."
        & docker compose @composeArgs restart auth-server registry
    }
    finally {
        Pop-Location
    }
}


# --- main ---
if ($Help) {
    Show-McpGatewayWindowsHelp
    exit 0
}

Write-Host "=========================================================="
Write-Host "  MCP Gateway & Registry - Windows start"
Write-Host "=========================================================="
Write-Host ""

$homeSet = Set-McpGatewayHomeEnv
Write-Host "[1/6] HOME set to $homeSet (for compose `${HOME} mounts)"

if (-not (Test-McpGatewayDockerReady)) {
    Write-Host "ERROR: Docker is not running or not installed."
    Write-Host "Start Docker Desktop and ensure 'docker' is on PATH."
    exit 1
}
Write-Host "[2/6] Docker is reachable."

$repoRoot = $ScriptDir
Write-Host "[3/6] Preparing .env..."
$envFile = Initialize-McpGatewayDotEnv -RepoRoot $repoRoot

Write-Host "[4/6] Preparing home layout under $env:HOME\mcp-gateway..."
$mcpHome = Initialize-McpGatewayHomeLayout -HomeRoot $env:HOME
Copy-McpGatewaySeedConfigs -RepoRoot $repoRoot -McpHome $mcpHome -EnvFile $envFile

if ($SkipDockerStart) {
    Write-Host "[5/6] SkipDockerStart set - not starting containers."
    Write-Host "[6/6] Done (prep only)."
    exit 0
}

Write-Host "[5/6] Starting stack..."
Invoke-McpGatewayCompose -RepoRoot $repoRoot -ResetData:$ResetData

$finalAdmin = Get-McpGatewayEnvVar -Path $envFile -Key "KEYCLOAK_ADMIN_PASSWORD"
$finalUser = Get-McpGatewayEnvVar -Path $envFile -Key "INITIAL_USER_PASSWORD"
$composeArgs = Get-McpGatewayComposeArgs

Write-Host ""
Write-Host "=========================================================="
Write-Host "SUCCESS - MCP Gateway stack is running"
Write-Host "=========================================================="
Write-Host "Web Dashboard:     http://localhost"
Write-Host "Keycloak Admin UI: http://localhost:8080"
Write-Host ""
Write-Host "Credentials (from .env):"
Write-Host "  admin / $finalAdmin"
Write-Host "  testuser / $finalUser"
Write-Host ""
Write-Host "Useful commands:"
Write-Host ("  docker compose {0} ps" -f ($composeArgs -join " "))
Write-Host ("  docker compose {0} logs -f" -f ($composeArgs -join " "))
Write-Host ("  docker compose {0} down" -f ($composeArgs -join " "))
Write-Host "  (add --volumes only if you intend to wipe data)"
Write-Host "=========================================================="
